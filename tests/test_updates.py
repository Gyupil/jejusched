"""새 버전 확인 — 설계서 §10(선택 구현).

**핵심 요구사항은 하나다: 무슨 일이 있어도 예외가 트레이까지 올라가지 않을 것.**
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from jejusched import __version__, updates


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def opener_returning(payload):
    def _open(request, timeout=None):  # noqa: ARG001
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    return _open


def opener_raising(exc):
    def _open(request, timeout=None):  # noqa: ARG001
        raise exc

    return _open


@pytest.mark.parametrize(
    "text, expected",
    [
        ("v0.1.0", (0, 1, 0)),
        ("0.1.0", (0, 1, 0)),
        ("v1.2.3", (1, 2, 3)),
        ("v2.0", (2, 0, 0)),
        ("v3", (3, 0, 0)),
        ("", (0, 0, 0)),
        ("최신", (0, 0, 0)),
    ],
)
def test_parse_version(text, expected):
    assert updates.parse_version(text) == expected


def test_newer_release_is_reported():
    result = updates.check(opener=opener_returning({"tag_name": "v9.9.9"}))
    assert result.latest == "v9.9.9"
    assert result.is_newer
    assert "새 버전" in result.message


def test_same_version_is_not_newer():
    result = updates.check(opener=opener_returning({"tag_name": f"v{__version__}"}))
    assert not result.is_newer
    assert "최신 버전" in result.message


def test_older_release_is_not_newer():
    result = updates.check(opener=opener_returning({"tag_name": "v0.0.1"}))
    assert not result.is_newer


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("네트워크 없음"),
        OSError("연결 거부"),
        TimeoutError("시간 초과"),
        ValueError("JSON 아님"),
        RuntimeError("생각 못한 오류"),  # 예상 못한 것도 삼켜야 한다
    ],
)
def test_network_failures_never_raise(exc):
    """**네트워크 실패가 트레이를 멈추면 안 된다**(완성 절차 1-5의 위험)."""
    result = updates.check(opener=opener_raising(exc))
    assert result.error is not None
    assert not result.is_newer
    assert "확인하지 못했습니다" in result.message


def test_missing_tag_name_is_an_error():
    result = updates.check(opener=opener_returning({"name": "태그 없음"}))
    assert result.error is not None
    assert not result.is_newer


def test_garbage_payload_is_an_error():
    def _open(request, timeout=None):  # noqa: ARG001
        return FakeResponse("<html>이건 JSON이 아니다</html>".encode("utf-8"))

    result = updates.check(opener=_open)
    assert result.error is not None


def test_release_url_comes_from_the_payload_when_present():
    result = updates.check(
        opener=opener_returning({"tag_name": "v9.9.9", "html_url": "https://example.test/r/9"})
    )
    assert result.url == "https://example.test/r/9"


def test_open_releases_page_swallows_failures(monkeypatch):
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: (_ for _ in ()).throw(RuntimeError("없음")))
    updates.open_releases_page()  # 예외가 새 나오면 실패다
