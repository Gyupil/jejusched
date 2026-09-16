"""샘플 PDF(docs/example_docs/*.pdf) → tests/fixtures/*.json 변환기.

설계서 §3 파서 계약대로 ScheduleItem 배열을 만든다. PDF는 배포 대상이 아니고
hwpx 파서가 들어오면 쓰이지 않지만, 픽스처를 손으로 옮기지 않고 재생성할 수
있게 남겨 둔다. 실행: python tools/pdf_to_fixture.py

주의: PyMuPDF(fitz)가 필요하다. `uv pip install pymupdf` (개발 전용 의존성).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pymupdf  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "example_docs"
DST = ROOT / "tests" / "fixtures"

HEADER_RE = re.compile(r"【\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*([월화수목금토일])\s*요\s*일\s*】")
DATE_CELL_RE = re.compile(r"^(\d{1,2})\s*/\s*(\d{1,2})\s*\(\s*([월화수목금토일])\s*\)")
# 시간 셀이 "9/5\n(토)\n14:00" 처럼 날짜를 함께 담은 경우
TIME_WITH_DATE_RE = re.compile(
    r"^(\d{1,2})\s*/\s*(\d{1,2})\s*\(\s*([월화수목금토일])\s*\)\s*(.*)$", re.S
)

SECTIONS = {"도지사": "도지사", "행정부지사": "행정부지사", "기후경제부지사": "기후경제부지사", "실일정": "실 일정"}


def squash(s: str | None) -> str:
    """셀 안의 줄바꿈을 지우고 공백을 하나로 줄인다(구분·시간 셀 판별용)."""
    return re.sub(r"\s+", "", s or "")


def clean(s: str | None) -> str:
    """표시용 정리: 줄 단위로 trim 하고 빈 줄은 버리되 줄바꿈은 보존한다."""
    lines = [ln.strip() for ln in (s or "").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def norm_time(cell: str) -> tuple[str | None, str]:
    """시간 셀 → (date_text, time_text)."""
    text = clean(cell)
    date_text = None
    m = TIME_WITH_DATE_RE.match(text.replace("\n", ""))
    if m:
        date_text = f"{int(m.group(1))}/{int(m.group(2))}({m.group(3)})"
        text = m.group(4)
    # "10:00\n~\n17:00" → "10:00~17:00"
    return date_text, re.sub(r"\s+", "", text)


def parse_pdf(path: Path) -> dict:
    doc = pymupdf.open(path)
    header = None
    rows: list[list[str]] = []
    for page in doc:
        if header is None:
            m = HEADER_RE.search(page.get_text())
            if m:
                header = (int(m.group(1)), int(m.group(2)), m.group(3))
        for table in page.find_tables().tables:
            for cells in table.extract():
                cells = list(cells) + [""] * (6 - len(cells))
                if squash(cells[0]) == "구분" and squash(cells[1]) == "시간":
                    continue  # 표 머리글
                # 페이지가 갈리며 잘린 행은 시간 셀이 비어 있다. 실제 일정 행은
                # 예외 없이 시간("10:00"/"종일")을 갖고 있으므로 이것으로 가른다.
                if rows and not squash(cells[1]):
                    for i in (2, 3, 4, 5):
                        extra = clean(cells[i])
                        if extra:
                            rows[-1][i] = "\n".join(filter(None, [rows[-1][i], extra]))
                    continue
                rows.append([clean(c) for c in cells])

    if header is None:
        raise ValueError(f"헤더(【 N월 N일 X요일 】)를 찾지 못했다: {path}")

    items = []
    section: str | None = None
    block_date: str | None = None  # 미래 날짜 블록의 현재 날짜
    for idx, cells in enumerate(rows):
        first = squash(cells[0])
        if first in SECTIONS:
            section, block_date = SECTIONS[first], None
        elif DATE_CELL_RE.match(first):
            # 미래 날짜 블록 진입 — 설계서 §11-1: 이 블록은 전부 "실 일정"
            m = DATE_CELL_RE.match(first)
            assert m
            section = "실 일정"
            block_date = f"{int(m.group(1))}/{int(m.group(2))}({m.group(3)})"
        if section is None:
            raise ValueError(f"구분을 확정하지 못한 행: {cells}")

        cell_date, time_text = norm_time(cells[1])
        items.append(
            {
                "section": section,
                "date_text": cell_date or block_date,
                "time_text": time_text,
                "title": cells[2],
                "location": cells[3],
                "attendees": cells[4],
                "department": cells[5],
                "row_index": idx,
            }
        )

    month, day, weekday = header
    return {
        "file_date_month": month,
        "file_date_day": day,
        "file_weekday": weekday,
        "items": items,
        "warnings": [],
    }


def main() -> int:
    if not SRC.is_dir():
        print(f"샘플 PDF 폴더가 없다: {SRC}", file=sys.stderr)
        return 1
    DST.mkdir(parents=True, exist_ok=True)
    for pdf in sorted(SRC.glob("*.pdf")):
        stem = pdf.stem.split("_")[0]  # "0907_주요일정" → "0907"
        parsed = parse_pdf(pdf)
        out = DST / f"{stem}.json"
        out.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{out.relative_to(ROOT)}: {len(parsed['items'])} items")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
