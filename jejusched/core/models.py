"""도메인 모델. 설계서 §2의 데이터 모델을 그대로 옮긴 것."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum
from typing import Any


class Section(Enum):
    """일정 구분. 값은 원문 표기 그대로 쓴다(로그·프롬프트에 노출됨)."""

    GOV = "도지사"
    ADMIN_VG = "행정부지사"
    CLIMATE_VG = "기후경제부지사"
    DEPT = "실 일정"

    @classmethod
    def from_text(cls, text: str) -> Section:
        """'실⏎일⏎정'처럼 공백이 섞인 원문도 받아들인다."""
        squashed = "".join(text.split())
        for member in cls:
            if "".join(member.value.split()) == squashed:
                return member
        raise ValueError(f"알 수 없는 구분: {text!r}")


class EventStatus(Enum):
    ACTIVE = "active"
    MARKED = "marked"  # 새 파일에서 빠져 `* ` 표시된 상태


class Op(Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    MARK = "MARK"
    RESTORE = "RESTORE"  # 표시 해제 + 내용 갱신
    SKIP = "SKIP"
    HOLD = "HOLD"  # LLM 판정 보류 — 이번 실행에서 손대지 않음


# ---------------------------------------------------------------- 파서 출력


@dataclass
class ScheduleItem:
    """파서가 뱉는 원문에 가까운 한 행."""

    section: str
    date_text: str | None  # 행의 날짜 셀 원문("9/5(토)"), 없으면 파일 날짜를 쓴다
    time_text: str  # "10:00" | "10:00~17:00" | "종일" 등 원문
    title: str
    location: str
    attendees: str  # 줄바꿈 포함 가능
    department: str
    row_index: int  # 디버깅용


@dataclass
class ParsedFile:
    file_date_month: int
    file_date_day: int
    file_weekday: str  # "월"
    items: list[ScheduleItem]
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------ 정규화 결과


@dataclass(frozen=True)
class CanonicalEvent:
    """비교·적용의 단위. frozen이라 해시 가능하고 실수로 바뀌지 않는다."""

    date: date
    section: Section
    all_day: bool
    start: time | None  # all_day면 None
    end: time | None
    title: str
    title_norm: str  # 비교용
    location: str
    attendees: str  # 줄바꿈 → ", "
    department: str
    note: str  # "*도 본청 셔틀 버스(17:00~)" 같은 부가 문구
    slot: str  # "10:00" | "ALLDAY" — 같은 시간 판정용
    event_key: str  # sha1(date|section|slot|title_norm)[:16]
    content_hash: str  # sha1(모든 필드)[:16]
    row_index: int = -1

    @property
    def time_text(self) -> str:
        """사람이 읽는 시간 표기(로그·LLM 프롬프트용)."""
        if self.all_day:
            return "종일"
        assert self.start is not None
        head = self.start.strftime("%H:%M")
        return f"{head}~{self.end.strftime('%H:%M')}" if self.end else head


@dataclass
class ExistingEvent:
    """캘린더에서 읽어온 기존 일정.

    설계서 §2.3대로 상태는 `extendedProperties.private`에 들어 있으므로
    로컬 DB 없이도 이 객체를 온전히 복원할 수 있다.
    """

    logical_id: str
    google_event_id: str
    event_key: str
    content_hash: str
    date: date
    section: Section
    slot: str
    title: str
    title_norm: str
    status: EventStatus
    src_file_date: date | None = None
    last_seen_file_date: date | None = None
    # 규칙 3(LLM)에 넘길 원문 필드
    time_text: str = ""
    location: str = ""
    attendees: str = ""
    department: str = ""
    #  MARK 시 본문을 보존하려면 기존 설명이 필요하다(§11-5)
    description: str = ""

    @property
    def is_marked(self) -> bool:
        return self.status is EventStatus.MARKED


# ------------------------------------------------------------------ 계획


@dataclass
class PlannedOp:
    """Planner가 만드는 하나의 연산."""

    op: Op
    logical_id: str
    date: date
    section: Section
    title: str  # 로그용 (표시 접두 제외한 행사명)
    google_event_id: str | None = None
    body: dict[str, Any] | None = None  # Google 이벤트 본문(CREATE/UPDATE/MARK/RESTORE)
    reason: str = ""  # "규칙 1: event_key 일치" 등
    #  적용 직후 events 인덱스를 갱신하기 위한 값 (§4-[9])
    event_key: str = ""
    content_hash: str = ""
    slot: str = ""
    status: str = "active"

    def __str__(self) -> str:
        return f"{self.op.value} {self.date} [{self.section.value}] {self.title} ({self.reason})"
