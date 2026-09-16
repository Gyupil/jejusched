"""처리 여부 결정 — 설계서 §4-[2].

큐에 쌓인 파일 중 **무엇을 적용할지** 고르는 곳. 순서대로 적용해도 최종 상태는
같지만 `*` 표시 노이즈와 LLM 호출이 늘어나므로 최신본 1개만 적용한다.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ..config import AppConfig
from datetime import timedelta

from ..core.normalize import normalize
from ..core.state import State
from ..parsers.base import ParseError, Parser

log = logging.getLogger(__name__)

#  files.status 값
PENDING = "pending"
APPLIED = "applied"
SKIPPED_DUP = "skipped_dup"
SKIPPED_STALE = "skipped_stale"
SKIPPED_SUPERSEDED = "skipped_superseded"
FAILED = "failed"
IN_PROGRESS = "in_progress"
HELD = "held"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Candidate:
    path: Path
    sha256: str
    mtime: float
    file_date: date | None = None
    status: str = PENDING
    reason: str = ""
    #  파일에 실린 일정의 날짜 범위. 여기서 한 번 파싱하며 알아낸 값을 들고 다니면
    #  force가 같은 파일을 다시 파싱하지 않아도 된다(설계서 Force).
    event_start: date | None = None
    event_end: date | None = None

    @property
    def rank(self) -> tuple[date, float]:
        """배치 축약의 정렬 기준: (파일 날짜, mtime)이 가장 큰 것이 정본."""
        return (self.file_date or date.min, self.mtime)

    def window(self) -> tuple[date, date] | None:
        """`Pipeline._window`와 **같은 범위** — 일정 최소일 -1일 ~ 최대일 +1일.

        일정 날짜를 모르면 파일 날짜 하루로 좁힌다. 범위를 모른다고 전체를
        지워 버리면 force가 의도보다 훨씬 넓게 동작한다.
        """
        start, end = self.event_start, self.event_end
        if start is None or end is None:
            if self.file_date is None:
                return None
            start = end = self.file_date
        return start - timedelta(days=1), end + timedelta(days=1)


@dataclass
class IntakeResult:
    chosen: Candidate | None
    rejected: list[Candidate]

    @property
    def all(self) -> list[Candidate]:
        return ([self.chosen] if self.chosen else []) + self.rejected


class Intake:
    def __init__(self, state: State, parser: Parser, config: AppConfig):
        self.state = state
        self.parser = parser
        self.config = config

    def evaluate(self, paths: list[Path], *, force: bool = False) -> IntakeResult:
        """큐의 파일들을 보고 적용할 1개를 고른다.

        `force`면 중복(sha256)·오래된 파일 규칙을 무시한다. 배치 축약은
        force에서도 유지된다(어차피 1개만 적용하는 것이 목적).
        """
        candidates: list[Candidate] = []
        rejected: list[Candidate] = []

        for path in paths:
            try:
                digest = sha256_of(path)
                mtime = path.stat().st_mtime
            except OSError as exc:
                log.warning("%s: 읽을 수 없다 — %s", path, exc)
                continue

            cand = Candidate(path=path, sha256=digest, mtime=mtime)

            # 1) 이미 적용한 해시면 건너뛴다
            if not force and self.state.has_applied(digest):
                cand.status, cand.reason = SKIPPED_DUP, "이미 적용한 파일(sha256 동일)"
                rejected.append(cand)
                continue

            # 2) 헤더에서 파일 날짜를 얻는다. 실패하면 파일은 손대지 않는다.
            try:
                parsed = self.parser.parse(path)
                file_date, events, _, _ = normalize(parsed, datetime.fromtimestamp(mtime).year)
                cand.file_date = file_date
                if events:
                    cand.event_start = min(e.date for e in events)
                    cand.event_end = max(e.date for e in events)
            except (ParseError, ValueError) as exc:
                cand.status, cand.reason = FAILED, f"파싱 실패: {exc}"
                log.error("%s: %s", path, cand.reason)
                rejected.append(cand)
                continue

            candidates.append(cand)

        if not candidates:
            return IntakeResult(None, rejected)

        # 3) 배치 축약 — (file_date, mtime) 최대 1개만 남긴다
        candidates.sort(key=lambda c: c.rank, reverse=True)
        chosen, others = candidates[0], candidates[1:]
        for other in others:
            other.status = SKIPPED_SUPERSEDED
            other.reason = f"더 새로운 파일이 있다: {chosen.path.name}"
        rejected.extend(others)

        # 4) 오래된 파일 규칙 — 같은 날짜의 재저장본은 정상 처리한다
        newest_applied = self.state.max_applied_file_date()
        if not force and newest_applied and chosen.file_date and chosen.file_date < newest_applied:
            chosen.status = SKIPPED_STALE
            chosen.reason = f"이미 {newest_applied} 파일까지 적용했다"
            rejected.append(chosen)
            return IntakeResult(None, rejected)

        chosen.status = IN_PROGRESS
        return IntakeResult(chosen, rejected)

    def record(self, result: IntakeResult) -> None:
        """결정 결과를 files 테이블에 남긴다."""
        for cand in result.all:
            self.state.record_file(
                path=str(cand.path),
                sha256=cand.sha256,
                status=cand.status,
                file_date=cand.file_date,
                mtime=cand.mtime,
                summary={"reason": cand.reason} if cand.reason else None,
            )
