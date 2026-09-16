"""격자 → `ScheduleItem`. 열별 결합·부기 분리·시간/날짜/구분 해석(§3.4~§3.7)."""

from __future__ import annotations

import re

from ...core.models import ScheduleItem, Section
from .table import Cell, Grid

#  표 머리글. 있으면 열 위치를 여기서 읽고, 없으면 아래 기본 배치를 쓴다.
SECTION_COL, TIME_COL, TITLE_COL, PLACE_COL, ATTEND_COL, DEPT_COL = range(6)
_HEADER_LABELS = {
    "구분": SECTION_COL, "날짜": SECTION_COL, "일자": SECTION_COL,
    "시간": TIME_COL, "시각": TIME_COL,
    "행사명": TITLE_COL, "행사": TITLE_COL,
    "장소": PLACE_COL,
    "참석": ATTEND_COL, "참석자": ATTEND_COL,
    "주관부서": DEPT_COL, "주관": DEPT_COL, "담당부서": DEPT_COL,
}

#  "9/5" 또는 "9/5(토)" — 줄 앞에서 떼어낸다
_DATE_PREFIX_RE = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*(?:\(\s*[월화수목금토일]\s*\))?\s*")
_WEEKDAY_ONLY_RE = re.compile(r"^\s*\(?\s*[월화수목금토일]\s*\)?\s*$")
_DATE_ANYWHERE_RE = re.compile(r"\d{1,2}\s*/\s*\d{1,2}")
_TWO_TIMES_RE = re.compile(r"^(\d{1,2}:\d{2})(\d{1,2}:\d{2})$")
_NOTE_LEAD = "*※"

#  표 2(향후 일정)는 구분 열이 없다. 전부 실 일정이다.
DEFAULT_SECTION = Section.DEPT.value


def column_map(grid: Grid, header_row: int | None) -> dict[int, int]:
    """머리글이 있으면 그 글자로, 없으면 위치 그대로 열을 잡는다.

    사람이 손으로 만드는 문서라 언젠가 열이 늘거나 순서가 바뀔 수 있다.
    머리글이 있을 때는 그것을 믿는 편이 위치보다 안전하다.
    """
    default = {index: index for index in range(6)}
    if header_row is None:
        return default
    found: dict[int, int] = {}
    for col in range(grid.n_cols):
        cell = grid.cell(header_row, col)
        if cell is None:
            continue
        role = _HEADER_LABELS.get(cell.squashed)
        if role is not None and role not in found:
            found[role] = col
    #  여섯 자리를 다 알아내지 못하면 섞어 쓰지 않고 통째로 기본 배치로 간다
    return found if len(found) == 6 else default


def find_header_row(grid: Grid) -> int | None:
    """머리글 행 — `header="1"` 속성이 1차, 글자 일치가 2차 기준(§2.3)."""
    for row in range(min(2, grid.n_rows)):  # 머리글은 맨 위에만 온다
        cells = [grid.cell(row, col) for col in range(grid.n_cols)]
        present = [c for c in cells if c is not None]
        if not present:
            continue
        if any(c.is_header for c in present):
            return row
        labels = [c.squashed for c in present]
        if sum(1 for label in labels if label in _HEADER_LABELS) >= 4:
            return row
    return None


def first_column_is_date(grid: Grid, body_rows: range, col: int) -> bool:
    """1열이 날짜인지 구분인지 — **내용으로** 판정한다.

    머리글 유무로 가르면 표 2에 머리글이 붙는 날 조용히 틀린다. 1열에 실제로
    무엇이 적혀 있는지 세는 편이 문서 편집에 훨씬 덜 민감하다.
    """
    dates = sections = 0
    for row in body_rows:
        cell = grid.cell(row, col)
        if cell is None:
            continue
        text = cell.squashed
        if not text:
            continue
        if _DATE_ANYWHERE_RE.search(text):
            dates += 1
        elif _known_section(text):
            sections += 1
    return dates > sections


def _known_section(squashed: str) -> str | None:
    try:
        return Section.from_text(squashed).value
    except ValueError:
        return None


def split_time_cell(cell: Cell | None) -> tuple[str | None, str]:
    """시간 셀 → (날짜 원문, 시간 원문).

    `9/5` / `(토)` / `14:00`처럼 날짜가 시간 칸에 끼어 있는 행이 실제로 있다(§3.6).
    """
    if cell is None:
        return None, ""
    date_text: str | None = None
    rest: list[str] = []
    for raw in cell.texts:
        line = raw.strip()
        if _WEEKDAY_ONLY_RE.match(line):
            continue
        match = _DATE_PREFIX_RE.match(line)
        if match:
            if date_text is None:
                date_text = f"{int(match.group(1))}/{int(match.group(2))}"
            line = line[match.end():].strip()
            if not line:
                continue
        rest.append(line)
    time_text = "".join(rest)
    #  "10:00" / "17:00"처럼 `~`가 빠진 칸도 범위로 읽는다
    two = _TWO_TIMES_RE.match(time_text)
    if two:
        time_text = f"{two.group(1)}~{two.group(2)}"
    return date_text, time_text


