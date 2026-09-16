"""로그 설정 — 설계서 §4-[10]. 일별 파일 30일 보관."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import app_home

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
BACKUP_DAYS = 30


def log_dir() -> Path:
    return app_home() / "logs"


def setup(level: int = logging.INFO, *, to_console: bool = True) -> Path:
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "jejusched.log"

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        path, when="midnight", backupCount=BACKUP_DAYS, encoding="utf-8"
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(file_handler)

    #  윈도우 GUI 빌드(windowed)에는 stdout이 없다
    if to_console and sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(console)

    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    return path
