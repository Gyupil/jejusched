"""규칙 3 판정자 — 설계서 §4-[7].

호출은 **파일당 1회**다. 모든 대상의 미해결 묶음을 한 번에 받아 판정한다.
실제 Gemini 구현은 `gemini.py`, 테스트는 `FakeResolver`, 키가 없을 때는
`NullResolver`(전부 "다른 일정")를 쓴다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..core.matcher import GroupKey, UnresolvedGroup, pair_key
from ..core.normalize import title_norm

if TYPE_CHECKING:  # 순환 임포트를 피한다
    from ..config import AppConfig, LlmSettings

log = logging.getLogger(__name__)


@dataclass
class ResolverResult:
    """`same_pairs`에 없는 쌍은 모두 '다른 일정'이다.

    `failed`에 든 묶음은 판정을 받지 못한 것이고, 호출자는 그 (날짜, 구분)을
    HOLD 한다. `calls`는 실제 네트워크 호출 수(테스트가 §8의 호출 횟수를 검증).
    """

    same_pairs: set[str] = field(default_factory=set)
    failed: set[GroupKey] = field(default_factory=set)
    calls: int = 0
    #  실제로 답한 모델. 폴백이 일어나면 목록의 첫 모델이 아니다 — 캐시에
    #  잘못 적으면 나중에 "어느 모델이 이렇게 판정했나"를 추적할 수 없다.
    model: str | None = None


@runtime_checkable
class Resolver(Protocol):
    def resolve(self, groups: list[UnresolvedGroup]) -> ResolverResult: ...


class NullResolver:
    """LLM 비활성(키 미입력). 규칙 3은 항상 '다른 일정'으로 처리한다."""

    def resolve(self, groups: list[UnresolvedGroup]) -> ResolverResult:
        return ResolverResult()


class FakeResolver:
    """테스트용. '같은 일정'으로 볼 행사명 쌍을 미리 주입한다.

    행사명은 `title_norm`으로 비교하므로 표기가 달라도 원문 그대로 적으면 된다.
    """

    def __init__(self, same_titles: list[tuple[str, str]] | None = None, *, fail: bool = False,
                 model: str = "fake-model"):
        self._same = {(title_norm(a), title_norm(b)) for a, b in (same_titles or [])}
        self._fail = fail
        self._model = model
        self.calls = 0
        self.seen_groups: list[UnresolvedGroup] = []

    def resolve(self, groups: list[UnresolvedGroup]) -> ResolverResult:
        if not groups:
            return ResolverResult()
        self.calls += 1
        self.seen_groups.extend(groups)
        if self._fail:
            return ResolverResult(failed={g.key for g in groups}, calls=1)
        same: set[str] = set()
        for group in groups:
            for inc in group.incoming:
                for ex in group.existing:
                    if (inc.title_norm, ex.title_norm) in self._same:
                        same.add(pair_key(inc, ex))
        return ResolverResult(same_pairs=same, calls=1, model=self._model)


# ------------------------------------------------------ 실제 Gemini 구현 (M4)


class GeminiResolver:
    """Gemini로 규칙 3을 판정한다 — 설계서 §4-[7].

    한 파일의 미해결 묶음 전부를 **한 요청**에 담는다. 모델은 폴백 체인을
    순서대로 시도하고, 전부 실패하면 그 묶음들을 HOLD 한다(예외를 던지지 않는다).
    """

    def __init__(self, api_key: str, config: "LlmSettings"):  # noqa: F821
        self._api_key = api_key
        self._config = config
        self._client = None
        self.last_model: str | None = None

    def _get_client(self):
        if self._client is None:
            from google import genai  # 지연 임포트: 키가 없으면 여기까지 오지 않는다

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def resolve(self, groups: list[UnresolvedGroup]) -> ResolverResult:
        if not groups:
            return ResolverResult()

        from ..llm.prompts import (
            RESPONSE_SCHEMA,
            SYSTEM_INSTRUCTION,
            build_payload,
            parse_response,
        )

        payload = build_payload(
            groups,
            send_attendees=self._config.send_attendees,
            send_location=self._config.send_location,
        )
        prompt = json.dumps(payload.body, ensure_ascii=False, indent=1)

        for model in self._config.models:
            try:
                raw = self._generate(model, prompt, SYSTEM_INSTRUCTION, RESPONSE_SCHEMA)
            except Exception as exc:  # noqa: BLE001 — 폴백 체인이 목적이다
                log.warning("%s 호출 실패 — 다음 모델로 (%s)", model, exc)
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("%s 응답이 JSON이 아니다 — 다음 모델로", model)
                continue
            self.last_model = model
            same = parse_response(data, payload)
            log.info("규칙 3: %s가 묶음 %d개에서 %d쌍을 같은 일정으로 판정",
                     model, len(groups), len(same))
            return ResolverResult(same_pairs=same, calls=1, model=model)

        log.error("모든 모델이 실패했다 — 묶음 %d개를 HOLD 한다", len(groups))
        return ResolverResult(failed={g.key for g in groups}, calls=1)

    def _generate(self, model: str, prompt: str, system: str, schema: dict) -> str:
        from google.genai import types

        base: dict[str, object] = {
            "system_instruction": system,
            "response_mime_type": "application/json",
            "response_schema": schema,
            "temperature": 0,
        }
        thinking: object | None = None
        try:
            #  생각을 얕게 — 판정은 대조 작업이지 추론 작업이 아니다
            thinking = types.ThinkingConfig(thinking_level="LOW")
        except (AttributeError, TypeError, ValueError):
            log.debug("이 SDK는 thinking_level을 받지 않는다 — 생략한다")

        attempts: list[dict[str, object]] = []
        if thinking is not None:
            attempts.append({**base, "thinking_config": thinking})
        attempts.append(base)

        last: Exception | None = None
        for config in attempts:
            try:
                response = self._get_client().models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config),
                )
                return response.text or ""
            except Exception as exc:  # noqa: BLE001
                #  모델이 thinking_config를 안 받으면 400이 온다. 그걸 "모델 실패"로
                #  읽어 폴백까지 소진하면 멀쩡한 모델을 두고 전부 HOLD 된다.
                last = exc
                if "thinking" in config:
                    log.info("%s가 thinking_config를 거부했다 — 빼고 다시 시도한다", model)
                    continue
                raise
        raise last or RuntimeError("생성 실패")


def build_resolver(config: "AppConfig", api_key: str | None) -> Resolver:  # noqa: F821
    """설정과 키를 보고 알맞은 Resolver를 고른다.

    키가 없거나 `llm.enabled=false`면 `NullResolver` — 규칙 3은 전부 "다름"이다.
    """
    if not config.llm.enabled or not api_key:
        log.info("LLM 비활성 — 규칙 3은 '다른 일정'으로 처리한다")
        return NullResolver()
    return GeminiResolver(api_key, config.llm)
