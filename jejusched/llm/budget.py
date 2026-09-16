"""일일 호출 예산 — 설계서 §4-[7].

무료 티어 한도(20 RPD)는 태평양시(PT) 자정에 리셋되므로 날짜도 PT로 센다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

#  PT는 DST에 따라 UTC-8/-7. 리셋 시점만 알면 되므로 UTC-8로 고정해도
#  하루 경계가 최대 1시간 어긋날 뿐이고, 그 방향은 항상 보수적(늦게 리셋)이다.
_PT = timezone(timedelta(hours=-8))


def pt_today(now: datetime | None = None) -> str:
    moment = (now or datetime.now(timezone.utc)).astimezone(_PT)
    return moment.date().isoformat()


class UsageStore(Protocol):
    def get_calls(self, day_pt: str) -> int: ...
    def add_call(self, day_pt: str) -> None: ...


class InMemoryUsage:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def get_calls(self, day_pt: str) -> int:
        return self._counts.get(day_pt, 0)

    def add_call(self, day_pt: str) -> None:
        self._counts[day_pt] = self._counts.get(day_pt, 0) + 1


class DailyBudget:
    def __init__(self, store: UsageStore, limit: int):
        self._store = store
        self._limit = limit

    def remaining(self, now: datetime | None = None) -> int:
        return max(0, self._limit - self._store.get_calls(pt_today(now)))

    def allow(self, now: datetime | None = None) -> bool:
        return self.remaining(now) > 0

    def consume(self, now: datetime | None = None) -> None:
        self._store.add_call(pt_today(now))
