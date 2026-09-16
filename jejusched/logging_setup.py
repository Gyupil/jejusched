"""로그 설정 — 설계서 §4-[10]. 일별 파일 30일 보관."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
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
    install_thread_excepthook()
    return path


def install_thread_excepthook() -> None:
    """스레드에서 터진 예외를 **로그 파일로** 끌어낸다.

    기본 훅은 `sys.stderr`에 쓴다. 그런데 윈도우 GUI 빌드(`console=False`)에는
    stderr가 없어서(None) 훅이 아무 데도 남기지 못한다. v0.1.0에서 워커·설정
    창·새로고침 스레드가 전부 예외로 죽었는데 **로그가 깨끗했던** 이유가 이것이다.
    같은 일이 다시 일어나면 이번엔 파일에 남는다.
    """
    def _hook(args: threading.ExceptHookArgs) -> None:  # pragma: no cover - 스레드 훅
        if args.exc_type is SystemExit:
            return
        logging.getLogger(__name__).error(
            "스레드 %s가 예외로 끝났다", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _hook
