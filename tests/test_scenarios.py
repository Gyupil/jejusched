"""실패 모드와 복구 — 설계서 §9."""

from __future__ import annotations

from pathlib import Path

import pytest

from jejusched.config import AppConfig
from jejusched.core.models import Op
from jejusched.core.pipeline import Pipeline
from jejusched.core.state import State
from jejusched.gcal.client import AuthExpired, EventAlreadyExists
from jejusched.gcal.fake import FakeCalendarClient
from jejusched.llm.resolver import FakeResolver
from jejusched.parsers.fixture_json import FixtureJsonParser

from conftest import requires_fixtures, SAME_TITLES, fixture

pytestmark = requires_fixtures


def test_local_db_loss_is_recovered_from_calendar(harness):
    """로컬 DB가 사라져도 extendedProperties로 전량 복원된다(§9)."""
    h = harness()
    h.apply("0907")
    before = h.summaries()

    #  DB만 날리고 같은 캘린더로 다시 시작한다
    fresh = State()
    target = fresh.upsert_target("dev@example.com", h.calendar_of(), "주요일정(자동)")
    pipeline = Pipeline(AppConfig(), fresh, FixtureJsonParser(), h.client, FakeResolver(SAME_TITLES))
    result = pipeline.run(fixture("0907"), [target], reference_year=2026)

    #  전부 기존 일정으로 인식되어 아무것도 새로 만들지 않는다
    assert result.totals()["created"] == 0
    assert result.totals()["skipped"] == 63
    assert h.summaries() == before


def test_crash_midway_then_rerun_creates_no_duplicates(harness):
    """적용 도중 죽어도 결정적 id 덕분에 중복이 생기지 않는다(§9)."""
    h = harness()

    class Flaky(FakeCalendarClient):
        def __init__(self, inner, limit):
            self.__dict__ = inner.__dict__
            self.limit = limit
            self.inserted = 0

        def insert_event(self, calendar_id, event_id, body):
            if self.inserted >= self.limit:
                raise RuntimeError("네트워크 끊김")
            self.inserted += 1
            return super().insert_event(calendar_id, event_id, body)

    flaky = Flaky(h.client, limit=20)
    pipeline = Pipeline(AppConfig(), h.state, FixtureJsonParser(), flaky, FakeResolver(SAME_TITLES))
    with pytest.raises(RuntimeError):
        pipeline.run(fixture("0907"), [h.state.get_target("dev@example.com")], reference_year=2026)
    assert len(h.client.events[h.calendar_of()]) == 20

    #  재실행: Reconcile이 캘린더 기준으로 정합성을 회복하고 나머지만 만든다
    result = h.apply("0907")
    assert len(h.client.events[h.calendar_of()]) == 63
    assert result.totals()["created"] == 43 and result.totals()["skipped"] == 20


def test_create_conflict_409_is_treated_as_success(harness):
    """같은 결정적 id로 다시 만들면 409 — 성공으로 본다."""
    h = harness()

    class AlwaysConflicts(FakeCalendarClient):
        def __init__(self, inner):
            self.__dict__ = inner.__dict__

        def insert_event(self, calendar_id, event_id, body):
            raise EventAlreadyExists(event_id)

    pipeline = Pipeline(AppConfig(), h.state, FixtureJsonParser(),
                        AlwaysConflicts(h.client), FakeResolver(SAME_TITLES))
    result = pipeline.run(fixture("0907"), [h.state.get_target("dev@example.com")], reference_year=2026)
    assert result.totals()["created"] == 63  # 예외가 새어 나오지 않는다


#  "정례브리핑"처럼 여러 날짜에 반복되는 행사명은 삭제 추적에 쓸 수 없다.
#  캘린더 안에서 딱 한 번만 나오는 행사명을 골라 쓴다.
VICTIM = "제4차 보건복지 중앙-지방 협력회의"


