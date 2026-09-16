"""hwpx 컨테이너 열기 — ZIP에서 본문 섹션과 header.xml을 꺼낸다.

`.hwp`(옛 이진 CFB 형식)는 여기서 분명하게 거절한다. 사용자가 확장자만 보고
잘못 넣기 쉬운데, 뒤에서 XML 파싱 오류로 터지면 원인을 알기 어렵다.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element, ParseError as XmlParseError, fromstring

from ..base import ParseError
from .xmlutil import descendants, local

#  OLE2/CFB 서명 — 구형 .hwp 파일의 첫 8바이트
_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_SECTION_RE = re.compile(r"section(\d+)\.xml$", re.IGNORECASE)
_HEADER_NAMES = ("Contents/header.xml", "Contents/Header.xml")


class HwpxContainer:
    """열린 hwpx 하나. 섹션 루트 목록과 header.xml 루트를 들고 있다."""

    def __init__(self, sections: list[Element], header: Element | None):
        self.sections = sections
        self.header = header


def open_hwpx(path: Path) -> HwpxContainer:
    """`.hwpx` → 파싱된 XML 루트들. 열 수 없으면 `ParseError`."""
    path = Path(path)
    _reject_binary_hwp(path)
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ParseError(f"hwpx(ZIP)로 열 수 없다: {path.name}") from exc

    with archive:
        names = set(archive.namelist())
        section_names = _section_names(archive, names)
        if not section_names:
            raise ParseError(f"본문(Contents/section*.xml)이 없다: {path.name}")
        sections = [_parse(archive, name, path) for name in section_names]
        header_name = next((n for n in _HEADER_NAMES if n in names), None)
        header = _parse(archive, header_name, path) if header_name else None
    return HwpxContainer(sections, header)


def _reject_binary_hwp(path: Path) -> None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        raise ParseError(f"파일을 읽을 수 없다: {path.name}") from exc
    if head == _CFB_MAGIC:
        raise ParseError(
            f"{path.name}은 구형 .hwp(이진) 형식이다 — 한/글에서 .hwpx로 저장해야 읽을 수 있다"
        )


def _section_names(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    """spine 순서를 우선 쓰고, 없으면 파일명 숫자 순으로 정렬한다."""
    ordered = _spine_order(archive, names)
    if ordered:
        return ordered
    found = [n for n in names if _SECTION_RE.search(n)]
    return sorted(found, key=lambda n: int(_SECTION_RE.search(n).group(1)))  # type: ignore[union-attr]


def _spine_order(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    """`Contents/content.hpf`의 manifest+spine으로 섹션 순서를 읽는다."""
    if "Contents/content.hpf" not in names:
        return []
    try:
        root = fromstring(archive.read("Contents/content.hpf"))
    except (KeyError, XmlParseError, OSError):
        return []
    hrefs: dict[str, str] = {}
    for item in descendants(root, "item"):
        ident, href = item.get("id"), item.get("href")
        if ident and href:
            hrefs[ident] = href
    ordered: list[str] = []
    for ref in descendants(root, "itemref"):
        href = hrefs.get(ref.get("idref") or "")
        if not href or not _SECTION_RE.search(href):
            continue
        #  hpf의 href는 Contents/ 기준 상대 경로다
        for candidate in (href, f"Contents/{href.lstrip('./')}"):
            if candidate in names:
                ordered.append(candidate)
                break
    return ordered


def _parse(archive: zipfile.ZipFile, name: str, path: Path) -> Element:
    try:
        return fromstring(archive.read(name))
    except (KeyError, XmlParseError, OSError) as exc:
        raise ParseError(f"{path.name}의 {name}을 해석할 수 없다") from exc


def text_of_element(elem: Element) -> str:
    """요소 아래 모든 `hp:t`의 글자를 이어 붙인다."""
    return "".join("".join(t.itertext()) for t in descendants(elem, "t"))


def top_level_paragraph_text(section: Element) -> list[str]:
    """표 **바깥** 문단들의 텍스트. 파일 날짜(`【9월 7일 월요일】`)가 여기 있다."""
    out: list[str] = []
    for node in section:
        if local(node) != "p":
            continue
        if any(local(inner) == "tbl" for inner in node.iter()):
            continue  # 표를 품은 문단은 표로 다룬다
        out.append(text_of_element(node))
    return out
