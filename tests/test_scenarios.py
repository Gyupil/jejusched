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
    """같은 쌍을 다시 만나면 캐시로 해결하고 호출하지 않는다.

    두 번째 0908을 그냥 다시 돌리면 이미 JIBS가 덮어써져 규칙 3 쌍이 **생기지 않아**
    캐시 경로를 타지 않는다. 그래서 캘린더만 비우고 DB(llm_cache)는 남긴 채
    0907→0908을 다시 재생해 같은 쌍을 다시 만들어 낸다.
    """
    h = harness()
    h.apply("0907")
    assert h.apply("0908").llm_calls == 1
    assert h.state.cached_verdicts([]) == {}  # 아래에서 실제 캐시 내용을 확인한다
    cached = h.state.conn.execute("SELECT COUNT(*) AS n FROM llm_cache").fetchone()["n"]
    assert cached == 2, "JIBS 묶음의 쌍 2개가 캐시에 남아야 한다"

    #  캘린더만 새로 — llm_cache는 그대로 둔다
    h.client = FakeCalendarClient()
    new_cal = h.client.ensure_calendar("주요일정(자동)")
    h.state.conn.execute("UPDATE targets SET calendar_id=?", (new_cal,))
    h.state.conn.execute("DELETE FROM events")
    h.pipeline = Pipeline(AppConfig(), h.state, FixtureJsonParser(), h.client, h.resolver)

    h.apply("0907")
    again = h.apply("0908")
    #  규칙 3 묶음이 **다시 생겼는데도** 호출이 없어야 캐시가 일한 것이다
    assert len(h.resolver.seen_groups) == 1, "규칙 3 묶음이 다시 생기지 않았다 — 캐시를 검증하지 못한다"
    assert again.llm_calls == 0, "같은 쌍인데 다시 물었다 — 캐시가 동작하지 않는다"
    assert "* 제46회 전국장애인체육대회 성화 출발식" in h.summaries()


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


def test_sync_runs_are_journalled(harness, tmp_path):
    """[10] Journal — 설정 창 "최근 처리"와 `status` 명령이 이걸 읽는다."""
    import shutil

    from jejusched.watcher.intake import Intake
    from jejusched.watcher.worker import Worker, WorkerDeps

    h = harness()
    source = tmp_path / "0907.json"
    shutil.copy(fixture("0907"), source)
    #  충분히 오래된 파일은 안정화를 기다리지 않는다 — 시작 시 스캔이 느려지지 않게 한 규칙
    import os
    import time

    old_time = time.time() - 600
    os.utime(source, (old_time, old_time))

    deps = WorkerDeps(
        config=AppConfig(), state=h.state, pipeline=h.pipeline,
        intake=Intake(h.state, FixtureJsonParser(), AppConfig()),
    )
    import queue

    Worker(deps, queue.Queue()).process([source])

    runs = h.state.recent_runs()
    assert len(runs) == 1
    assert runs[0]["created"] == 63 and runs[0]["error"] is None
    assert runs[0]["target_id"] == h.targets[0].id


def test_mark_keeps_every_extended_property(harness):
    """MARK가 private 맵을 통째로 보내지 않으면 이벤트가 조회에서 빠져 고아가 된다."""
    h = harness()
    h.apply("0907")
    h.apply("0908")

    marked = [
        e for e in h.client.events[h.calendar_of()].values()
        if e["summary"].startswith("* ")
    ]
    assert marked, "표시된 일정이 있어야 한다"
    props = marked[0]["extendedProperties"]["private"]
    assert props["app"] == "jjsched"          # 이게 없으면 다음 조회에서 사라진다
    assert props["status"] == "marked"
    assert props["logical_id"] and props["key"] and props["section"]

    #  그리고 실제로 다음 Reconcile에서 다시 읽혀야 한다
    from jejusched.gcal.client import to_existing_event

    restored = to_existing_event(marked[0])
    assert restored is not None and restored.is_marked


def test_cache_records_the_model_that_actually_answered(harness):
    """폴백이 일어나면 첫 모델이 아니라 **답한 모델**이 캐시에 남아야 한다."""
    h = harness(resolver=FakeResolver(SAME_TITLES, model="gemini-3.5-flash-lite"))
    h.apply("0907")
    h.apply("0908")

    models = {r["model"] for r in h.state.conn.execute("SELECT model FROM llm_cache")}
    assert models == {"gemini-3.5-flash-lite"}
