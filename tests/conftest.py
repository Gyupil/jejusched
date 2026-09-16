from __future__ import annotations

from pathlib import Path

import pytest

from jejusched.config import AppConfig
from jejusched.core.pipeline import Pipeline
from jejusched.core.state import State
from jejusched.gcal.fake import FakeCalendarClient
from jejusched.llm.resolver import FakeResolver
from jejusched.parsers.fixture_json import FixtureJsonParser

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_NAMES = ("0907", "0908", "0909", "0910")

#  픽스처에는 실제 일정이 들어 있어 저장소에 커밋하지 않는다(.gitignore).
#  로컬에 원본 PDF가 있으면 `python tools/pdf_to_fixture.py`로 만들어진다.
#  없는 환경(새 클론, CI)에서는 의존 테스트를 건너뛴다 — 실패가 아니다.
HAVE_FIXTURES = all((FIXTURES / f"{n}.json").is_file() for n in FIXTURE_NAMES)
requires_fixtures = pytest.mark.skipif(
    not HAVE_FIXTURES,
    reason="tests/fixtures/*.json이 없다. `python tools/pdf_to_fixture.py`로 생성하라.",
)

CALENDAR_NAME = "주요일정(자동)"
#  샘플 파일의 연도. 헤더 요일(9/7 월요일)과 맞는 해다.
YEAR = 2026

#  §8에서 LLM이 "같은 일정"으로 판정해야 하는 쌍 (0908의 9/10 실 일정)
SAME_TITLES = [("(방송대담) JIBS<시사이슈 결>", '(토론)jibs"제주는 안전한가?"')]


def fixture(name: str) -> Path:
    return FIXTURES / f"{name}.json"


class Harness:
    """한 파일씩 순차 적용하며 §8의 숫자를 확인하기 위한 테스트 장치."""

    def __init__(self, config: AppConfig, resolver: FakeResolver, emails: list[str]):
        self.state = State()
        self.client = FakeCalendarClient()
        self.resolver = resolver
        self.config = config
        self.pipeline = Pipeline(config, self.state, FixtureJsonParser(), self.client, resolver)
        self.targets = []
        for email in emails:
            cal_id = self.client.ensure_calendar(f"{CALENDAR_NAME}")
            if len(self.targets):  # 대상마다 자기 캘린더를 갖는다
                cal_id = self.client.ensure_calendar(f"{CALENDAR_NAME} #{len(self.targets) + 1}")
            self.targets.append(self.state.upsert_target(email, cal_id, CALENDAR_NAME))

    def apply(self, name: str):
        targets = [self.state.get_target(t.email) for t in self.targets]
        targets = [t for t in targets if t and t.enabled and not t.needs_reauth]
        return self.pipeline.run(
            fixture(name), targets, reference_year=YEAR, source_name=f"{name}_주요일정.hwpx"
        )

    def calendar_of(self, index: int = 0) -> str:
        return self.targets[index].calendar_id  # type: ignore[return-value]

    def summaries(self, index: int = 0) -> list[str]:
        return sorted(
            e["summary"] for e in self.client.events[self.calendar_of(index)].values()
        )


@pytest.fixture
def harness():
    def _make(emails: list[str] | None = None, config: AppConfig | None = None,
              resolver: FakeResolver | None = None) -> Harness:
        return Harness(
            config or AppConfig(),
            resolver if resolver is not None else FakeResolver(SAME_TITLES),
            emails or ["dev@example.com"],
        )

    return _make
