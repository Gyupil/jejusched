"""hwpx 파서 — 설계서 M8(후순위).

`Parser` Protocol을 만족하도록 구현하면 확장자로 선택되어 그대로 끼워진다.
`tools/pdf_to_fixture.py`의 표 처리(구분·날짜 채우기, 페이지 넘김 행 병합,
시간 셀 안의 날짜 분리)가 그대로 참고가 된다.
"""

from __future__ import annotations

from pathlib import Path

from ..core.models import ParsedFile
from .base import ParseError


class HwpxParser:
    def parse(self, path: Path) -> ParsedFile:
        raise ParseError("hwpx 파서는 아직 구현되지 않았다 (M8)")
