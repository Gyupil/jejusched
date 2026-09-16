"""`Contents/header.xml`의 글자모양 표 — id → (색, 크기).

부기 줄 판별(§3.5)과 빨간 글씨 판별이 여기에 달려 있다. 글자모양을 읽지
못하면 둘 다 폴백으로 내려가되 파싱 자체는 계속한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree.ElementTree import Element

from .xmlutil import descendants

HIGHLIGHT_COLOR = "#FF0000"  # 실장이 직접 참석하는 일정 — 원본에서 빨간 글씨


@dataclass(frozen=True)
class CharStyle:
    color: str | None = None
    height: int | None = None  # HWPUNIT. 본문 1300, 부기 1100 같은 식

    @property
    def is_highlight(self) -> bool:
        return (self.color or "").upper() == HIGHLIGHT_COLOR


EMPTY = CharStyle()


def load_char_styles(header_root: Element | None) -> dict[str, CharStyle]:
    """`hh:charPr` 목록 → {id: CharStyle}. header.xml이 없으면 빈 표."""
    if header_root is None:
        return {}
    styles: dict[str, CharStyle] = {}
    for node in descendants(header_root, "charPr"):
        ident = node.get("id")
        if ident is None:
            continue
        raw_height = node.get("height")
        try:
            height = int(raw_height) if raw_height is not None else None
        except ValueError:
            height = None
        styles[ident] = CharStyle(color=node.get("textColor"), height=height)
    return styles
