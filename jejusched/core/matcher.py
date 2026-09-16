"""같은 일정 판단 — 설계서 §4-[6]의 규칙 1~5.

비교의 전제
  * 같은 날짜, 같은 구분 안에서만 비교한다.
  * 한 번 짝지어진 일정은 다음 규칙에서 다시 쓰지 않는다.
  * `*` 표시된 기존 일정도 짝짓기 대상이다(다시 실리면 복구된다).

LLM 호출은 파일당 1회로 묶어야 하므로(§4-[7]) 두 단계로 나눈다.
    1) `match_rules_1_2()` — 대상마다 호출, 규칙 1·2를 적용하고 남은 쌍을 보고
    2) 모든 대상의 pending을 모아 Resolver를 1회 호출
    3) `finish()` — 대상마다 호출, 규칙 3·4·5를 적용
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .models import CanonicalEvent, EventStatus, ExistingEvent, Section

GroupKey = tuple[date, Section]


def pair_key(incoming: CanonicalEvent, existing: ExistingEvent) -> str:
    """LLM 캐시 키. 대상들은 같은 내용을 미러링하므로 대상 간 공유된다."""
    return f"{incoming.event_key}|{existing.event_key}"


@dataclass
class Match:
    incoming: CanonicalEvent
    existing: ExistingEvent
    rule: int  # 1 | 2 | 3
    reason: str


@dataclass
class UnresolvedGroup:
    """규칙 1·2로 가리지 못해 LLM 판단이 필요한 (날짜, 구분) 묶음."""

    date: date
    section: Section
    incoming: list[CanonicalEvent]
    existing: list[ExistingEvent]

    @property
    def key(self) -> GroupKey:
        return (self.date, self.section)

    def pair_keys(self) -> list[str]:
        return [pair_key(i, e) for i in self.incoming for e in self.existing]


@dataclass
class PartialMatch:
    """규칙 1·2까지 적용한 중간 결과."""

    matches: list[Match] = field(default_factory=list)
    pending: list[UnresolvedGroup] = field(default_factory=list)
    #  LLM이 필요 없는 잔여분 (한쪽만 남은 묶음)
    leftover_incoming: dict[GroupKey, list[CanonicalEvent]] = field(default_factory=dict)
    leftover_existing: dict[GroupKey, list[ExistingEvent]] = field(default_factory=dict)


@dataclass
class MatchResult:
    """규칙 5개를 모두 적용한 최종 결과."""

    matches: list[Match] = field(default_factory=list)
    creates: list[CanonicalEvent] = field(default_factory=list)
    marks: list[ExistingEvent] = field(default_factory=list)
    held: set[GroupKey] = field(default_factory=set)
    #  사용자가 직접 지운 적이 있어 다시 만들지 않은 것
    suppressed: list[CanonicalEvent] = field(default_factory=list)


def _group(items, key):
    out: dict[GroupKey, list] = defaultdict(list)
    for it in items:
        out[key(it)].append(it)
    return out


def match_rules_1_2(
    incoming: list[CanonicalEvent], existing: list[ExistingEvent]
) -> PartialMatch:
    """규칙 1(시간·행사명 동일)과 규칙 2(행사명 동일 1:1)를 적용한다."""
    result = PartialMatch()
    by_in = _group(incoming, lambda e: (e.date, e.section))
    by_ex = _group(existing, lambda e: (e.date, e.section))

    for key in sorted(set(by_in) | set(by_ex), key=lambda k: (k[0], k[1].value)):
        ins = list(by_in.get(key, []))
        exs = list(by_ex.get(key, []))

        # ── 규칙 1: event_key(= 날짜·구분·시작시각·행사명) 완전 일치
        ex_by_event_key: dict[str, list[ExistingEvent]] = defaultdict(list)
        for ex in exs:
            ex_by_event_key[ex.event_key].append(ex)
        rest_in: list[CanonicalEvent] = []
        for inc in ins:
            bucket = ex_by_event_key.get(inc.event_key)
            if bucket:
                result.matches.append(
                    Match(inc, bucket.pop(0), 1, "규칙 1: 시간·행사명 동일")
                )
            else:
                rest_in.append(inc)
        rest_ex = [ex for bucket in ex_by_event_key.values() for ex in bucket]

        # ── 규칙 2: 남은 것 중 행사명이 같은 것이 양쪽에 하나씩만
        in_by_title: dict[str, list[CanonicalEvent]] = defaultdict(list)
        for inc in rest_in:
            in_by_title[inc.title_norm].append(inc)
        ex_by_title: dict[str, list[ExistingEvent]] = defaultdict(list)
        for ex in rest_ex:
            ex_by_title[ex.title_norm].append(ex)
        paired_in: set[int] = set()
        paired_ex: set[int] = set()
        for title, ins_t in in_by_title.items():
            exs_t = ex_by_title.get(title, [])
            if len(ins_t) == 1 and len(exs_t) == 1:
                result.matches.append(
                    Match(ins_t[0], exs_t[0], 2, "규칙 2: 행사명 동일(양쪽 1건)")
                )
                paired_in.add(id(ins_t[0]))
                paired_ex.add(id(exs_t[0]))
        rest_in = [i for i in rest_in if id(i) not in paired_in]
        rest_ex = [e for e in rest_ex if id(e) not in paired_ex]

        # ── 남은 것이 양쪽에 모두 있으면 규칙 3(LLM) 대상
        if rest_in and rest_ex:
            result.pending.append(UnresolvedGroup(key[0], key[1], rest_in, rest_ex))
        else:
            if rest_in:
                result.leftover_incoming[key] = rest_in
            if rest_ex:
                result.leftover_existing[key] = rest_ex
    return result


def finish(
    partial: PartialMatch,
    *,
    same_pairs: set[str],
    failed_groups: set[GroupKey],
    judged: set[GroupKey],
    user_deleted: set[str],
) -> MatchResult:
    """규칙 3·4·5를 적용해 최종 결과를 만든다.

    `same_pairs`  — LLM이 "같은 일정"이라고 본 pair_key 집합
    `failed_groups` — 판정을 받지 못한 묶음. 그 (날짜, 구분)은 HOLD 한다.
    `judged`      — 새 파일이 일정을 적은 (날짜, 구분). 규칙 5는 여기서만.
    `user_deleted` — 사용자가 캘린더에서 직접 지운 event_key. 다시 만들지 않는다.
    """
    result = MatchResult(matches=list(partial.matches))
    leftover_in = {k: list(v) for k, v in partial.leftover_incoming.items()}
    leftover_ex = {k: list(v) for k, v in partial.leftover_existing.items()}

    for group in partial.pending:
        if group.key in failed_groups:
            # 판정 보류 — 이 (날짜, 구분)은 이번 실행에서 손대지 않는다
            result.held.add(group.key)
            continue
        rest_in, rest_ex = list(group.incoming), list(group.existing)
        used_in: set[int] = set()
        used_ex: set[int] = set()
        # ── 규칙 3: LLM 판정. 1:1을 강제한다(먼저 나온 짝이 이긴다).
        for inc in rest_in:
            for ex in rest_ex:
                if id(inc) in used_in or id(ex) in used_ex:
                    continue
                if pair_key(inc, ex) in same_pairs:
                    result.matches.append(Match(inc, ex, 3, "규칙 3: LLM 판정 같음"))
                    used_in.add(id(inc))
                    used_ex.add(id(ex))
        leftover_in.setdefault(group.key, []).extend(
            i for i in rest_in if id(i) not in used_in
        )
        leftover_ex.setdefault(group.key, []).extend(
            e for e in rest_ex if id(e) not in used_ex
        )

    # ── 규칙 4: 짝 없는 새 일정은 추가. 단 직접 지운 적 있으면 재생성 금지.
    for key, items in leftover_in.items():
        if key in result.held:
            continue
        for inc in items:
            if inc.event_key in user_deleted:
                result.suppressed.append(inc)
            else:
                result.creates.append(inc)

    # ── 규칙 5: 짝 없는 기존 일정에 `*` 표시. judged 안에서만, 이미 표시된 건 제외.
    for key, items in leftover_ex.items():
        if key in result.held or key not in judged:
            continue
        for ex in items:
            if ex.status is EventStatus.ACTIVE:
                result.marks.append(ex)
    return result