def split_date_cell(cell: Cell | None) -> str | None:
    """날짜 셀(`9/15` + `(화)`) → `9/15`. 요일은 버린다."""
    if cell is None:
        return None
    match = _DATE_PREFIX_RE.match(cell.joined(""))
    return f"{int(match.group(1))}/{int(match.group(2))}" if match else None


def split_title_cell(cell: Cell | None) -> tuple[str, str, bool]:
    """행사명 셀 → (제목, 부기, 강조).

    부기는 **글자 크기**로 가른다(§3.5). 원본에서 부기 줄은 예외 없이 본문보다
    작다. `*`·`※` 규칙은 글자모양을 못 읽었을 때만 쓰는 폴백이다 — 그것만으로는
    `(여자 개인전 DB, 육성종목)` 같은 줄을 제목에 섞어 버린다.
    """
    if cell is None:
        return "", "", False
    lines = cell.filled
    if not lines:
        return "", "", False

    head = lines[0]
    base_height = head.style.height
    body = [head.text]
    notes: list[str] = []
    for line in lines[1:]:
        smaller = base_height and line.style.height and line.style.height < base_height
        if smaller or line.text[:1] in _NOTE_LEAD:
            notes.append(line.text)
        else:
            body.append(line.text)
    return " ".join(body), " ".join(notes), head.style.is_highlight


def row_to_item(
    grid: Grid,
    row: int,
    cols: dict[int, int],
    *,
    table_index: int,
    col0_is_date: bool,
    warnings: list[str],
) -> ScheduleItem | None:
    """격자 한 행 → `ScheduleItem`. 행사명이 비면 빈 행으로 보고 버린다."""
    first = grid.cell(row, cols[SECTION_COL])
    title, note, highlight = split_title_cell(grid.cell(row, cols[TITLE_COL]))
    cell_date, time_text = split_time_cell(grid.cell(row, cols[TIME_COL]))

    if not title:
        if any(grid.text(row, cols[role]) for role in (TIME_COL, PLACE_COL, ATTEND_COL)):
            warnings.append(f"{table_index + 1}번 표 {row}행: 행사명이 비어 있어 건너뛴다")
        return None

    if col0_is_date:
        section = DEFAULT_SECTION
        date_text = split_date_cell(first)
        if cell_date and date_text and cell_date != date_text:
            warnings.append(
                f"{table_index + 1}번 표 {row}행: 날짜 칸({date_text})과 "
                f"시간 칸({cell_date})이 달라 시간 칸을 쓴다"
            )
        date_text = cell_date or date_text
    else:
        squashed = first.squashed if first else ""
        known = _known_section(squashed)
        if known is None:
            warnings.append(
                f"{table_index + 1}번 표 {row}행: 모르는 구분 {squashed!r} — 실 일정으로 둔다"
            )
        section = known or DEFAULT_SECTION
        date_text = cell_date

    if not time_text:
        warnings.append(f"{table_index + 1}번 표 {row}행: 시간 칸이 비어 종일로 둔다 — {title}")

    return ScheduleItem(
        section=section,
        date_text=date_text,
        time_text=time_text,
        title=title,
        location=grid.text(row, cols[PLACE_COL], " "),
        attendees=grid.text(row, cols[ATTEND_COL], ", "),
        department=grid.text(row, cols[DEPT_COL], ", "),
        row_index=row,
        note=note,
        highlight=highlight,
        table_index=table_index,
    )


def items_from_grid(
    grid: Grid, table_index: int, warnings: list[str]
) -> list[ScheduleItem]:
    """표 하나 → 항목들. 머리글 행은 건너뛴다."""
    header_row = find_header_row(grid)
    cols = column_map(grid, header_row)
    body = range(0 if header_row is None else header_row + 1, grid.n_rows)
    col0_is_date = first_column_is_date(grid, body, cols[SECTION_COL])

    items: list[ScheduleItem] = []
    for row in body:
        item = row_to_item(
            grid, row, cols,
            table_index=table_index, col0_is_date=col0_is_date, warnings=warnings,
        )
        if item is not None:
            items.append(item)
    return items
