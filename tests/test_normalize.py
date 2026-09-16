"""정규화 규칙 — 설계서 §2.2."""

from __future__ import annotations

import re
from datetime import date, time

import pytest

from jejusched.core.models import ParsedFile, ScheduleItem
from jejusched.core.normalize import (
    hash_text,
    normalize,
    parse_file_date,
    parse_time_cell,
    resolve_event_date,
    split_title_and_note,
    title_norm,
    to_canonical,
)
from jejusched.core.planner import event_id_from_logical, new_logical_id


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('(토론)jibs"제주는 안전한가?"', "(토론)jibs제주는안전한가?"),
        ("(방송대담) JIBS<시사이슈 결>", "(방송대담)jibs시사이슈결"),
        ("* 제46회 전국장애인체육대회 성화 출발식", "제46회전국장애인체육대회성화출발식"),
        ("「제주」 『회의』", "제주회의"),
    ],
)
def test_title_norm(raw, expected):
    assert title_norm(raw) == expected


def test_title_norm_ignores_mark_prefix():
    """캘린더 쪽 `* ` 접두는 비교에서 무시된다 — 그래야 규칙 1로 복구된다."""
    assert title_norm("* 일일전략회의") == title_norm("일일전략회의")


@pytest.mark.parametrize(
    "cell, expected",
    [
        ("10:00", (False, time(10, 0), time(11, 0))),
        ("10:00~17:00", (False, time(10, 0), time(17, 0))),
        ("종일", (True, None, None)),
        ("08:30", (False, time(8, 30), time(9, 30))),
        ("", (True, None, None)),
    ],
)
def test_parse_time_cell(cell, expected):
    assert parse_time_cell(cell) == expected


def test_default_duration_is_60_minutes():
    """종료 시각이 없는 일정은 60분(설계서 §11-3)."""
    _, start, end = parse_time_cell("23:30")
    assert (start, end) == (time(23, 30), time(0, 30)) or end == time(23, 59)


def test_file_date_uses_weekday_to_fix_year():
    """헤더 요일과 맞지 않으면 ±1년 중 맞는 해로 보정한다."""
    parsed = ParsedFile(file_date_month=9, file_date_day=7, file_weekday="월", items=[])
    assert parse_file_date(parsed, 2026) == date(2026, 9, 7)  # 2026-09-07은 월요일
    #  기준 연도가 틀려도 요일로 되찾는다
    assert parse_file_date(parsed, 2027) == date(2026, 9, 7)


def test_event_year_picks_nearest_to_file_date():
    """연말·연초에 걸친 일정의 연도를 파일 날짜와의 거리로 고른다."""
    assert resolve_event_date("1/3(금)", date(2025, 12, 30)) == date(2026, 1, 3)
    assert resolve_event_date("12/30(화)", date(2026, 1, 3)) == date(2025, 12, 30)
    assert resolve_event_date(None, date(2026, 9, 7)) == date(2026, 9, 7)


def test_note_keeps_extra_line_and_allday_time():
    title, note = split_title_and_note("제46회 개회식\n*도 본청 셔틀 버스(17:00~)")
    assert (title, note) == ("제46회 개회식", "*도 본청 셔틀 버스(17:00~)")

    item = ScheduleItem("실 일정", None, "종일", "(14:00)2026년 을지연습 사후강평 참석",
                        "정부서울청사", "", "사회재난과", 0)
    event = to_canonical(item, date(2026, 9, 18))
    assert event.all_day and event.note == "(14:00)"
    assert event.title == "2026년 을지연습 사후강평 참석"


def test_content_hash_ignores_line_wrap_only_changes():
    """표 칸 너비가 달라져 줄바꿈 위치만 바뀐 것은 내용 변경이 아니다."""
    a = ScheduleItem("실 일정", None, "18:30", "천마천 하천기본계획 주민 설명회", "송당리", "", "자연재난과", 0)
    b = ScheduleItem("실 일정", None, "18:30", "천마천 하천기본계획 주민 설명\n회", "송당리", "", "자연재난과", 1)
    assert to_canonical(a, date(2026, 9, 10)).content_hash == to_canonical(b, date(2026, 9, 10)).content_hash
    assert hash_text("가 나\n다") == "가나다"


def test_content_hash_still_catches_real_changes():
    a = ScheduleItem("실 일정", None, "15:00", "재해예방사업 회의", "상황실", "안전건강실장", "자연재난과", 0)
    b = ScheduleItem("실 일정", None, "15:00", "재해예방사업 회의", "상황실", "실장", "자연재난과", 0)
    assert to_canonical(a, date(2026, 9, 17)).content_hash != to_canonical(b, date(2026, 9, 17)).content_hash


def test_duplicate_rows_in_one_file_are_dropped_with_warning():
    row = dict(section="실 일정", date_text=None, time_text="10:00", title="정례브리핑",
               location="프레스센터", attendees="실장", department="안전정책과")
    parsed = ParsedFile(9, 7, "월", [ScheduleItem(**row, row_index=0), ScheduleItem(**row, row_index=1)])
    _, events, _, warnings = normalize(parsed, 2026)
    assert len(events) == 1
    assert any("중복" in w for w in warnings)


def test_judged_excludes_past_dates():
    """과거 날짜는 판단 대상이 아니다 — 절대 `*` 표시하지 않는다(설계서 §4-[4])."""
    items = [
        ScheduleItem("실 일정", "9/5(토)", "10:00", "과거 일정", "", "", "", 0),
        ScheduleItem("실 일정", None, "10:00", "오늘 일정", "", "", "", 1),
    ]
    file_date, events, judged, _ = normalize(ParsedFile(9, 7, "월", items), 2026)
    assert file_date == date(2026, 9, 7)
    assert {d for d, _ in judged} == {date(2026, 9, 7)}
    assert date(2026, 9, 5) in {e.date for e in events}  # 추가·덮어쓰기는 된다


def test_deterministic_event_id_is_valid_for_calendar_api():
    """base32hex 소문자 26자 — Calendar API의 id 규칙을 만족한다."""
    for _ in range(50):
        logical = new_logical_id()
        event_id = event_id_from_logical(logical)
        assert re.fullmatch(r"[0-9a-v]{26}", event_id)
        assert event_id_from_logical(logical) == event_id  # 결정적
