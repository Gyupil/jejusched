"""테스트·개발용 JSON 픽스처 파서.

`tools/pdf_to_fixture.py`가 만든 파일을 읽는다. 실제 hwpx 파서가 들어오기
전까지 파이프라인 전체를 이 파서로 돌린다.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.models import ParsedFile, ScheduleItem
from .base import ParseError

REQUIRED = ("file_date_month", "file_date_day", "file_weekday", "items")


class FixtureJsonParser:
    def parse(self, path: Path) -> ParsedFile:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ParseError(f"픽스처를 읽을 수 없다: {path}") from exc
        missing = [k for k in REQUIRED if k not in raw]
        if missing:
            raise ParseError(f"픽스처에 필드가 없다 {missing}: {path}")

        items = [
            ScheduleItem(
                section=row["section"],
                date_text=row.get("date_text"),
                time_text=row.get("time_text", ""),
                title=row.get("title", ""),
                location=row.get("location", ""),
                attendees=row.get("attendees", ""),
                department=row.get("department", ""),
                row_index=row.get("row_index", idx),
            )
            for idx, row in enumerate(raw["items"])
        ]
        return ParsedFile(
            file_date_month=int(raw["file_date_month"]),
            file_date_day=int(raw["file_date_day"]),
            file_weekday=str(raw["file_weekday"]),
            items=items,
            warnings=list(raw.get("warnings", [])),
        )
