"""연산 목록 생성 — 설계서 §4-[8], §2.3의 Google 이벤트 매핑.

Planner는 네트워크를 모른다. `PlannedOp` 목록만 만들고 실제 호출은 Applier가 한다.
덕분에 `dry_run`은 여기까지만 돌리면 된다.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..config import AppConfig
from .matcher import MatchResult
from .models import CanonicalEvent, ExistingEvent, Op, PlannedOp, Section

TIMEZONE = "Asia/Seoul"
APP_TAG = "jjsched"
MARK_NOTICE_PREFIX = "※ "


def new_logical_id() -> str:
    return str(uuid.uuid4())


def event_id_from_logical(logical_id: str) -> str:
    """logical_id → Google 이벤트 id.

    base32hex 소문자(문자 집합 `[0-9a-v]`, 26자)라 Calendar API의 id 규칙을
    만족한다. 같은 logical_id는 항상 같은 id를 내므로 CREATE 재시도가 멱등이다.
    """
    raw = uuid.UUID(logical_id).bytes
    return base64.b32hexencode(raw).decode("ascii").rstrip("=").lower()


@dataclass
class PlanContext:
    """한 파일을 한 대상에 적용할 때 쓰는 부가 정보."""

    file_date: date
    source_name: str  # "0910_주요일정.hwpx"


class Planner:
    def __init__(self, config: AppConfig):
        self.config = config

    # ------------------------------------------------------------ 본문 조립

    def _time_block(self, event: CanonicalEvent) -> dict[str, Any]:
        if event.all_day:
            return {
                "start": {"date": event.date.isoformat()},
                "end": {"date": (event.date + timedelta(days=1)).isoformat()},
            }
        assert event.start is not None and event.end is not None
        return {
            "start": {
                "dateTime": f"{event.date.isoformat()}T{event.start.strftime('%H:%M:%S')}",
                "timeZone": TIMEZONE,
            },
            "end": {
                "dateTime": f"{event.date.isoformat()}T{event.end.strftime('%H:%M:%S')}",
                "timeZone": TIMEZONE,
            },
        }

    def _description(self, event: CanonicalEvent, ctx: PlanContext) -> str:
        lines = []
        if event.attendees:
            lines.append(f"참석: {event.attendees}")
        if event.department:
            lines.append(f"주관부서: {event.department}")
        lines.append(f"구분: {event.section.value}")
        if event.note:
            lines.append(f"비고: {event.note}")
        lines.append(f"출처: {ctx.source_name} ({ctx.file_date.isoformat()})")
        return "\n".join(lines)

    def _reminders(self) -> dict[str, Any]:
        if not self.config.reminders:
            return {"useDefault": False, "overrides": []}
        return {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in self.config.reminders],
        }

    def _transparency(self, section: Section) -> str:
        return "transparent" if section.value in self.config.transparent_sections else "opaque"

    def _color(self, section: Section, *, marked: bool) -> str | None:
        if marked and self.config.mark_change_color:
            return self.config.mark_color
        return self.config.color_map.get(section.value)

    def _private_props(
        self, event: CanonicalEvent, logical_id: str, ctx: PlanContext, *, marked: bool
    ) -> dict[str, str]:
        return {
            "app": APP_TAG,
            "logical_id": logical_id,
            "key": event.event_key,
            "chash": event.content_hash,
            "section": event.section.value,
            "status": "marked" if marked else "active",
            "src": ctx.file_date.isoformat(),
            "seen": ctx.file_date.isoformat(),
        }

    def build_body(
        self, event: CanonicalEvent, logical_id: str, ctx: PlanContext, *, marked: bool = False
    ) -> dict[str, Any]:
        """CREATE/UPDATE/RESTORE가 쓰는 전체 본문."""
        prefix = self.config.mark_prefix if marked else ""
        body: dict[str, Any] = {
            "summary": f"{prefix}{event.title}",
            "location": event.location,
            "description": self._description(event, ctx),
            "transparency": self._transparency(event.section),
            "reminders": self._reminders(),
            "extendedProperties": {
                "private": self._private_props(event, logical_id, ctx, marked=marked)
            },
            **self._time_block(event),
        }
        color = self._color(event.section, marked=marked)
        if color:
            body["colorId"] = color
        return body

    def build_mark_body(self, existing: ExistingEvent, ctx: PlanContext) -> dict[str, Any]:
        """MARK는 내용을 바꾸지 않는다 — 접두·색상·상태·안내 문구만 건드린다."""
        notice = f"{MARK_NOTICE_PREFIX}{ctx.file_date.isoformat()} 파일부터 목록에서 빠짐"
        kept = [
            line
            for line in existing.description.split("\n")
            if not line.startswith(MARK_NOTICE_PREFIX)
        ]
        body: dict[str, Any] = {
            "summary": f"{self.config.mark_prefix}{existing.title}",
            "description": "\n".join([notice, *kept]).strip(),
            "extendedProperties": {"private": {"status": "marked"}},
        }
        if self.config.mark_change_color:
            body["colorId"] = self.config.mark_color
        return body

    # ------------------------------------------------------------- 계획 생성

    def plan(self, result: MatchResult, ctx: PlanContext) -> list[PlannedOp]:
        ops: list[PlannedOp] = []

        for match in result.matches:
            inc, ex = match.incoming, match.existing
            if ex.is_marked:
                op = Op.RESTORE  # 표시 해제 + 새 내용 적용
                reason = f"{match.reason} → 표시 해제"
            elif ex.content_hash != inc.content_hash:
                op = Op.UPDATE
                reason = f"{match.reason} → 내용 변경"
            else:
                ops.append(
                    PlannedOp(
                        op=Op.SKIP,
                        logical_id=ex.logical_id,
                        date=inc.date,
                        section=inc.section,
                        title=inc.title,
                        google_event_id=ex.google_event_id,
                        reason=f"{match.reason} → 변경 없음",
                        event_key=inc.event_key,
                        content_hash=inc.content_hash,
                        slot=inc.slot,
                    )
                )
                continue
            ops.append(
                PlannedOp(
                    op=op,
                    logical_id=ex.logical_id,
                    date=inc.date,
                    section=inc.section,
                    title=inc.title,
                    google_event_id=ex.google_event_id,
                    body=self.build_body(inc, ex.logical_id, ctx),
                    reason=reason,
                    event_key=inc.event_key,
                    content_hash=inc.content_hash,
                    slot=inc.slot,
                )
            )

        for inc in result.creates:
            logical_id = new_logical_id()
            ops.append(
                PlannedOp(
                    op=Op.CREATE,
                    logical_id=logical_id,
                    date=inc.date,
                    section=inc.section,
                    title=inc.title,
                    google_event_id=event_id_from_logical(logical_id),
                    body=self.build_body(inc, logical_id, ctx),
                    reason="규칙 4: 새 일정",
                    event_key=inc.event_key,
                    content_hash=inc.content_hash,
                    slot=inc.slot,
                )
            )

        for ex in result.marks:
            ops.append(
                PlannedOp(
                    op=Op.MARK,
                    logical_id=ex.logical_id,
                    date=ex.date,
                    section=ex.section,
                    title=ex.title,
                    google_event_id=ex.google_event_id,
                    body=self.build_mark_body(ex, ctx),
                    reason="규칙 5: 새 파일에 없음",
                    event_key=ex.event_key,
                    content_hash=ex.content_hash,
                    slot=ex.slot,
                    status="marked",
                )
            )

        for when, section in sorted(result.held, key=lambda k: (k[0], k[1].value)):
            ops.append(
                PlannedOp(
                    op=Op.HOLD,
                    logical_id="",
                    date=when,
                    section=section,
                    title="(판정 보류)",
                    reason="규칙 3: LLM 판정 실패 — 이 날짜·구분은 이번에 건너뛴다",
                )
            )
        return ops
