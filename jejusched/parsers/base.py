"""파서 인터페이스. hwpx 파서(M8)는 이 Protocol만 만족하면 끼워 넣을 수 있다."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..core.models import ParsedFile


@runtime_checkable
class Parser(Protocol):
    def parse(self, path: Path) -> ParsedFile: ...


class ParseError(ValueError):
    """파일을 해석할 수 없을 때. 호출자는 파일을 `failed`로 기록하고 손대지 않는다."""
