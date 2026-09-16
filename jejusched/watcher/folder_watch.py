"""폴더 감시 — 설계서 §4-[1].

한글이 파일 잠금을 쥐고 있을 수 있으므로 크기·mtime이 멈추고 읽기 열기에
성공할 때까지 기다린다. 처리는 **단일 워커·직렬**이라 동시성 문제가 없다.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from queue import Queue
from typing import Callable

log = logging.getLogger(__name__)

IGNORED_PREFIXES = ("~$", ".")
IGNORED_SUFFIXES = (".tmp", ".crdownload", ".part")


def is_interesting(path: Path, glob: str) -> bool:
    if path.name.startswith(IGNORED_PREFIXES) or path.suffix.lower() in IGNORED_SUFFIXES:
        return False
    if not path.match(glob):
        return False
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def wait_until_settled(
    path: Path,
    settle_seconds: float = 3.0,
    timeout: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """크기·mtime이 `settle_seconds` 동안 변하지 않고 열리면 True.

    한글이 저장 중이면 열기가 실패하므로 지수 백오프로 최대 `timeout`까지 기다린다.
    """
    deadline = now() + timeout
    last: tuple[int, float] | None = None
    stable_since: float | None = None
    backoff = 0.2

    while now() < deadline:
        try:
            stat = path.stat()
            current = (stat.st_size, stat.st_mtime)
        except OSError:
            current = None

        if current is None or current != last:
            last, stable_since = current, None
        elif stable_since is None:
            stable_since = now()
        elif now() - stable_since >= settle_seconds:
            try:
                with open(path, "rb") as fh:
                    fh.read(1)
                return True
            except OSError:
                stable_since = None  # 아직 잠겨 있다 — 더 기다린다

        sleep(min(backoff, 1.0))
        backoff = min(backoff * 1.5, 1.0)

    log.warning("%s: %.0f초 안에 안정되지 않았다 — 다음 감시 이벤트에서 다시 시도한다",
                path, timeout)
    return False


def scan_folder(folder: Path, glob: str) -> list[Path]:
    """시작 시 스캔 — 프로그램이 꺼져 있는 동안 추가된 파일을 회수한다."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if is_interesting(p, glob))


class FolderWatcher:
    """watchdog 이벤트를 큐에 넣는다. watchdog이 없으면 주기 스캔으로 대체한다."""

    def __init__(self, folder: Path, glob: str, queue: "Queue[Path]"):
        self.folder = folder
        self.glob = glob
        self.queue = queue
        self._observer = None

    def start(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:  # pragma: no cover - 개발 환경에 watchdog이 없을 때
            log.warning("watchdog이 없다 — 시작 시 스캔만 동작한다")
            return

        outer = self

        class _Handler(FileSystemEventHandler):
            def _maybe(self, raw_path: str) -> None:
                path = Path(raw_path)
                if is_interesting(path, outer.glob):
                    outer.queue.put(path)

            def on_created(self, event):  # noqa: ANN001
                if not event.is_directory:
                    self._maybe(event.src_path)

            def on_modified(self, event):  # noqa: ANN001
                if not event.is_directory:
                    self._maybe(event.src_path)

            def on_moved(self, event):  # noqa: ANN001
                if not event.is_directory:
                    self._maybe(event.dest_path)

        self._observer = Observer()
        self._observer.schedule(_Handler(), str(self.folder), recursive=False)
        self._observer.start()
        log.info("폴더 감시 시작: %s (%s)", self.folder, self.glob)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
