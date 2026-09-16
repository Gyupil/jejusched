"""파일 확장자로 파서를 고른다."""

from __future__ import annotations

from pathlib import Path

from .base import Parser
from .fixture_json import FixtureJsonParser
from .hwpx import HwpxParser

__all__ = ["Parser", "FixtureJsonParser", "HwpxParser", "for_path"]


def for_path(path: Path) -> Parser:
    if path.suffix.lower() == ".json":
        return FixtureJsonParser()
    return HwpxParser()


class DispatchingParser:
    """경로마다 알맞은 파서에 넘긴다. 파이프라인은 이것 하나만 쥐면 된다."""

    def parse(self, path: Path):  # noqa: ANN201
        return for_path(Path(path)).parse(Path(path))
