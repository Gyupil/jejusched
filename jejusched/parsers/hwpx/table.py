"""표 격자 복원과 셀 줄 추출.

**병합 셀이 이 파서의 급소다.** hwpx는 병합된 칸을 아예 내보내지 않고
왼쪽·위쪽 셀의 `cellSpan`에만 적어 둔다. 그래서 `<hp:tr>`을 순서대로 읽으면
구분(도지사/실 일정)이 통째로 밀린다. `cellAddr`로 좌표를 잡고 `cellSpan`만큼
같은 셀을 펼쳐 두면 "채워 내리기"가 저절로 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.etree.ElementTree import Element

from .charpr import EMPTY, CharStyle
from .xmlutil import attr_int, child, children, local


@dataclass
class Line:
    """셀 안의 한 줄. 문단 하나 = 한 줄이다(§2.1)."""

    text: str
    style: CharStyle = EMPTY

    def __bool__(self) -> bool:
        return bool(self.text)


@dataclass
class Cell:
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    lines: list[Line] = field(default_factory=list)

    @property
    def texts(self) -> list[str]:
        """빈 줄을 뺀 텍스트 목록."""
        return [ln.text for ln in self.lines if ln.text]

    @property
    def filled(self) -> list[Line]:
        return [ln for ln in self.lines if ln.text]

    def joined(self, sep: str) -> str:
        return sep.join(self.texts)

    @property
    def squashed(self) -> str:
        """공백을 전부 뺀 텍스트 — 머리글·구분 비교용."""
        return "".join("".join(self.texts).split())


@dataclass
class Grid:
    """행×열로 펼친 표. 병합 칸은 같은 `Cell` 객체를 가리킨다."""

    rows: list[list[Cell | None]]

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return len(self.rows[0]) if self.rows else 0

    def cell(self, row: int, col: int) -> Cell | None:
        if 0 <= row < self.n_rows and 0 <= col < len(self.rows[row]):
            return self.rows[row][col]
        return None

    def text(self, row: int, col: int, sep: str = "") -> str:
        cell = self.cell(row, col)
        return cell.joined(sep) if cell else ""


def build_grid(tbl: Element, styles: dict[str, CharStyle], warnings: list[str]) -> Grid:
    """`hp:tbl` → 격자. 선언된 `rowCnt/colCnt`보다 좌표가 크면 격자를 늘린다."""
    n_rows = max(attr_int(tbl, "rowCnt", 0), 0)
    n_cols = max(attr_int(tbl, "colCnt", 0), 0)

    placed: list[Cell] = []
    for tr in children(tbl, "tr"):
        for tc in children(tr, "tc"):
            addr = child(tc, "cellAddr")
            span = child(tc, "cellSpan")
            cell = Cell(
                row=attr_int(addr, "rowAddr", -1),
                col=attr_int(addr, "colAddr", -1),
                row_span=max(attr_int(span, "rowSpan", 1), 1),
                col_span=max(attr_int(span, "colSpan", 1), 1),
                is_header=tc.get("header") == "1",
                lines=cell_lines(tc, styles, warnings),
            )
            if cell.row < 0 or cell.col < 0:
                warnings.append("셀 좌표(cellAddr)가 없어 건너뛴다")
                continue
            placed.append(cell)
            n_rows = max(n_rows, cell.row + cell.row_span)
            n_cols = max(n_cols, cell.col + cell.col_span)

    rows: list[list[Cell | None]] = [[None] * n_cols for _ in range(n_rows)]
    for cell in placed:
        for dr in range(cell.row_span):
            for dc in range(cell.col_span):
                rows[cell.row + dr][cell.col + dc] = cell
    return Grid(rows)


def cell_lines(tc: Element, styles: dict[str, CharStyle], warnings: list[str]) -> list[Line]:
    """셀 → 줄 목록. 줄마다 첫 글자의 색·크기를 달아 둔다.

    한 문단이 서식 변경 지점마다 여러 `hp:run`/`hp:t`로 쪼개지므로 **구분자 없이
    그대로 이어 붙인다**(§2.2). 공백을 끼우면 `수립(안) 에 따른`이 된다.
    """
    lines: list[Line] = []
    buffer: list[str] = []
    style: CharStyle | None = None

    def flush() -> None:
        nonlocal buffer, style
        lines.append(Line(text="".join(buffer).strip(), style=style or EMPTY))
        buffer, style = [], None

    def push(text: str, at: CharStyle) -> None:
        nonlocal style
        if not text:
            return
        for index, piece in enumerate(text.split("\n")):
            if index:
                flush()
            if piece:
                if style is None:
                    style = at
                buffer.append(piece)

    for para in children(child(tc, "subList"), "p"):
        for run in children(para, "run"):
            at = styles.get(run.get("charPrIDRef") or "", EMPTY)
            for node in run:
                name = local(node)
                if name == "t":
                    push(_text_of(node), at)
                elif name == "lineBreak":
                    flush()
                elif name == "tab":
                    push(" ", at)
                elif name == "tbl":
                    warnings.append("셀 안에 표가 중첩되어 있다 — 안쪽 표는 읽지 않는다")
        flush()  # 문단 하나 = 한 줄
    return lines


def _text_of(node: Element) -> str:
    """`hp:t` 안의 글자. 자식 태그(형광펜·되돌리기 표시 등) 사이 글자도 모은다.

    이 문서들에는 `hp:lineBreak`가 한 번도 안 나오지만, 다른 작성자·판본에서는
    나올 수 있으므로 줄바꿈으로 받아 둔다.
    """
    parts: list[str] = [node.text or ""]
    for inner in node:
        parts.append("\n" if local(inner) == "lineBreak" else _text_of(inner))
        parts.append(inner.tail or "")
    return "".join(parts)
