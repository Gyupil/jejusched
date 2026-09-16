"""Google 이벤트 본문 매핑 — 설계서 §2.3, §4-[8]."""

from __future__ import annotations

from datetime import date

from jejusched.config import AppConfig
from jejusched.core import matcher as M
from jejusched.core.models import Op, ScheduleItem, Section
from jejusched.core.normalize import to_canonical
from jejusched.core.planner import PlanContext, Planner

from test_matcher import DEPT, exi, finish, inc

DAY = date(2026, 9, 10)
CTX = PlanContext(file_date=DAY, source_name="0910_주요일정.hwpx")


def plan_of(result):
    return Planner(AppConfig()).plan(result, CTX)


def test_create_body_has_all_required_fields():
    event = inc("10:00", "정례브리핑", location="프레스센터", attendees="실장", department="안전정책과")
    op = plan_of(finish(M.match_rules_1_2([event], [])))[0]

    assert op.op is Op.CREATE
    body = op.body
    assert body["summary"] == "정례브리핑"
    assert body["start"] == {"dateTime": "2026-09-10T10:00:00", "timeZone": "Asia/Seoul"}
    assert body["end"]["dateTime"] == "2026-09-10T11:00:00"
    assert body["location"] == "프레스센터"
    assert body["colorId"] == "9"  # 실 일정 = 블루베리
    assert body["transparency"] == "opaque"  # 실 일정은 내 시간을 점유한다
    assert body["reminders"] == {"useDefault": False, "overrides": []}  # 알림 없음

    props = body["extendedProperties"]["private"]
    assert props["app"] == "jjsched"
    assert props["status"] == "active"
    assert props["key"] == event.event_key and props["chash"] == event.content_hash
    assert props["src"] == props["seen"] == "2026-09-10"
    assert "참석: 실장" in body["description"]
    assert "구분: 실 일정" in body["description"]
    assert "출처: 0910_주요일정.hwpx (2026-09-10)" in body["description"]


def test_governor_sections_are_transparent_and_colored():
    event = to_canonical(ScheduleItem("도지사", None, "10:00", "도정질문", "", "", "", 0), DAY)
    op = plan_of(finish(M.match_rules_1_2([event], []), judged={(DAY, Section.GOV)}))[0]
    assert op.body["transparency"] == "transparent"
    assert op.body["colorId"] == "11"  # 도지사 = 토마토


def test_all_day_event_uses_date_and_next_day():
    event = inc("종일", "지역필수공공의료 회의")
    body = plan_of(finish(M.match_rules_1_2([event], [])))[0].body
    assert body["start"] == {"date": "2026-09-10"}
    assert body["end"] == {"date": "2026-09-11"}


def test_unchanged_event_is_skipped():
    event = inc("10:00", "정례브리핑", location="프레스센터")
    existing = exi("10:00", "정례브리핑", location="프레스센터")
    ops = plan_of(finish(M.match_rules_1_2([event], [existing])))
    assert [o.op for o in ops] == [Op.SKIP]


def test_mark_keeps_description_body_and_adds_notice():
    existing = exi("17:30", "임명장 수여식")
    existing.description = "참석: 실장\n구분: 실 일정\n출처: 0909_주요일정.hwpx (2026-09-09)"
    op = plan_of(finish(M.match_rules_1_2([], [existing])))[0]

    assert op.op is Op.MARK
    assert op.body["summary"] == "* 임명장 수여식"
    assert op.body["colorId"] == "8"  # 그래파이트
    assert op.body["extendedProperties"]["private"]["status"] == "marked"
    lines = op.body["description"].split("\n")
    assert lines[0] == "※ 2026-09-10 파일부터 목록에서 빠짐"
    assert "참석: 실장" in lines  # 본문은 유지된다 (§11-5)


def test_mark_notice_is_not_duplicated_on_remark():
    existing = exi("17:30", "임명장 수여식")
    existing.description = "※ 2026-09-09 파일부터 목록에서 빠짐\n참석: 실장"
    body = Planner(AppConfig()).build_mark_body(existing, CTX)
    assert body["description"].count("※") == 1
    assert body["description"].startswith("※ 2026-09-10")


def test_restore_clears_prefix_and_color():
    event = inc("09:40", "성화 출발식")
    existing = exi("09:40", "성화 출발식", marked=True)
    op = plan_of(finish(M.match_rules_1_2([event], [existing])))[0]

    assert op.op is Op.RESTORE
    assert op.body["summary"] == "성화 출발식"
    assert op.body["colorId"] == "9"
    assert op.body["extendedProperties"]["private"]["status"] == "active"


def test_mark_color_change_can_be_disabled():
    config = AppConfig(mark_change_color=False)
    op = Planner(config).plan(finish(M.match_rules_1_2([], [exi("17:30", "빠진 일정")])), CTX)[0]
    assert "colorId" not in op.body  # 원래 색을 유지한다


def test_hold_produces_no_body():
    new, old = inc("10:00", "새것"), exi("09:00", "옛것")
    ops = plan_of(finish(M.match_rules_1_2([new], [old]), failed={DEPT}))
    assert [o.op for o in ops] == [Op.HOLD]
    assert ops[0].body is None
