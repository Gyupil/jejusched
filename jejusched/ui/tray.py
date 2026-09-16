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


#  달력 색 — 트레이 아이콘과 exe 아이콘이 같은 그림을 쓴다(`tools/make_icon.py`)
BODY = (40, 96, 176, 255)
BAND = (28, 68, 128, 255)
RING = (210, 214, 220, 255)
CELL = (236, 240, 245, 255)


def _icon_image(size: int = 64):
    """의존성 없이 단색 달력 모양을 그린다.

    좌표를 전부 `size` 비율로 잡는다. 고정 픽셀로 그리면 256px .ico를 뽑거나
    고DPI 화면에서 크게 그릴 때 모양이 깨진다.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    u = size / 64  # 64px 기준으로 잡은 치수를 실제 크기로 옮기는 배율

    def box(*values):
        return [round(v * u) for v in values]

    draw.rounded_rectangle(box(4, 10, 60, 58), radius=max(1, round(8 * u)), fill=BODY)
    draw.rectangle(box(4, 10, 60, 24), fill=BAND)
    for x in (18, 44):  # 고리 두 개
        draw.rectangle(box(x - 3, 4, x + 3, 16), fill=RING)
    for row in range(2):
        for col in range(3):
            x, y = 12 + col * 14, 32 + row * 12
            draw.rectangle(box(x, y, x + 8, y + 7), fill=CELL)
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

    #  설정 창은 **한 번에 하나만** 띄운다.
    #  pystray가 메인 스레드를 쥔 상태에서 데몬 스레드에 Tk 루트를 또 만들면
    #  Tcl이 스레드에 예민해 두 번째부터 멈추거나 죽을 수 있다(완성 절차 4-2).
    settings_thread: list[threading.Thread] = []

    def open_settings(icon, item):  # noqa: ANN001, ARG001
        from .settings_window import open_settings_window

        if settings_thread and settings_thread[0].is_alive():
            log.info("설정 창이 이미 열려 있다")
            notify(APP_NAME, "설정 창이 이미 열려 있습니다")
            return
        thread = threading.Thread(
            target=open_settings_window, args=(config, state, auth, worker),
            name="jjsched-settings", daemon=True,
        )
        settings_thread[:] = [thread]
        thread.start()

    def check_updates(icon, item):  # noqa: ANN001, ARG001
        """설계서 §10 — 알려 주기만 하고 내려받지는 않는다."""
        def _run():
            from .. import updates

            result = updates.check()
            notify(APP_NAME, result.message)
            if result.is_newer:
                updates.open_releases_page(result.url)

        threading.Thread(target=_run, name="jjsched-update", daemon=True).start()

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
            pystray.MenuItem("새 버전 확인", check_updates),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", quit_app),
        ),
    )
    log.info("트레이 시작 — 감시 폴더 %s", config.watch_dir or "(미설정)")
    icon.run()
    return 0
