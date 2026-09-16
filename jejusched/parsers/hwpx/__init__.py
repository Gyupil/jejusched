"""hwpx 파서 — 설계서 M8 / `docs/parser_design.md`.

`zipfile` + 표준 `xml.etree`만 쓴다. 한/글 설치도, 외부 XML 라이브러리도
요구하지 않는다(윈도우 번들에 숨은 의존이 늘지 않는다).

출력은 `ParsedFile`/`ScheduleItem` 그대로다 — 파이프라인의 다른 부분은
이 파일이 생겼다고 해서 한 줄도 바뀌지 않는다.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path
from xml.etree.ElementTree import Element

from ...core.models import ParsedFile, ScheduleItem
from ..base import ParseError
from .charpr import load_char_styles
from .container import open_hwpx, text_of_element, top_level_paragraph_text
from .extract import items_from_grid
from .table import build_grid
from .xmlutil import descendants, local

log = logging.getLogger(__name__)

#  【9월 7일 월요일】 — 표 바깥 문단에 있다(§3.8)
HEADER_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*([월화수목금토일])\s*요일")
#  폴백: 0907_주요일정.hwpx
#  `\b`는 숫자와 밑줄 사이에 서지 않는다(밑줄도 단어 문자다) — 뒤에 숫자만 없으면 된다
FILENAME_RE = re.compile(r"^(\d{2})(\d{2})(?!\d)")

EXPECTED_TABLES = 2


class HwpxParser:
    """`.hwpx` 주요일정 → `ParsedFile`."""

    def parse(self, path: Path) -> ParsedFile:
        path = Path(path)
        try:
            return self._parse(path)
        except ParseError:
            raise
        except Exception as exc:  # noqa: BLE001 — Intake가 `failed`로 기록해야 한다
            raise ParseError(f"{path.name}을 해석할 수 없다: {exc}") from exc

    # ------------------------------------------------------------------

    def _parse(self, path: Path) -> ParsedFile:
        container = open_hwpx(path)
        styles = load_char_styles(container.header)
        warnings: list[str] = []
        if not styles:
            warnings.append("글자모양(header.xml)을 읽지 못했다 — 부기·강조는 폴백 규칙으로 가른다")

        month, day, weekday = self._file_date(container.sections, path, warnings)

        tables = [t for section in container.sections for t in _top_tables(section)]
        if not tables:
            raise ParseError(f"{path.name}에 표가 없다 — 주요일정 문서가 맞는지 확인하라")
        if len(tables) != EXPECTED_TABLES:
            warnings.append(f"표가 {len(tables)}개다(보통 {EXPECTED_TABLES}개) — 전부 읽는다")

        items: list[ScheduleItem] = []
        for index, tbl in enumerate(tables):
            grid = build_grid(tbl, styles, warnings)
            items.extend(items_from_grid(grid, index, warnings))

        if not items:
            raise ParseError(f"{path.name}에서 일정을 한 건도 읽지 못했다")

        #  구분이 통째로 밀리는 것이 이 파서의 가장 무서운 오작동이라
        #  매번 건수를 남긴다(완성 절차 2-2).
        counts = Counter(item.section for item in items)
        log.info(
            "%s: %d건 파싱 — %s%s",
            path.name, len(items),
            " · ".join(f"{name} {n}" for name, n in sorted(counts.items())),
            f" · 경고 {len(warnings)}건" if warnings else "",
        )
        for line in warnings:
            log.warning("%s: %s", path.name, line)

        return ParsedFile(
            file_date_month=month,
            file_date_day=day,
            file_weekday=weekday,
            items=items,
            warnings=warnings,
        )

    def _file_date(
        self, sections: list[Element], path: Path, warnings: list[str]
    ) -> tuple[int, int, str]:
        for text in (t for section in sections for t in top_level_paragraph_text(section)):
            match = HEADER_RE.search(text)
            if match:
                return int(match.group(1)), int(match.group(2)), match.group(3)
        #  헤더 문단을 못 찾으면 본문 전체에서 한 번 더 찾는다(글상자에 들어간 경우)
        for section in sections:
            match = HEADER_RE.search(text_of_element(section))
            if match:
                warnings.append("파일 날짜를 표 안에서 찾았다 — 머리말 문단이 아니다")
                return int(match.group(1)), int(match.group(2)), match.group(3)
        match = FILENAME_RE.match(path.stem)
        if match:
            warnings.append(f"머리말에서 날짜를 못 찾아 파일명({path.stem})을 쓴다 — 요일 확인 불가")
            return int(match.group(1)), int(match.group(2)), ""
        raise ParseError(f"{path.name}에서 파일 날짜(【9월 7일 월요일】)를 찾지 못했다")


def _top_tables(section: Element) -> list[Element]:
    """문서 순서대로 표를 모은다. **표 안에 중첩된 표는 건너뛴다.**

    중첩 표를 따로 세면 같은 행을 두 번 읽어 일정이 중복된다.
    """
    found = list(descendants(section, "tbl"))
    nested = {
        id(inner)
        for tbl in found
        for inner in tbl.iter()
        if inner is not tbl and local(inner) == "tbl"
    }
    return [tbl for tbl in found if id(tbl) not in nested]


__all__ = ["HwpxParser", "HEADER_RE"]
