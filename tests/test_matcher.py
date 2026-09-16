"""매칭 규칙 1~5 — 설계서 §4-[6]."""

from __future__ import annotations

from datetime import date

import pytest

from jejusched.core import matcher as M
from jejusched.core.models import EventStatus, ExistingEvent, ScheduleItem, Section
from jejusched.core.normalize import title_norm, to_canonical

DAY = date(2026, 9, 10)
DEPT = (DAY, Section.DEPT)


def inc(time_text: str, title: str, *, location="", attendees="", department="", idx=0):
    return to_canonical(
        ScheduleItem("실 일정", None, time_text, title, location, attendees, department, idx), DAY
    )


def exi(time_text: str, title: str, *, marked=False, location="", attendees="", department=""):
    """같은 정규화를 거쳐 만든 '캘린더에 이미 있는' 일정."""
    canonical = inc(time_text, title, location=location, attendees=attendees, department=department)
    return ExistingEvent(
        logical_id=f"lid-{title}",
        google_event_id=f"gid-{title}",
        event_key=canonical.event_key,
        content_hash=canonical.content_hash,
        date=canonical.date,
        section=canonical.section,
        slot=canonical.slot,
        title=canonical.title,
        title_norm=canonical.title_norm,
        status=EventStatus.MARKED if marked else EventStatus.ACTIVE,
        location=canonical.location,
        attendees=canonical.attendees,
        department=canonical.department,
    )


def finish(partial, *, same=(), failed=(), judged=(DEPT,), deleted=()):
    return M.finish(
        partial,
        same_pairs=set(same),
        failed_groups=set(failed),
        judged=set(judged),
        user_deleted=set(deleted),
    )


def test_rule1_same_time_and_title():
    partial = M.match_rules_1_2([inc("10:00", "정례브리핑")], [exi("10:00", "정례브리핑")])
    assert [m.rule for m in partial.matches] == [1]
    assert not partial.pending


def test_rule1_ignores_spacing_and_quotes():
    partial = M.match_rules_1_2(
        [inc("10:00", '(토론) JIBS "제주는 안전한가?"')],
        [exi("10:00", '(토론)jibs"제주는 안전한가?"')],
    )
    assert [m.rule for m in partial.matches] == [1]


def test_rule2_same_title_changed_time():
    """행사명이 같고 양쪽에 하나씩이면 시간이 바뀐 같은 일정이다."""
    partial = M.match_rules_1_2(
        [inc("10:00", "행안부 주관 추석 연휴 안전관리대책 점검회의")],
        [exi("14:00", "행안부 주관 추석 연휴 안전관리대책 점검회의")],
    )
    assert [m.rule for m in partial.matches] == [2]


def test_rule2_does_not_fire_when_ambiguous():
    """같은 행사명이 여러 건이면 1:1이 아니므로 규칙 2를 쓰지 않는다."""
    partial = M.match_rules_1_2(
        [inc("10:00", "과장 회의", idx=0), inc("11:00", "과장 회의", idx=1)],
        [exi("14:00", "과장 회의")],
    )
    assert not partial.matches
    assert len(partial.pending) == 1


def test_rule3_pairs_go_to_llm_only_when_both_sides_remain():
    only_incoming = M.match_rules_1_2([inc("10:00", "새 일정")], [])
    assert not only_incoming.pending and only_incoming.leftover_incoming

    both = M.match_rules_1_2([inc("10:00", "새 일정")], [exi("09:40", "옛 일정")])
    assert len(both.pending) == 1
    assert both.pending[0].pair_keys()


def test_rule3_same_verdict_overwrites():
    new, old = inc("10:00", "(방송대담) JIBS<시사이슈 결>"), exi("10:00", '(토론)jibs"제주는 안전한가?"')
    partial = M.match_rules_1_2([new], [old])
    result = finish(partial, same={M.pair_key(new, old)})
    assert [m.rule for m in result.matches] == [3]
    assert not result.creates and not result.marks


def test_rule3_different_verdict_adds_and_marks():
    new, old = inc("08:50", "임명장 수여식"), exi("09:00", "현안업무 점검회의")
    result = finish(M.match_rules_1_2([new], [old]))
    assert [e.title for e in result.creates] == ["임명장 수여식"]
    assert [e.title for e in result.marks] == ["현안업무 점검회의"]


def test_rule3_enforces_one_to_one():
    """LLM이 한 일정을 두 번 짝지어도 먼저 나온 짝만 살린다."""
    new = inc("10:00", "새 회의")
    old_a, old_b = exi("09:00", "옛 회의 A"), exi("11:00", "옛 회의 B")
    partial = M.match_rules_1_2([new], [old_a, old_b])
    result = finish(partial, same={M.pair_key(new, old_a), M.pair_key(new, old_b)})
    assert len(result.matches) == 1
    assert len(result.marks) == 1


def test_rule4_skips_user_deleted():
    """사용자가 직접 지운 일정은 다시 만들지 않는다(설계서 §4-[5])."""
    new = inc("10:00", "다시 만들지 말 것")
    result = finish(M.match_rules_1_2([new], []), deleted={new.event_key})
    assert not result.creates
    assert [e.title for e in result.suppressed] == ["다시 만들지 말 것"]


def test_rule5_only_marks_inside_judged():
    """새 파일이 그 날짜·구분에 아무 것도 안 적었으면 손대지 않는다."""
    partial = M.match_rules_1_2([], [exi("10:00", "남은 일정")])
    assert not finish(partial, judged=()).marks
    assert finish(partial, judged=(DEPT,)).marks


def test_rule5_leaves_already_marked_alone():
    partial = M.match_rules_1_2([], [exi("10:00", "이미 표시됨", marked=True)])
    assert not finish(partial).marks


def test_marked_event_is_matchable_and_restored():
    """`*` 표시된 일정도 짝짓기 대상이다 — 다시 실리면 복구된다."""
    partial = M.match_rules_1_2([inc("09:40", "성화 출발식")], [exi("09:40", "성화 출발식", marked=True)])
    result = finish(partial)
    assert len(result.matches) == 1 and result.matches[0].existing.is_marked


def test_different_section_or_date_never_matches():
    """9/7 정례회가 도지사·실 일정 양쪽에 있는 것은 정상이다."""
    gov = to_canonical(ScheduleItem("도지사", None, "10:00", "제454회 정례회", "", "", "", 0), DAY)
    dept = exi("10:00", "제454회 정례회")
    partial = M.match_rules_1_2([gov], [dept])
    assert not partial.matches and not partial.pending


def test_hold_blocks_rules_4_and_5_for_that_group_only():
    """판정 실패한 (날짜, 구분)만 멈추고 다른 날짜는 정상 적용한다."""
    other_day = date(2026, 9, 11)
    new_here, old_here = inc("10:00", "여기 새것"), exi("09:00", "여기 옛것")
    new_there = to_canonical(
        ScheduleItem("실 일정", "9/11(금)", "10:00", "저기 새것", "", "", "", 9), DAY
    )
    partial = M.match_rules_1_2([new_here, new_there], [old_here])
    result = finish(partial, failed={DEPT}, judged={DEPT, (other_day, Section.DEPT)})

    assert result.held == {DEPT}
    assert [e.title for e in result.creates] == ["저기 새것"]  # 다른 날짜는 진행
    assert not result.marks  # HOLD된 날짜는 표시하지 않는다
