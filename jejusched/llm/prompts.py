"""규칙 3 판정 프롬프트와 응답 검증 — 설계서 §4-[7].

묶음마다 "새 파일 일정"과 "기존 일정"을 주고 **1:1로 짝지으라**고 시킨다.
짝지어지지 않은 것은 자동으로 "다른 일정"이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.matcher import UnresolvedGroup, pair_key

SYSTEM_INSTRUCTION = """\
당신은 제주도청의 「주요일정」 문서를 날마다 비교하는 일을 돕는다.

같은 날짜·같은 구분 안에서, 새로 받은 목록의 일정과 이미 기록된 일정 중
**같은 회의·행사를 가리키는 것끼리** 짝지어라.

판단 기준
- 표기가 달라도(약칭, 괄호, 따옴표, 프로그램명) 같은 자리를 가리키면 같은 일정이다.
- 같은 시각·같은 장소·같은 참석자는 같은 일정이라는 **강한 근거**다.
  행사명이 많이 달라도 이 셋이 일치하면 같은 일정일 가능성이 높다.
- 시간이 바뀌었을 뿐 같은 행사인 경우가 흔하다. 시간 차이만으로 다르다고 보지 마라.
- 성격이 다른 행사(임명장 수여식 ↔ 점검회의)는 시간이 가까워도 다른 일정이다.
- **1:1로만 짝지어라.** 하나의 일정을 둘 이상과 짝지을 수 없다.
- **확신이 없으면 짝짓지 마라.** 짝이 없으면 각각 별개의 일정으로 처리된다.

예시
- `(토론)jibs"제주는 안전한가?" 10:00 JIBS 실장`
  ≡ `(방송대담) JIBS<시사이슈 결> 10:00 JIBS 실장`  → 같은 일정
- `신임 제주의료원장 임명장 수여식 08:50`
  ≠ `현안업무 점검회의(행정부지사주재) 09:00`  → 다른 일정

출력은 지정된 JSON 스키마만 쓴다. 설명을 덧붙이지 마라.
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "pairs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "new_id": {"type": "string"},
                                "old_id": {"type": "string"},
                            },
                            "required": ["new_id", "old_id"],
                        },
                    },
                },
                "required": ["group_id", "pairs"],
            },
        }
    },
    "required": ["groups"],
}


@dataclass
class PromptPayload:
    """모델에 보낼 본문과, 응답의 id를 되돌리기 위한 대응표."""

    body: dict[str, Any]
    #  group_id → (묶음, {new_id: CanonicalEvent}, {old_id: ExistingEvent})
    index: dict[str, tuple[UnresolvedGroup, dict[str, Any], dict[str, Any]]]


def build_payload(
    groups: list[UnresolvedGroup], *, send_attendees: bool = True, send_location: bool = True
) -> PromptPayload:
    """설계서 §11-9: 행사명·시간·장소·참석·주관부서만 보낸다."""
    items = []
    index: dict[str, tuple[UnresolvedGroup, dict[str, Any], dict[str, Any]]] = {}

    for gi, group in enumerate(groups):
        group_id = f"g{gi}"
        new_map: dict[str, Any] = {}
        old_map: dict[str, Any] = {}

        def describe(obj: Any, *, marked: bool | None = None) -> dict[str, Any]:
            row: dict[str, Any] = {
                "시간": obj.time_text,
                "행사명": obj.title,
                "주관부서": obj.department,
            }
            if send_location:
                row["장소"] = obj.location
            if send_attendees:
                row["참석"] = obj.attendees
            if marked is not None:
                row["표시됨"] = marked
            return row

        new_rows = []
        for ni, event in enumerate(group.incoming):
            new_id = f"{group_id}n{ni}"
            new_map[new_id] = event
            new_rows.append({"id": new_id, **describe(event)})

        old_rows = []
        for oi, event in enumerate(group.existing):
            old_id = f"{group_id}e{oi}"
            old_map[old_id] = event
            old_rows.append({"id": old_id, **describe(event, marked=event.is_marked)})

        index[group_id] = (group, new_map, old_map)
        items.append(
            {
                "group_id": group_id,
                "날짜": group.date.isoformat(),
                "구분": group.section.value,
                "새_파일_일정": new_rows,
                "기존_일정": old_rows,
            }
        )

    return PromptPayload(body={"묶음": items}, index=index)


def parse_response(data: dict[str, Any], payload: PromptPayload) -> set[str]:
    """응답 → "같은 일정"으로 판정된 pair_key 집합.

    존재하지 않는 id, 두 번 쓰인 id, 다른 묶음의 id는 버린다(설계서 §4-[7]).
    """
    same: set[str] = set()
    for entry in data.get("groups", []) or []:
        group_id = entry.get("group_id")
        if group_id not in payload.index:
            continue
        _, new_map, old_map = payload.index[group_id]
        used_new: set[str] = set()
        used_old: set[str] = set()
        for pair in entry.get("pairs", []) or []:
            new_id, old_id = pair.get("new_id"), pair.get("old_id")
            if new_id not in new_map or old_id not in old_map:
                continue  # 없는 id이거나 다른 묶음의 id
            if new_id in used_new or old_id in used_old:
                continue  # 1:1 위반
            used_new.add(new_id)
            used_old.add(old_id)
            same.add(pair_key(new_map[new_id], old_map[old_id]))
    return same
