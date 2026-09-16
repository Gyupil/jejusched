"""새 버전 확인 — 설계서 §10(선택 구현).

GitHub Releases의 `tag_name`을 현재 버전과 견준다. 자동으로 내려받거나 설치하지
않는다. 릴리스 페이지를 열어 줄 뿐이다.

**네트워크 실패가 트레이를 멈추면 안 된다.** 이 모듈의 모든 함수는 예외를 밖으로
내보내지 않고 `UpdateCheck`에 담아 돌려준다.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import __version__

log = logging.getLogger(__name__)

REPO = "Gyupil/jejusched"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
TIMEOUT_SECONDS = 6.0

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass
class UpdateCheck:
    """확인 결과. `error`가 있으면 나머지는 믿지 않는다."""

    current: str
    latest: str | None = None
    url: str = RELEASES_URL
    error: str | None = None

    @property
    def is_newer(self) -> bool:
        if self.error or not self.latest:
            return False
        return parse_version(self.latest) > parse_version(self.current)

    @property
    def message(self) -> str:
        """사용자에게 그대로 보여 줄 한 줄."""
        if self.error:
            return f"새 버전을 확인하지 못했습니다: {self.error}"
        if self.is_newer:
            return f"새 버전 {self.latest}이 있습니다 (지금 {self.current})."
        return f"최신 버전입니다 ({self.current})."


def parse_version(text: str) -> tuple[int, int, int]:
    """`v0.2.1` / `0.2` → (0, 2, 1). 못 읽으면 (0, 0, 0)."""
    match = _VERSION_RE.search(text or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(g) if g else 0 for g in match.groups())  # type: ignore[return-value]


def check(*, timeout: float = TIMEOUT_SECONDS, opener=urllib.request.urlopen) -> UpdateCheck:
    """최신 릴리스 태그를 읽는다. **무슨 일이 있어도 예외를 던지지 않는다.**"""
    result = UpdateCheck(current=__version__)
    request = urllib.request.Request(
        API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"jejusched/{__version__}"},
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        result.error = str(exc)
        log.info("새 버전 확인 실패: %s", exc)
        return result
    except Exception as exc:  # noqa: BLE001 — 트레이를 멈추느니 삼킨다
        result.error = str(exc)
        log.warning("새 버전 확인 중 예상 못한 오류: %s", exc)
        return result

    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    if not tag:
        result.error = "릴리스 정보에 tag_name이 없습니다"
        return result
    result.latest = str(tag)
    result.url = str(payload.get("html_url") or RELEASES_URL)
    return result


def open_releases_page(url: str = RELEASES_URL) -> None:
    """브라우저로 릴리스 페이지를 연다. 실패해도 조용히 넘어간다."""
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("릴리스 페이지를 열지 못했다: %s", exc)
