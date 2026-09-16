"""단일 워커 — 설계서 §4-[1], §9.

처리는 **직렬**이다. 동시에 두 파일을 적용하지 않으므로 캘린더 상태가 꼬이지 않는다.
실패는 상태별로 다르게 다룬다.
    네트워크 불가 → 파일을 `in_progress`로 두고 5분 뒤 재시도
    LLM 판정 실패 → `held`, `llm.retry_minutes` 뒤 재시도
    더 새로운 파일 도착 → 배치 축약으로 자연 대체
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import AppConfig
from ..core.pipeline import Pipeline, PipelineResult
from ..core.state import State
from ..gcal.client import CalendarError
from . import intake as I
from .folder_watch import scan_folder, wait_until_settled

log = logging.getLogger(__name__)

NETWORK_RETRY_SECONDS = 300  # 5분
SETTLED_AGE_SECONDS = 60  # 이보다 오래된 파일은 안정화를 기다리지 않는다


@dataclass
class WorkerDeps:
    config: AppConfig
    state: State
    pipeline: Pipeline
    intake: I.Intake


class Worker:
    """큐에서 파일을 하나씩 꺼내 파이프라인에 태운다."""

    def __init__(
        self,
        deps: WorkerDeps,
        work_queue: "queue.Queue[Path]",
        on_result: Callable[[PipelineResult], None] | None = None,
    ):
        self.deps = deps
        self.queue = work_queue
        self.on_result = on_result or (lambda _: None)
        self._stop = threading.Event()
        self._retry_at: float | None = None

    # ------------------------------------------------------------- 실행

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self, poll_seconds: float = 1.0) -> None:
        """트레이 앱에서 백그라운드 스레드로 돈다."""
        self.enqueue_startup_scan()
        while not self._stop.is_set():
            #  바깥 그물. `process` 안에서 CalendarError는 이미 다루지만, 그 밖의
            #  예외가 새 나오면 스레드가 죽어 **재시작 전까지 동기화가 영영 멈춘다.**
            #  한 파일 때문에 감시 전체를 잃지 않는다.
            try:
                batch = self._drain()
                if batch:
                    self.process(batch)
                elif self._retry_at and time.monotonic() >= self._retry_at:
                    self._retry_at = None
                    self.process(self._pending_paths())
            except Exception:  # noqa: BLE001
                log.exception("워커 처리 중 예외 — 다음 차례에 계속한다")
            self._stop.wait(poll_seconds)

    def enqueue_startup_scan(self) -> None:
        """프로그램이 꺼져 있는 동안 추가된 파일을 회수한다(설계서 §4-[1])."""
        folder = Path(self.deps.config.watch_dir or ".")
        for path in scan_folder(folder, self.deps.config.file_glob):
            self.queue.put(path)

    def _drain(self) -> list[Path]:
        """큐를 비워 한 배치로 만든다 — 배치 축약이 여기서 의미를 갖는다."""
        found: list[Path] = []
        while True:
            try:
                found.append(self.queue.get_nowait())
            except queue.Empty:
                break
        seen: set[Path] = set()
        return [p for p in found if not (p in seen or seen.add(p))]

    def _pending_paths(self) -> list[Path]:
        """재시도 대상: 아직 끝내지 못한 파일들."""
        return [Path(row["path"]) for row in self.deps.state.files_in_progress()]

    # ------------------------------------------------------------- 처리

    def process(
        self,
        paths: list[Path],
        *,
        force: bool = False,
        before_apply: Callable[[I.Candidate, list], None] | None = None,
    ) -> PipelineResult | None:
        settled = [p for p in paths if p.exists() and self._settle(p)]
        if not settled:
            return None

        result_of_intake = self.deps.intake.evaluate(settled, force=force)
        self.deps.intake.record(result_of_intake)
        chosen = result_of_intake.chosen
        if chosen is None:
            return None

        targets = self.deps.state.active_targets()
        if not targets:
            log.warning("등록된 계정이 없다 — %s를 적용하지 않는다", chosen.path.name)
            return None

        #  어떤 파일이 정본인지 정해진 **뒤**, 적용하기 **전**에만 할 수 있는 일이 있다.
        #  force의 `user_deleted` 정리가 그렇다 — 규칙 4가 그 기록을 읽기 때문이다.
        if before_apply is not None:
            before_apply(chosen, targets)

        log.info("적용 시작: %s (%s)", chosen.path.name, chosen.file_date)
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            result = self.deps.pipeline.run(chosen.path, targets, source_name=chosen.path.name)
        except CalendarError as exc:
            #  네트워크·API 문제 — 파일을 in_progress로 두고 나중에 다시 한다
            log.error("%s: 적용 실패, %d초 뒤 재시도 (%s)", chosen.path.name,
                      NETWORK_RETRY_SECONDS, exc)
            file_id = self.deps.state.record_file(
                path=str(chosen.path), sha256=chosen.sha256, status=I.IN_PROGRESS,
                file_date=chosen.file_date, mtime=chosen.mtime,
            )
            self.deps.state.record_run(file_id, None, {}, started_at, error=str(exc))
            self._schedule_retry(NETWORK_RETRY_SECONDS)
            return None

        status = I.HELD if result.any_held else I.APPLIED
        file_id = self.deps.state.record_file(
            path=str(chosen.path), sha256=chosen.sha256, status=status,
            file_date=chosen.file_date, mtime=chosen.mtime, summary=result.totals(),
        )
        #  [10] Journal — 대상마다 한 줄. 설정 창의 "최근 처리"가 이걸 읽는다.
        for outcome in result.outcomes:
            self.deps.state.record_run(
                file_id, outcome.target.id, outcome.counts, started_at, outcome.error
            )
        if status == I.HELD:
            log.warning("%s: 일부 (날짜, 구분)이 보류됐다 — %d분 뒤 재시도",
                        chosen.path.name, self.deps.config.llm.retry_minutes)
            self._schedule_retry(self.deps.config.llm.retry_minutes * 60)

        totals = result.totals()
        log.info("적용 끝: %s — 추가 %d · 덮어쓰기 %d · 표시 %d · 복구 %d · 보류 %d",
                 chosen.path.name, totals["created"], totals["updated"], totals["marked"],
                 totals["restored"], totals["held"])
        self.on_result(result)
        return result

    def force_refresh(self, path: Path | None = None, *, restore_deleted: bool = False) -> PipelineResult | None:
        """설정 창의 '강제 새로고침' — 중복·오래된 파일 규칙을 무시한다(설계서 Force).

        `restore_deleted`는 **그 파일이 덮는 날짜 범위의** 직접삭제 기록만 지운다.
        범위를 두지 않으면 9월 파일을 force했을 뿐인데 8월에 직접 지운 일정까지
        되살아난다 — 사용자가 지운 것을 되돌리는 셈이라 의도보다 훨씬 넓다.
        """
        folder = Path(self.deps.config.watch_dir or ".")
        paths = [path] if path else scan_folder(folder, self.deps.config.file_glob)

        def _clear_user_deleted(chosen: I.Candidate, targets: list) -> None:
            window = chosen.window()
            if window is None:
                log.warning("%s: 날짜 범위를 몰라 직접 삭제 기록을 지우지 않는다", chosen.path.name)
                return
            start, end = window
            for target in targets:
                cleared = self.deps.state.clear_user_deleted(target.id, start, end)
                log.info("%s: %s~%s의 직접 삭제 기록 %d건을 지웠다",
                         target.email, start, end, cleared)

        return self.process(
            paths, force=True, before_apply=_clear_user_deleted if restore_deleted else None
        )

    def _settle(self, path: Path) -> bool:
        """방금 쓰인 파일만 안정화를 기다린다.

        시작 시 스캔은 폴더의 모든 파일을 훑으므로, 오래된 파일까지 3초씩
        기다리면 시작이 몇십 초씩 늦어진다. 충분히 오래된 파일은 이미 안정적이다.
        """
        try:
            if time.time() - path.stat().st_mtime > SETTLED_AGE_SECONDS:
                return True
        except OSError:
            return False
        return wait_until_settled(path, settle_seconds=self.deps.config.settle_seconds)

    def _schedule_retry(self, seconds: float) -> None:
        self._retry_at = time.monotonic() + seconds
