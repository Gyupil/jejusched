"""윈도우 자동 실행 — 설계서 §6.

실제 동작은 윈도우에서만 확인할 수 있다. 여기서는 **맥·CI에서도 지켜야 하는 것**을
지킨다: 모듈을 불러도 터지지 않을 것, 빌드되지 않은 상태에서 켜지지 않을 것,
끄기는 어디서든 안전할 것.
"""

from __future__ import annotations

import sys

import pytest

from jejusched import autostart


def test_importing_the_module_never_touches_windows_only_code():
    """맥에서 `jejusched.main`을 불러도 터지지 않아야 한다(CLAUDE.md 규칙)."""
    assert autostart.SHORTCUT_NAME.endswith(".lnk")


def test_not_available_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    ok, reason = autostart.available()
    assert not ok and "윈도우" in reason


def test_not_available_when_running_from_source(monkeypatch):
    """**중요**: 소스로 돌 때 `sys.executable`은 python.exe다. 바로가기를 만들면
    앱이 아니라 파이썬이 뜬다 — 그래서 막는다(완성 절차 4-3의 위험)."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(autostart, "is_frozen", lambda: False)
    ok, reason = autostart.available()
    assert not ok and "exe" in reason


def test_not_available_without_appdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(autostart, "is_frozen", lambda: True)
    monkeypatch.delenv("APPDATA", raising=False)
    ok, reason = autostart.available()
    assert not ok and "시작프로그램" in reason


def test_available_when_frozen_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(autostart, "is_frozen", lambda: True)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert autostart.available() == (True, "")
    assert autostart.shortcut_path() == (
        tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        / autostart.SHORTCUT_NAME
    )


def test_enable_refuses_when_unavailable():
    with pytest.raises(autostart.AutostartError):
        autostart.enable()


def test_is_enabled_reads_the_shortcut_on_disk(monkeypatch, tmp_path):
    """설정값이 아니라 **바로가기가 실제로 있는지**가 진실이다."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    link = autostart.shortcut_path()
    assert link is not None
    assert not autostart.is_enabled()

    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_bytes(b"")
    assert autostart.is_enabled()


def test_disable_removes_the_shortcut(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    link = autostart.shortcut_path()
    assert link is not None
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_bytes(b"")

    autostart.disable()
    assert not link.exists()


def test_disable_is_harmless_when_nothing_is_there():
    """맥에서도, 바로가기가 없어도 조용히 지나간다."""
    autostart.disable()
    autostart.apply(False)
