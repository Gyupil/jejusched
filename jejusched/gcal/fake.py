"""인메모리 캘린더 — 테스트용. 실제 API의 멱등성·오류를 흉내 낸다."""

from __future__ import annotations

import copy
import itertools
from datetime import date
from typing import Any

from .client import APP_TAG, EventAlreadyExists, EventNotFound


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Calendar의 patch 의미: dict는 병합, 나머지는 치환."""
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class FakeCalendarClient:
    def __init__(self) -> None:
        self.calendars: dict[str, str] = {}  # calendar_id → name
        self.events: dict[str, dict[str, dict[str, Any]]] = {}  # calendar_id → id → event
        self._ids = itertools.count(1)
        self.calls: list[tuple[str, str]] = []  # (동작, 이벤트 id) — 호출 검증용

    def ensure_calendar(self, name: str) -> str:
        for cal_id, cal_name in self.calendars.items():
            if cal_name == name:
                return cal_id
        cal_id = f"cal{next(self._ids)}@group.calendar.google.com"
        self.calendars[cal_id] = name
        self.events[cal_id] = {}
        return cal_id

    def delete_calendar(self, calendar_id: str) -> None:
        self.calls.append(("delete_calendar", calendar_id))
        self.calendars.pop(calendar_id, None)
        self.events.pop(calendar_id, None)

    def list_events(self, calendar_id: str, time_min: date, time_max: date) -> list[dict[str, Any]]:
        out = []
        for raw in self.events.get(calendar_id, {}).values():
            props = (raw.get("extendedProperties") or {}).get("private") or {}
            if props.get("app") != APP_TAG:
                continue
            start = raw["start"].get("date") or raw["start"]["dateTime"][:10]
            when = date.fromisoformat(start)
            if time_min <= when <= time_max:
                out.append(copy.deepcopy(raw))
        return sorted(out, key=lambda e: e["id"])

    def insert_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        store = self.events.setdefault(calendar_id, {})
        if event_id in store:
            raise EventAlreadyExists(event_id)
        self.calls.append(("insert", event_id))
        store[event_id] = {**copy.deepcopy(body), "id": event_id, "status": "confirmed"}
        return copy.deepcopy(store[event_id])

    def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        store = self.events.setdefault(calendar_id, {})
        if event_id not in store:
            raise EventNotFound(event_id)
        self.calls.append(("patch", event_id))
        store[event_id] = _deep_merge(store[event_id], body)
        return copy.deepcopy(store[event_id])

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        store = self.events.setdefault(calendar_id, {})
        if event_id not in store:
            raise EventNotFound(event_id)
        return copy.deepcopy(store[event_id])

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        store = self.events.setdefault(calendar_id, {})
        if event_id not in store:
            raise EventNotFound(event_id)
        self.calls.append(("delete", event_id))
        del store[event_id]

    # -- 테스트 편의

    def user_deletes(self, calendar_id: str, event_id: str) -> None:
        """사용자가 캘린더 앱에서 직접 지운 상황."""
        self.events.get(calendar_id, {}).pop(event_id, None)
