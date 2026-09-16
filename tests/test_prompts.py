"""LLM 요청 구성과 응답 검증 — 설계서 §4-[7], §11-9.

실제 Gemini 호출은 이 파일에서 하지 않는다. 보내는 내용과 받아들이는 내용의
규칙만 고정한다.
"""

from __future__ import annotations

import json

from jejusched.core import matcher as M
from jejusched.llm.prompts import (
    RESPONSE_SCHEMA,
    build_payload,
    parse_response,
)

from test_matcher import exi, inc


def make_groups():
    new = inc("10:00", "(방송대담) JIBS<시사이슈 결>", location="JIBS", attendees="실장",
              department="안전정책과")
    old_same = exi("10:00", '(토론)jibs"제주는 안전한가?"', location="JIBS", attendees="실장",
                   department="안전정책과")
    old_other = exi("09:40", "성화 출발식", location="제주도청 앞", attendees="실장",
                    department="전국체전기획과")
    partial = M.match_rules_1_2([new], [old_same, old_other])
    return partial.pending, new, old_same, old_other


def test_payload_carries_only_allowed_fields():
    groups, *_ = make_groups()
    payload = build_payload(groups)
    row = payload.body["묶음"][0]["새_파일_일정"][0]

    assert set(row) == {"id", "시간", "행사명", "장소", "참석", "주관부서"}
    assert payload.body["묶음"][0]["날짜"] == "2026-09-10"
    assert payload.body["묶음"][0]["구분"] == "실 일정"


def test_payload_can_omit_attendees_and_location():
    """기관 정책상 참석·장소 전송을 끌 수 있어야 한다(§11-9)."""
    groups, *_ = make_groups()
    payload = build_payload(groups, send_attendees=False, send_location=False)
    row = payload.body["묶음"][0]["새_파일_일정"][0]

    assert "참석" not in row and "장소" not in row
    assert "행사명" in row and "시간" in row
    assert "실장" not in json.dumps(payload.body, ensure_ascii=False)


def test_payload_flags_marked_existing_events():
    new = inc("10:00", "새 일정")
    old = exi("09:00", "표시된 옛 일정", marked=True)
    payload = build_payload(M.match_rules_1_2([new], [old]).pending)
    assert payload.body["묶음"][0]["기존_일정"][0]["표시됨"] is True


def test_parse_response_maps_pairs_back():
    groups, new, old_same, _ = make_groups()
    payload = build_payload(groups)
    response = {"groups": [{"group_id": "g0", "pairs": [{"new_id": "g0n0", "old_id": "g0e0"}]}]}

    assert parse_response(response, payload) == {M.pair_key(new, old_same)}


def test_parse_response_drops_unknown_ids():
    groups, *_ = make_groups()
    payload = build_payload(groups)
    response = {
        "groups": [
            {"group_id": "g0", "pairs": [{"new_id": "없음", "old_id": "g0e0"}]},
            {"group_id": "없는묶음", "pairs": [{"new_id": "g0n0", "old_id": "g0e0"}]},
        ]
    }
    assert parse_response(response, payload) == set()


def test_parse_response_enforces_one_to_one():
    """같은 id를 두 번 쓴 응답은 첫 짝만 살린다."""
    groups, new, old_same, _ = make_groups()
    payload = build_payload(groups)
    response = {
        "groups": [
            {
                "group_id": "g0",
                "pairs": [
                    {"new_id": "g0n0", "old_id": "g0e0"},
                    {"new_id": "g0n0", "old_id": "g0e1"},
                ],
            }
        ]
    }
    assert parse_response(response, payload) == {M.pair_key(new, old_same)}


def test_parse_response_tolerates_garbage():
    groups, *_ = make_groups()
    payload = build_payload(groups)
    for junk in ({}, {"groups": None}, {"groups": [{"group_id": "g0", "pairs": None}]}):
        assert parse_response(junk, payload) == set()


def test_response_schema_is_well_formed():
    assert RESPONSE_SCHEMA["type"] == "object"
    pairs = RESPONSE_SCHEMA["properties"]["groups"]["items"]["properties"]["pairs"]
    assert pairs["items"]["required"] == ["new_id", "old_id"]
