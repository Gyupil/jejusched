"""윈도우 자동 실행 — 설계서 §6.

시작프로그램 폴더(`shell:startup`)에 exe 바로가기를 놓거나 지운다. 레지스트리
`Run` 키가 아니라 폴더를 쓰는 이유는 사용자가 **탐색기에서 직접 확인하고 지울 수
있기** 때문이다. 백신 오탐도 덜하다.

빌드되지 않은 상태(`python -m jejusched`)에서는 `sys.executable`이 python.exe라
바로가기를 만들어도 앱이 뜨지 않는다. 그래서 `available()`이 먼저 막는다.

윈도우 전용 임포트는 **함수 안에서** 한다 — 맥·CI에서 이 모듈을 불러도 터지지
않아야 한다.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .config import APP_NAME, is_frozen

log = logging.getLogger(__name__)

SHORTCUT_NAME = f"{APP_NAME}.lnk"


class AutostartError(RuntimeError):
    """바로가기를 만들거나 지우지 못했을 때."""


def startup_dir() -> Path | None:
    """`%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup`."""
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path | None:
    folder = startup_dir()
    return folder / SHORTCUT_NAME if folder else None


def available() -> tuple[bool, str]:
    """켤 수 있는 상태인가 — (가능 여부, 이유).

    이유 문자열은 설정 창이 체크박스 옆에 그대로 띄운다.
    """
    if sys.platform != "win32":
        return False, "자동 실행은 윈도우에서만 됩니다."
    if not is_frozen():
        return False, "빌드된 exe에서만 켤 수 있습니다(지금은 소스로 실행 중)."
    if startup_dir() is None:
        return False, "시작프로그램 폴더를 찾지 못했습니다."
    return True, ""


def is_enabled() -> bool:
    path = shortcut_path()
    return bool(path and path.is_file())


def enable() -> Path:
    """시작프로그램 폴더에 exe 바로가기를 만든다."""
    ok, reason = available()
    if not ok:
        raise AutostartError(reason)
    path = shortcut_path()
    assert path is not None
    target = Path(sys.executable)

    try:
        from win32com.client import Dispatch  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 윈도우 전용
        raise AutostartError("pywin32가 없어 바로가기를 만들 수 없습니다") from exc

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        shell = Dispatch("WScript.Shell")
        link = shell.CreateShortCut(str(path))
        link.TargetPath = str(target)
        link.WorkingDirectory = str(target.parent)
        link.IconLocation = str(target)
        link.Description = "주요일정 hwpx를 Google Calendar에 자동 반영"
        link.save()
    except Exception as exc:  # noqa: BLE001 — COM은 무엇이든 던진다
        raise AutostartError(f"바로가기를 만들지 못했습니다: {exc}") from exc

    log.info("자동 실행 켬: %s → %s", path, target)
    return path


def disable() -> None:
    """바로가기를 지운다. 없으면 아무 일도 하지 않는다."""
    path = shortcut_path()
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise AutostartError(f"바로가기를 지우지 못했습니다: {exc}") from exc
    log.info("자동 실행 끔: %s", path)


def apply(enabled: bool) -> None:
    """설정값에 맞춘다. 끄는 쪽은 어디서든 안전하게 부를 수 있다."""
    if enabled:
        enable()
    else:
        disable()
