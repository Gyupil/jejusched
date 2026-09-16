"""진단 CLI — 격자를 눈으로 확인한다.

    python -m jejusched.parsers.hwpx.dump 0907_주요일정.hwpx
    python -m jejusched.parsers.hwpx.dump 0907_주요일정.hwpx --grid

표 구조가 파일마다 미묘하게 다를 수 있어(사람이 손으로 만드는 문서다) 새 파일이
안 읽힐 때 **먼저 여기를 돌려 격자를 본다.** 구분이 밀렸는지가 한눈에 보인다.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .charpr import load_char_styles
from .container import open_hwpx
from .extract import find_header_row
from . import HwpxParser, _top_tables
from .table import build_grid


def _print_grid(path: Path, width: int) -> None:
    container = open_hwpx(path)
    styles = load_char_styles(container.header)
    warnings: list[str] = []
    for index, tbl in enumerate(
        [t for section in container.sections for t in _top_tables(section)]
    ):
        grid = build_grid(tbl, styles, warnings)
        header_row = find_header_row(grid)
        print(f"\n=== {index + 1}번 표 — {grid.n_rows}행 × {grid.n_cols}열 "
              f"(머리글 {header_row if header_row is not None else '없음'}) ===")
        for row in range(grid.n_rows):
            cells = []
            for col in range(grid.n_cols):
                cell = grid.cell(row, col)
                text = cell.joined("/") if cell else ""
                merged = "^" if cell and cell.row != row else " "
                cells.append(f"{merged}{text[:width]:<{width}}")
            print(f"{row:>3} |" + "|".join(cells))
    for line in warnings:
        print("경고:", line)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="hwpx 파싱 결과를 들여다본다")
    ap.add_argument("path", type=Path)
    ap.add_argument("--grid", action="store_true", help="격자를 그대로 출력한다(병합 확인용)")
    ap.add_argument("--json", action="store_true", help="ParsedFile을 JSON으로 출력한다")
    ap.add_argument("--width", type=int, default=18, help="격자 칸 너비")
    args = ap.parse_args(argv)

    if args.grid:
        _print_grid(args.path, args.width)
        return 0

    parsed = HwpxParser().parse(args.path)
    if args.json:
        print(json.dumps(
            {
                "file_date_month": parsed.file_date_month,
                "file_date_day": parsed.file_date_day,
                "file_weekday": parsed.file_weekday,
                "items": [asdict(i) for i in parsed.items],
                "warnings": parsed.warnings,
            },
            ensure_ascii=False, indent=1,
        ))
        return 0

    print(f"{args.path.name} — {parsed.file_date_month}/{parsed.file_date_day}"
          f"({parsed.file_weekday}) · {len(parsed.items)}건")
    for name, count in sorted(Counter(i.section for i in parsed.items).items()):
        print(f"  {name}: {count}")
    for index, item in enumerate(parsed.items):
        note = f"  ※{item.note}" if item.note else ""
        star = "★" if item.highlight else " "
        print(f"{index:>3}{star}[{item.section}] {item.date_text or '-':>6} "
              f"{item.time_text:<12} {item.title}{note}")
    for line in parsed.warnings:
        print("경고:", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
