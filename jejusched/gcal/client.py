"""Calendar 클라이언트 인터페이스와 Google 이벤트 ↔ 도메인 변환.

`extendedProperties.private`가 상태의 원본이다. 로컬 DB가 사라져도 여기서
`ExistingEvent`를 온전히 복원할 수 있어야 한다(설계서 §2.3).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any, Protocol, runtime_checkable

from ..core.models import EventStatus, ExistingEvent, Section
from ..core.normalize import title_norm

APP_TAG = "jjsched"
_MARK_PREFIX_RE = re.compile(r"^\s*\*+\s*")


class CalendarError(RuntimeError):
    """캘린더 호출 실패의 최상위."""


class AuthExpired(CalendarError):
    """401 / invalid_grant — 이 대상은 재로그인이 필요하다."""


class EventAlreadyExists(CalendarError):
    """409 — 결정적 id로 CREATE 재시도했을 때. 성공으로 간주한다."""


class EventNotFound(CalendarError):
    """404 — 사용자가 캘린더에서 직접 지웠다."""


class RateLimited(CalendarError):
    """403 rateLimitExceeded / 5xx — 지수 백오프 후 재시도."""


@runtime_checkable
class CalendarClient(Protocol):
    def ensure_calendar(self, name: str) -> str:
        """이름이 같은 보조 캘린더를 찾거나 만들고 calendar_id를 준다."""

    def list_events(self, calendar_id: str, time_min: date, time_max: date) -> list[dict[str, Any]]:
        """`app=jjsched`가 붙은 이벤트만, 페이지네이션을 다 돌아서 준다."""

    def insert_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def delete_event(self, calendar_id: str, event_id: str) -> None: ...


# --------------------------------------------------------------- 변환


def strip_mark(summary: str) -> str:
    return _MARK_PREFIX_RE.sub("", summary or "").strip()


def _parse_start(block: dict[str, Any]) -> tuple[date, bool, time | None]:
    if "date" in block:
        return date.fromisoformat(block["date"]), True, None
    raw = block["dateTime"]
    moment = datetime.fromisoformat(raw)
    return moment.date(), False, moment.time().replace(second=0, microsecond=0)


def to_existing_event(raw: dict[str, Any]) -> ExistingEvent | None:
    """Google 이벤트 dict → ExistingEvent. 우리 이벤트가 아니면 None."""
    props = (raw.get("extendedProperties") or {}).get("private") or {}
    if props.get("app") != APP_TAG:
        return None
    if raw.get("status") == "cancelled":
        return None

    when, all_day, start = _parse_start(raw["start"])
    slot = "ALLDAY" if all_day else start.strftime("%H:%M")  # type: ignore[union-attr]

    time_text = "종일"
    if not all_day:
        _, _, end = _parse_start(raw["end"])
        time_text = start.strftime("%H:%M")  # type: ignore[union-attr]
        if end is not None:
            time_text = f"{time_text}~{end.strftime('%H:%M')}"

    title = strip_mark(raw.get("summary", ""))
    try:
        section = Section.from_text(props.get("section", ""))
    except ValueError:
        return None  # 구분을 모르면 비교 단위를 정할 수 없다 — 건드리지 않는다

    #  접두와 status 속성이 어긋나면 **속성을 따른다**(설계서 §4-[5])
    status = EventStatus(props.get("status", EventStatus.ACTIVE.value))

    return ExistingEvent(
        logical_id=props.get("logical_id", ""),
        google_event_id=raw["id"],
        event_key=props.get("key", ""),
        content_hash=props.get("chash", ""),
        date=when,
        section=section,
        slot=slot,
        title=title,
        title_norm=title_norm(title),
        status=status,
        src_file_date=_maybe_date(props.get("src")),
        last_seen_file_date=_maybe_date(props.get("seen")),
        time_text=time_text,
        location=raw.get("location", "") or "",
        attendees=_field_from_description(raw.get("description", ""), "참석"),
        department=_field_from_description(raw.get("description", ""), "주관부서"),
        description=raw.get("description", "") or "",
    )


def _maybe_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _field_from_description(description: str, label: str) -> str:
    for line in (description or "").split("\n"):
        if line.startswith(f"{label}: "):
            return line[len(label) + 2 :].strip()
    return ""


# ------------------------------------------------- 실제 Google 구현 (M3)

def _retryable(attempt: int, max_attempts: int = 5) -> bool:
    return attempt + 1 < max_attempts


def _backoff_seconds(attempt: int) -> float:
    """지수 백오프 + 지터. 0.5, 1, 2, 4, 8초 언저리."""
    import random

    return min(8.0, 0.5 * (2**attempt)) * (0.75 + random.random() * 0.5)


class GoogleCalendarClient:
    """`googleapiclient` 위의 얇은 껍데기.

    HTTP 오류를 도메인 예외로 바꾸고, 재시도 가능한 오류만 지수 백오프한다
    (설계서 §4-[9]). 이 클래스는 상태를 갖지 않는다.
    """

    def __init__(self, credentials: Any, *, max_attempts: int = 5):
        from googleapiclient.discovery import build  # 지연 임포트: 테스트는 Fake만 쓴다

        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self._max_attempts = max_attempts

    # -- 오류 변환

    @staticmethod
    def _translate(exc: Exception) -> CalendarError:
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError

        if isinstance(exc, RefreshError):
            return AuthExpired(str(exc))
        if isinstance(exc, HttpError):
            status = exc.status_code or getattr(exc.resp, "status", None)
            if status == 401:
                return AuthExpired(str(exc))
            if status == 404:
                return EventNotFound(str(exc))
            if status == 409:
                return EventAlreadyExists(str(exc))
            if status == 403:
                reason = str(exc)
                if "rateLimitExceeded" in reason or "userRateLimitExceeded" in reason:
                    return RateLimited(reason)
                return CalendarError(reason)
            if status is not None and 500 <= status < 600:
                return RateLimited(str(exc))
            return CalendarError(str(exc))
        return CalendarError(str(exc))

    def _call(self, request_factory):
        """재시도 가능한 오류만 백오프하며 최대 `max_attempts`회 시도한다."""
        import time

        last: CalendarError | None = None
        for attempt in range(self._max_attempts):
            try:
                return request_factory().execute()
            except Exception as exc:  # noqa: BLE001 — 아래에서 도메인 예외로 바꾼다
                error = self._translate(exc)
                if not isinstance(error, RateLimited) or not _retryable(attempt, self._max_attempts):
                    raise error from exc
                last = error
                time.sleep(_backoff_seconds(attempt))
        raise last or CalendarError("재시도 한도 초과")

    # -- CalendarClient

    def ensure_calendar(self, name: str) -> str:
        """이름이 같은 보조 캘린더를 찾고, 없으면 만든다.

        `calendar.app.created` 스코프에서는 앱이 만든 캘린더만 보이므로
        목록에 남의 캘린더가 섞이지 않는다.
        """
        page_token = None
        while True:
            listing = self._call(
                lambda t=page_token: self._service.calendarList().list(pageToken=t, maxResults=250)
            )
            for entry in listing.get("items", []):
                if entry.get("summary") == name:
                    return entry["id"]
            page_token = listing.get("nextPageToken")
            if not page_token:
                break

        created = self._call(
            lambda: self._service.calendars().insert(
                body={"summary": name, "timeZone": "Asia/Seoul"}
            )
        )
        return created["id"]

    def list_events(self, calendar_id: str, time_min: date, time_max: date) -> list[dict[str, Any]]:
        from datetime import datetime, time as _time, timezone

        def _rfc3339(day: date, end: bool) -> str:
            moment = datetime.combine(day, _time.max if end else _time.min)
            return moment.replace(tzinfo=timezone.utc).isoformat()

        out: list[dict[str, Any]] = []
        page_token = None
        while True:
            page = self._call(
                lambda t=page_token: self._service.events().list(
                    calendarId=calendar_id,
                    timeMin=_rfc3339(time_min, False),
                    timeMax=_rfc3339(time_max, True),
                    privateExtendedProperty=f"app={APP_TAG}",
                    singleEvents=True,
                    maxResults=2500,
                    pageToken=t,
                    showDeleted=False,
                )
            )
            out.extend(page.get("items", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return out

    def insert_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = {**body, "id": event_id}
        return self._call(
            lambda: self._service.events().insert(calendarId=calendar_id, body=payload)
        )

    def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            lambda: self._service.events().patch(
                calendarId=calendar_id, eventId=event_id, body=body
            )
        )

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        return self._call(
            lambda: self._service.events().get(calendarId=calendar_id, eventId=event_id)
        )

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self._call(
            lambda: self._service.events().delete(calendarId=calendar_id, eventId=event_id)
        )
