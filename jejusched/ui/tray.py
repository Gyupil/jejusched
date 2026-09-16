"""트레이 상주 — 설계서 §6.

무거운 UI 라이브러리는 함수 안에서 임포트한다. 그래야 헤드리스 환경(CI, 맥
테스트)에서 `jejusched.main`을 불러도 터지지 않는다.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..config import APP_NAME, AppConfig, app_home
from ..core.state import State
from ..gcal.auth import Authenticator
from ..watcher.folder_watch import FolderWatcher
from .notify import notify, notify_error

log = logging.getLogger(__name__)


def _icon_image(size: int = 64):
    """의존성 없이 단색 달력 모양을 그린다 — .ico 파일을 들고 다니지 않아도 된다."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([4, 10, size - 4, size - 6], radius=8, fill=(40, 96, 176, 255))
    draw.rectangle([4, 10, size - 4, 24], fill=(28, 68, 128, 255))
    for x in (18, 44):  # 고리 두 개
        draw.rectangle([x - 3, 4, x + 3, 16], fill=(210, 214, 220, 255))
    for row in range(2):
        for col in range(3):
            x, y = 12 + col * 14, 32 + row * 12
            draw.rectangle([x, y, x + 8, y + 7], fill=(236, 240, 245, 255))
    return image


def run_tray() -> int:
    try:
        import pystray
    except ImportError:
        log.error("pystray가 없다 — `pip install .[ui]`로 UI 의존성을 설치하라")
        return 2

    config = AppConfig.load()
    state = State(app_home() / "state.db")
    auth = Authenticator(config)

    from ..main import build_worker

    #  최초 실행: 감시 폴더가 없으면 마법사부터 띄운다.
    #  워커는 **마법사가 끝난 뒤에** 만든다 — 그래야 마법사가 저장한 Gemini 키가
    #  build_resolver에 잡힌다(먼저 만들면 첫 동기화가 NullResolver로 돈다).
    if not config.watch_dir:
        from .wizard import run_wizard

        config = run_wizard(config, state, auth) or config

    worker, work_queue = build_worker(config, state, auth)

    watcher = FolderWatcher(Path(config.watch_dir or "."), config.file_glob, work_queue)
    watcher.start()

    thread = threading.Thread(target=worker.run_forever, name="jjsched-worker", daemon=True)
    thread.start()

    def open_settings(icon, item):  # noqa: ANN001, ARG001
        from .settings_window import open_settings_window

        threading.Thread(
            target=open_settings_window, args=(config, state, auth, worker), daemon=True
        ).start()

    def force_refresh(icon, item):  # noqa: ANN001, ARG001
        def _run():
            try:
                result = worker.force_refresh()
                if result is None:
                    notify(APP_NAME, "적용할 파일이 없다")
            except Exception as exc:  # noqa: BLE001
                notify_error("강제 새로고침 실패", str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def quit_app(icon, item):  # noqa: ANN001, ARG001
        worker.stop()
        watcher.stop()
        state.close()
        icon.stop()

    icon = pystray.Icon(
        APP_NAME,
        _icon_image(),
        "주요일정 동기화",
        menu=pystray.Menu(
            pystray.MenuItem("설정 열기", open_settings, default=True),
            pystray.MenuItem("지금 새로고침", force_refresh),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", quit_app),
        ),
    )
    log.info("트레이 시작 — 감시 폴더 %s", config.watch_dir or "(미설정)")
    icon.run()
    return 0