def _victim_id(h) -> str:
    matches = [
        gid for gid, e in h.client.events[h.calendar_of()].items() if e["summary"] == VICTIM
    ]
    assert len(matches) == 1, f"{VICTIM}은 캘린더에 1건이어야 한다: {len(matches)}건"
    return matches[0]


def test_user_deleted_event_is_not_recreated(harness):
    """사용자가 캘린더에서 직접 지운 일정은 되살리지 않는다(§4-[5])."""
    h = harness()
    h.apply("0907")
    h.client.user_deletes(h.calendar_of(), _victim_id(h))

    result = h.apply("0907")  # 같은 파일을 다시 적용해도
    assert VICTIM not in h.summaries()
    assert result.totals()["suppressed"] == 1
    assert h.state.user_deleted_keys(h.targets[0].id)


def test_force_can_restore_user_deleted(harness):
    """force + '직접 삭제한 일정도 복구' 옵션."""
    h = harness()
    h.apply("0907")
    h.client.user_deletes(h.calendar_of(), _victim_id(h))
    h.apply("0907")
    assert VICTIM not in h.summaries()

    h.state.clear_user_deleted(h.targets[0].id)
    h.apply("0907")
    assert VICTIM in h.summaries()


def test_reauth_skips_one_target_and_continues_others(harness):
    """토큰이 죽은 대상만 건너뛰고 다른 대상은 계속 진행한다(§9)."""
    h = harness(emails=["a@example.com", "b@example.com"])
    first_cal = h.targets[0].calendar_id

    class OneTargetExpired(FakeCalendarClient):
        def __init__(self, inner):
            self.__dict__ = inner.__dict__

        def list_events(self, calendar_id, time_min, time_max):
            if calendar_id == first_cal:
                raise AuthExpired(calendar_id)
            return super().list_events(calendar_id, time_min, time_max)

    pipeline = Pipeline(AppConfig(), h.state, FixtureJsonParser(),
                        OneTargetExpired(h.client), FakeResolver(SAME_TITLES))
    result = pipeline.run(fixture("0907"), h.targets, reference_year=2026)

    assert result.outcomes[0].error == "needs_reauth"
    assert h.state.get_target("a@example.com").needs_reauth
    assert result.totals()["created"] == 63  # 두 번째 대상은 정상 적용
    assert len(h.client.events[h.targets[1].calendar_id]) == 63


def test_multiple_targets_mirror_and_share_one_llm_call(harness):
    """대상이 여럿이어도 LLM은 파일당 1회다(§4-[7])."""
    h = harness(emails=["a@example.com", "b@example.com", "c@example.com"])
    h.apply("0907")
    result = h.apply("0908")

    assert result.llm_calls == 1
    assert len({len(h.client.events[t.calendar_id]) for t in h.targets}) == 1  # 동일 미러
    assert h.summaries(0) == h.summaries(1) == h.summaries(2)


def test_llm_cache_prevents_a_second_call(harness):
    """같은 쌍을 다시 만나면 캐시로 해결하고 호출하지 않는다."""
    h = harness()
    h.apply("0907")
    assert h.apply("0908").llm_calls == 1

    #  같은 상태에서 0908을 다시 적용하면 규칙 3 쌍이 캐시에 있다
    fresh_state_calls = h.apply("0908").llm_calls
    assert fresh_state_calls == 0


def test_daily_budget_holds_instead_of_calling(harness):
    """일일 예산을 넘기면 호출하지 않고 HOLD 한다(§4-[7])."""
    config = AppConfig()
    config.llm.daily_budget = 0
    h = harness(config=config)
    h.apply("0907")
    result = h.apply("0908")

    assert result.llm_calls == 0
    assert result.any_held
    assert [op.op for op in result.outcomes[0].ops if op.op is Op.HOLD]


def test_dry_run_touches_nothing(harness):
    h = harness(config=AppConfig(dry_run=True))
    result = h.apply("0907")

    assert result.totals()["created"] == 63  # 계획은 만들어진다
    assert h.client.events[h.calendar_of()] == {}  # 캘린더는 그대로
    assert h.client.calls == []
