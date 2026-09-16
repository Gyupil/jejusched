"""설계서 §8 — 0907→0910 순차 적용 시나리오.

이 파일의 숫자는 **설계 기준선**이다. matcher/normalize를 고쳐서 이 값이
바뀌면 코드가 아니라 이해가 틀린 것이다(CLAUDE.md 참고).
"""

from __future__ import annotations

from datetime import date

import pytest

from jejusched.core.models import Op
from jejusched.llm.resolver import FakeResolver

from conftest import requires_fixtures, SAME_TITLES

pytestmark = requires_fixtures

#  (파일, 추가, 덮어쓰기, 표시, LLM 호출)
EXPECTED = [
    ("0907", 63, 0, 0, 0),
    ("0908", 23, 1, 1, 1),
    ("0909", 19, 1, 0, 0),
    ("0910", 13, 2, 3, 1),
]


def test_sequence_matches_design_table(harness):
    h = harness()
    for name, created, overwritten, marked, llm_calls in EXPECTED:
        result = h.apply(name)
        totals = result.totals()
        assert result.outcomes[0].error is None
        #  "덮어쓰기"는 UPDATE + RESTORE 둘 다를 뜻한다
        assert (
            totals["created"],
            totals["updated"] + totals["restored"],
            totals["marked"],
            result.llm_calls,
        ) == (created, overwritten, marked, llm_calls), f"{name} 시나리오 불일치: {totals}"


def test_0908_llm_pair_is_the_jibs_case(harness):
    """0908의 LLM 호출은 9/10 실 일정의 JIBS 쌍 하나여야 한다."""
    h = harness()
    h.apply("0907")
    h.apply("0908")

    groups = h.resolver.seen_groups
    assert len(groups) == 1, [g.key for g in groups]
    group = groups[0]
    assert (group.date, group.section.value) == (date(2026, 9, 10), "실 일정")
    assert [e.title for e in group.incoming] == ["(방송대담) JIBS<시사이슈 결>"]
    assert sorted(e.title for e in group.existing) == [
        "(토론)jibs\"제주는 안전한가?\"",
        "제46회 전국장애인체육대회 성화 출발식",
    ]
    #  성화 출발식은 짝이 없어 `* ` 표시로 남는다 — 지워지지 않는다
    assert "* 제46회 전국장애인체육대회 성화 출발식" in h.summaries()
    assert "(방송대담) JIBS<시사이슈 결>" in h.summaries()


def test_0910_llm_pair_is_judged_different(harness):
    """0910의 9/11 쌍은 '다름' → 임명장 추가 + 현안업무 표시."""
    h = harness()
    for name in ("0907", "0908", "0909", "0910"):
        h.apply(name)

    summaries = h.summaries()
    assert "신임 제주의료원장 임명장 수여식" in summaries
    assert "* 현안업무 점검회의(행정부지사주재)" in summaries


def test_marked_event_is_restored_when_it_returns(harness):
    """`*` 표시된 일정이 같은 행사명·시각으로 다시 실리면 규칙 1로 복구된다(§8 가정: 0911)."""
    h = harness()
    h.apply("0907")
    h.apply("0908")
    assert "* 제46회 전국장애인체육대회 성화 출발식" in h.summaries()

    #  0907을 force로 다시 적용하면 성화 출발식이 다시 실린다
    result = h.pipeline.run(
        __import__("pathlib").Path("tests/fixtures/0907.json"),
        [h.state.get_target("dev@example.com")],
        reference_year=2026,
        source_name="0907_주요일정.hwpx",
    )
    assert result.totals()["restored"] >= 1
    assert "제46회 전국장애인체육대회 성화 출발식" in h.summaries()


def test_llm_disabled_treats_rule3_as_different(harness):
    """키가 없으면 규칙 3은 항상 '다름' — 추가 + 표시 (설계서 §11-8)."""
    from jejusched.config import AppConfig

    h = harness(config=AppConfig().with_llm_disabled())
    h.apply("0907")
    result = h.apply("0908")

    assert result.llm_calls == 0
    #  JIBS가 같은 일정으로 묶이지 않으므로 추가 1건·표시 1건이 늘어난다
    assert result.totals()["created"] == 24
    assert result.totals()["marked"] == 2


def test_llm_failure_holds_only_that_group(harness):
    """LLM이 실패하면 그 (날짜, 구분)만 HOLD하고 나머지는 정상 적용한다(§4-[6])."""
    h = harness(resolver=FakeResolver(SAME_TITLES, fail=True))
    h.apply("0907")
    result = h.apply("0908")

    held_ops = [op for o in result.outcomes for op in o.ops if op.op is Op.HOLD]
    assert [(op.date, op.section.value) for op in held_ops] == [(date(2026, 9, 10), "실 일정")]
    #  9/10 실 일정은 손대지 않았지만 다른 날짜는 정상 추가되었다
    assert result.totals()["created"] == 23 - 0
    assert "* 제46회 전국장애인체육대회 성화 출발식" not in h.summaries()
