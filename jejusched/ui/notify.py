"""알림 — 설계서 §4-[10].

윈도우는 토스트, 그 외(맥 개발 환경)는 로그로 대체한다.
"""

from __future__ import annotations

import logging
import sys

from ..config import APP_NAME

log = logging.getLogger(__name__)


def notify(title: str, message: str) -> None:
    if sys.platform == "win32":
        try:
            from windows_toasts import Toast, WindowsToaster  # type: ignore[import-not-found]

            toaster = WindowsToaster(APP_NAME)
            toast = Toast()
            toast.text_fields = [title, message]
            toaster.show_toast(toast)
            return
        except Exception as exc:  # noqa: BLE001 — 알림 실패가 동기화를 막으면 안 된다
            log.debug("토스트를 띄우지 못했다: %s", exc)
    log.info("[알림] %s — %s", title, message)


def notify_result(result) -> None:  # noqa: ANN001
    """처리 완료 요약."""
    totals = result.totals()
    notify(
        "주요일정 동기화 완료",
        f"추가 {totals['created']} · 덮어쓰기 {totals['updated'] + totals['restored']} · "
        f"표시 {totals['marked']}" + (f" · 보류 {totals['held']}" if totals["held"] else ""),
    )


def notify_error(what: str, detail: str = "") -> None:
    notify(f"주요일정 오류: {what}", detail)
