"""단계 오케스트레이션 — 설계서 §1의 [4]~[10].

LLM은 **파일당 1회**만 부르므로 대상별 처리를 두 겹으로 나눈다.
    대상마다 Reconcile + 규칙 1·2  →  전 대상 합쳐 LLM 1회  →  대상마다 규칙 3·4·5 + 적용
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import AppConfig
from ..gcal.client import (
    AuthExpired,
    CalendarClient,
    EventAlreadyExists,
    EventNotFound,
    to_existing_event,
)
from ..llm.budget import DailyBudget
from ..llm.resolver import Resolver, ResolverResult
from ..parsers.base import Parser
from . import matcher as M
from .matcher import GroupKey, PartialMatch, UnresolvedGroup
from .models import CanonicalEvent, ExistingEvent, Op, PlannedOp
from .normalize import normalize
from .planner import PlanContext, Planner
from .state import State, Target

log = logging.getLogger(__name__)


@dataclass
class TargetOutcome:
    target: Target
    ops: list[PlannedOp] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def held(self) -> bool:
        return self.counts.get("held", 0) > 0


@dataclass
class PipelineResult:
    file_date: date
    events: int = 0
    warnings: list[str] = field(default_factory=list)
    llm_calls: int = 0
    outcomes: list[TargetOutcome] = field(default_factory=list)

    @property
    def any_held(self) -> bool:
        return any(o.held for o in self.outcomes)

    def totals(self) -> dict[str, int]:
        keys = ("created", "updated", "marked", "restored", "skipped", "held", "suppressed")
        return {k: sum(o.counts.get(k, 0) for o in self.outcomes) for k in keys}


class Pipeline:
    def __init__(
        self,
        config: AppConfig,
        state: State,
        parser: Parser,
        client: CalendarClient | Callable[[Target], CalendarClient],
        resolver: Resolver,
    ):
        self.config = config
        self.state = state
        self.parser = parser
        #  대상마다 자격증명이 다르므로 클라이언트도 대상마다 다를 수 있다.
        #  테스트처럼 하나를 공유해도 되도록 둘 다 받는다.
        self.client = client
        self.resolver = resolver
        self.planner = Planner(config)
        self.budget = DailyBudget(state, config.llm.daily_budget)

    # ------------------------------------------------------------ 진입점

    def run(
        self,
        path: Path,
        targets: list[Target],
        *,
        reference_year: int | None = None,
        source_name: str | None = None,
    ) -> PipelineResult:
        parsed = self.parser.parse(path)
        year = reference_year or datetime.fromtimestamp(
            path.stat().st_mtime if path.exists() else datetime.now().timestamp()
        ).year
        file_date, events, judged, warnings = normalize(parsed, year)
        ctx = PlanContext(file_date=file_date, source_name=source_name or Path(path).name)
        result = PipelineResult(file_date=file_date, events=len(events), warnings=warnings)

        if not events:
            log.warning("%s: 일정이 하나도 없다 — 건너뛴다", path)
            return result

        window = self._window(events, file_date)

        # ── 1단계: 대상마다 Reconcile + 규칙 1·2
        staged: list[tuple[Target, PartialMatch, list[ExistingEvent]]] = []
        for target in targets:
            try:
                existing = self.reconcile(target, *window)
            except AuthExpired:
                log.error("%s: 재로그인이 필요하다 — 이 대상은 건너뛴다", target.email)
                self.state.set_needs_reauth(target.id)
                result.outcomes.append(TargetOutcome(target=target, error="needs_reauth"))
                continue
            staged.append((target, M.match_rules_1_2(events, existing), existing))

        # ── 2단계: 전 대상의 미해결 묶음을 모아 LLM 1회
        pending = [g for _, partial, _ in staged for g in partial.pending]
        verdicts = self._resolve(pending)
        result.llm_calls = verdicts.calls

        # ── 3단계: 대상마다 규칙 3·4·5 → 계획 → 적용
        for target, partial, _ in staged:
            outcome = self._apply_target(target, partial, judged, ctx, verdicts)
            result.outcomes.append(outcome)
        return result

    def client_for(self, target: Target) -> CalendarClient:
        """대상에 맞는 캘린더 클라이언트. 팩토리를 받았으면 대상마다 새로 만든다."""
        if isinstance(self.client, CalendarClient):
            return self.client
        return self.client(target)

    @staticmethod
    def _window(events: list[CanonicalEvent], file_date: date) -> tuple[date, date]:
        """조회 범위 = 파일 내 최소 날짜 -1일 ~ 최대 날짜 +1일 (설계서 §4-[5])."""
        dates = [e.date for e in events] or [file_date]
        return min(dates) - timedelta(days=1), max(dates) + timedelta(days=1)

    # ------------------------------------------------------- [5] Reconcile

    def reconcile(self, target: Target, start: date, end: date) -> list[ExistingEvent]:
        """캘린더를 진실의 원천으로 삼아 로컬 인덱스를 맞춘다.

        로컬에는 있는데 캘린더에 없으면 사용자가 직접 지운 것으로 보고 기록한다.
        """
        assert target.calendar_id, f"{target.email}: calendar_id가 없다"
        raws = self.client_for(target).list_events(target.calendar_id, start, end)
        existing = [ev for ev in (to_existing_event(r) for r in raws) if ev is not None]

        live_ids = {e.google_event_id for e in existing}
        for stale in self.state.local_events(target.id, start, end):
            if stale.google_event_id not in live_ids:
                log.info("%s: 사용자가 직접 삭제 — %s", target.email, stale.google_event_id)
                self.state.mark_user_deleted(
                    target.id, stale.event_key, stale.logical_id, stale.date
                )
        self.state.replace_events(target.id, existing, start, end)
        return existing

    # ----------------------------------------------------- [7] LLM Resolver

    def _resolve(self, pending: list[UnresolvedGroup]) -> ResolverResult:
        """캐시 → 예산 → 모델 순으로 규칙 3의 판정을 구한다."""
        if not pending:
            return ResolverResult()

        # 대상들은 같은 내용을 미러링하므로 같은 묶음이 여러 번 온다 — 합친다
        unique: dict[tuple[Any, ...], UnresolvedGroup] = {}
        for group in pending:
            unique.setdefault(
                (group.date, group.section, tuple(sorted(group.pair_keys()))), group
            )
        groups = list(unique.values())

        all_pairs = {pk for g in groups for pk in g.pair_keys()}
        cached = self.state.cached_verdicts(all_pairs)
        same = {pk for pk, verdict in cached.items() if verdict}

        ask = [g for g in groups if not all(pk in cached for pk in g.pair_keys())]
        if not ask:
            log.info("규칙 3: %d개 묶음 전부 캐시 적중 — LLM 호출 없음", len(groups))
            return ResolverResult(same_pairs=same)

        if not self.config.llm.enabled:
            log.info("LLM 비활성 — 규칙 3은 전부 '다른 일정'으로 처리한다")
            return ResolverResult(same_pairs=same)

        if not self.budget.allow():
            log.warning("LLM 일일 예산(%d) 소진 — %d개 묶음 HOLD",
                        self.config.llm.daily_budget, len(ask))
            return ResolverResult(same_pairs=same, failed={g.key for g in ask})

        self.budget.consume()
        outcome = self.resolver.resolve(ask)
        same |= outcome.same_pairs

        #  판정을 받은 묶음의 모든 쌍에 결론이 났다 — 짝이 안 된 쌍은 "다름"
        fresh: dict[str, bool] = {}
        for group in ask:
            if group.key in outcome.failed:
                continue
            for pk in group.pair_keys():
                fresh[pk] = pk in same
        if fresh:
            self.state.store_verdicts(fresh, self.config.llm.models[0])
        return ResolverResult(same_pairs=same, failed=outcome.failed, calls=outcome.calls)

    # -------------------------------------------- [8] Planner + [9] Applier

    def _apply_target(
        self,
        target: Target,
        partial: PartialMatch,
        judged: set[GroupKey],
        ctx: PlanContext,
        verdicts: ResolverResult,
    ) -> TargetOutcome:
        match_result = M.finish(
            partial,
            same_pairs=verdicts.same_pairs,
            failed_groups=verdicts.failed,
            judged=judged,
            user_deleted=self.state.user_deleted_keys(target.id),
        )
        ops = self.planner.plan(match_result, ctx)
        outcome = TargetOutcome(target=target, ops=ops)
        outcome.counts = {"suppressed": len(match_result.suppressed)}

        if self.config.dry_run:
            for op in ops:
                log.info("[dry-run] %s", op)
            outcome.counts.update(self._count(ops, applied=False))
            return outcome

        outcome.counts.update(self._execute(target, ops, ctx.file_date))
        return outcome

    def _execute(
        self, target: Target, ops: list[PlannedOp], file_date: date | None = None
    ) -> dict[str, int]:
        assert target.calendar_id
        client = self.client_for(target)
        counts = {"created": 0, "updated": 0, "marked": 0, "restored": 0, "skipped": 0, "held": 0}
        label = {Op.CREATE: "created", Op.UPDATE: "updated", Op.MARK: "marked",
                 Op.RESTORE: "restored", Op.SKIP: "skipped", Op.HOLD: "held"}
        for op in ops:
            if op.op in (Op.SKIP, Op.HOLD):
                counts[label[op.op]] += 1
                continue
            try:
                if op.op is Op.CREATE:
                    try:
                        client.insert_event(target.calendar_id, op.google_event_id, op.body)
                    except EventAlreadyExists:
                        #  결정적 id 덕분에 재시도가 멱등하다 — 이미 있으면 성공이다
                        log.debug("이미 존재(409) — 성공으로 본다: %s", op.google_event_id)
                else:
                    client.patch_event(target.calendar_id, op.google_event_id, op.body)
            except EventNotFound:
                log.info("%s: 적용 중 사라짐 — 사용자가 지운 것으로 기록", op.google_event_id)
                self.state.mark_user_deleted(target.id, op.event_key, op.logical_id, op.date)
                counts["skipped"] += 1
                continue
            except AuthExpired:
                self.state.set_needs_reauth(target.id)
                raise
            #  연산 직후 인덱스를 갱신한다(§4-[9]). 이 기록이 있어야 다음
            #  Reconcile에서 사용자가 직접 지운 일정을 알아볼 수 있다.
            self.state.upsert_event(
                target.id,
                logical_id=op.logical_id,
                google_event_id=op.google_event_id or "",
                event_key=op.event_key,
                content_hash=op.content_hash,
                when=op.date,
                section=op.section,
                slot=op.slot,
                status=op.status,
                file_date=file_date,
            )
            counts[label[op.op]] += 1
        return counts

    @staticmethod
    def _count(ops: list[PlannedOp], *, applied: bool) -> dict[str, int]:
        label = {Op.CREATE: "created", Op.UPDATE: "updated", Op.MARK: "marked",
                 Op.RESTORE: "restored", Op.SKIP: "skipped", Op.HOLD: "held"}
        counts = {v: 0 for v in label.values()}
        for op in ops:
            counts[label[op.op]] += 1
        return counts
