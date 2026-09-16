"""알림 — 설계서 §4-[10].

윈도우는 토스트, 그 외(맥 개발 환경)는 로그로 대체한다.
"""

from __future__ import annotations

import logging
import sys

from ..config import APP_NAME

log = logging.getLogger(__name__)

#  토스트가 안 뜨는 환경(미등록 AUMID 등)에서 같은 경고를 매번 남기지 않는다
_toast_warned = False


def notify(title: str, message: str) -> None:
    global _toast_warned

    if sys.platform == "win32":
        try:
            from windows_toasts import Toast, WindowsToaster  # type: ignore[import-not-found]

            toaster = WindowsToaster(APP_NAME)
            toast = Toast()
            toast.text_fields = [title, message]
            toaster.show_toast(toast)
            return
        except Exception as exc:  # noqa: BLE001 — 알림 실패가 동기화를 막으면 안 된다
            #  **처음 한 번은 WARNING으로 남긴다.** DEBUG로 묻어 두면 "토스트가 안
            #  뜰 뿐인데 기능이 죽은 줄 아는" 상황을 로그로 구분할 수 없다.
            if not _toast_warned:
                _toast_warned = True
                log.warning(
                    "토스트를 띄우지 못한다 — 알림은 로그로만 남는다 (%s: %s)",
                    type(exc).__name__, exc,
                )
            else:
                log.debug("토스트를 띄우지 못했다: %s", exc)
    #  토스트가 안 되더라도 **무슨 일이 있었는지는 로그에 반드시 남는다**
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
