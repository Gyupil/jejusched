"""폴더 감시·워커 — 설계서 §4-[1], Force.

`folder_watch`에는 그동안 테스트가 하나도 없었다. 파일 잠금을 진짜로 재현하긴
어렵지만 `wait_until_settled`는 `sleep`/`now`를 주입받게 설계돼 있어 **가짜
시계로 전부 검증**할 수 있다. 실제로 기다리는 테스트는 하나도 없다.
"""

from __future__ import annotations

import os
import queue
import shutil
import time
from datetime import date
from pathlib import Path

import pytest

from jejusched.config import AppConfig
from jejusched.core.state import State
from jejusched.parsers.fixture_json import FixtureJsonParser
from jejusched.watcher import intake as I
from jejusched.watcher.folder_watch import is_interesting, scan_folder, wait_until_settled
from jejusched.watcher.worker import SETTLED_AGE_SECONDS, Worker, WorkerDeps

from conftest import FIXTURES, fixture, requires_fixtures


# ------------------------------------------------------------- 가짜 시계


class FakeClock:
    """`wait_until_settled`에 넣을 단조 시계. `sleep`이 곧 시간의 흐름이다."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class GrowingFile:
    """`stat()`을 부를 때마다 커지는 척하는 경로. 저장 중인 파일을 흉내 낸다."""

    def __init__(self, path: Path, grow_times: int):
        self.path = path
        self.remaining = grow_times
        self.size = 100

    def stat(self):
        if self.remaining > 0:
            self.remaining -= 1
            self.size += 100
        return os.stat_result((0, 0, 0, 0, 0, 0, self.size, 0, int(self.size), 0))


def _real_file(tmp_path: Path, name: str = "a.hwpx", data: bytes = b"x" * 100) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ------------------------------------------------- wait_until_settled


def test_settles_immediately_when_the_file_is_already_quiet(tmp_path):
    path = _real_file(tmp_path)
    clock = FakeClock()
    assert wait_until_settled(path, settle_seconds=3.0, sleep=clock.sleep, now=clock.now)
    #  실제로 잔 시간은 0이다 — 가짜 시계가 흐름을 대신했다
    assert sum(clock.slept) >= 3.0


def test_waits_while_the_size_keeps_changing(tmp_path):
    """저장 중이라 크기가 계속 변하면 통과시키지 않는다."""
    path = _real_file(tmp_path)
    clock = FakeClock()
    growing = GrowingFile(path, grow_times=10_000)  # 끝없이 커진다

    assert not wait_until_settled(
        growing, settle_seconds=3.0, timeout=30.0, sleep=clock.sleep, now=clock.now
    )


def test_passes_once_the_size_stops_changing(tmp_path):
    path = _real_file(tmp_path)
    clock = FakeClock()
    growing = GrowingFile(path, grow_times=3)  # 세 번 커지고 멈춘다
    growing.path = path

    class Stabilising:
        def __init__(self, inner, real):
            self.inner, self.real = inner, real

        def stat(self):
            return self.inner.stat()

        def __fspath__(self):
            return str(self.real)

    assert wait_until_settled(
        Stabilising(growing, path), settle_seconds=3.0, timeout=60.0,
        sleep=clock.sleep, now=clock.now,
    )


def test_times_out_when_the_file_can_never_be_opened(tmp_path):
    """한/글이 잠금을 놓지 않으면 타임아웃하고 다음 이벤트를 기다린다."""
    missing = tmp_path / "없는파일.hwpx"  # stat이 계속 실패한다
    clock = FakeClock()
    assert not wait_until_settled(
        missing, settle_seconds=3.0, timeout=20.0, sleep=clock.sleep, now=clock.now
    )
    assert clock.now() >= 1020.0  # timeout만큼은 기다렸다


def test_locked_file_keeps_waiting_then_gives_up(tmp_path, monkeypatch):
    """크기는 멈췄는데 **열기가 계속 실패**하는 상태 — 잠금이 풀리지 않은 경우."""
    path = _real_file(tmp_path)
    clock = FakeClock()

    import builtins

    real_open = builtins.open

    def refuse(file, *args, **kwargs):
        if Path(file) == path:
            raise PermissionError("잠겨 있다")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", refuse)
    assert not wait_until_settled(
        path, settle_seconds=3.0, timeout=30.0, sleep=clock.sleep, now=clock.now
    )


def test_backoff_never_exceeds_one_second(tmp_path):
    path = _real_file(tmp_path)
    clock = FakeClock()
    wait_until_settled(
        GrowingFile(path, 10_000), settle_seconds=3.0, timeout=30.0,
        sleep=clock.sleep, now=clock.now,
    )
    assert clock.slept and max(clock.slept) <= 1.0


# ----------------------------------------------------- is_interesting


@pytest.mark.parametrize(
    "name, expected",
    [
        ("0907_주요일정.hwpx", True),
        ("~$0907_주요일정.hwpx", False),   # 한/글 임시 파일
        (".0907_주요일정.hwpx", False),    # 숨김 파일
        ("0907_주요일정.hwpx.tmp", False),
        ("0907_주요일정.crdownload", False),
        ("0907_주요일정.part", False),
        ("메모.txt", False),               # glob에 안 맞는다
    ],
)
def test_is_interesting_filters_by_name(tmp_path, name, expected):
    path = _real_file(tmp_path, name)
    assert is_interesting(path, "*.hwpx") is expected


def test_empty_file_is_not_interesting(tmp_path):
    """저장이 막 시작된 0바이트 파일은 아직 볼 것이 없다."""
    path = _real_file(tmp_path, "0907_주요일정.hwpx", data=b"")
    assert not is_interesting(path, "*.hwpx")


def test_directory_is_not_interesting(tmp_path):
    sub = tmp_path / "하위.hwpx"
    sub.mkdir()
    assert not is_interesting(sub, "*.hwpx")


def test_scan_folder_returns_sorted_matches(tmp_path):
    for name in ("0910_주요일정.hwpx", "0907_주요일정.hwpx", "메모.txt", "~$x.hwpx"):
        _real_file(tmp_path, name)
    assert [p.name for p in scan_folder(tmp_path, "*.hwpx")] == [
        "0907_주요일정.hwpx", "0910_주요일정.hwpx"
    ]


def test_scan_folder_on_a_missing_directory_is_empty(tmp_path):
    assert scan_folder(tmp_path / "없음", "*.hwpx") == []


# ------------------------------------------------------ Force 날짜 범위


def _aged(path: Path) -> Path:
    """안정화를 기다리지 않도록 파일을 충분히 오래된 것으로 만든다."""
    old = time.time() - SETTLED_AGE_SECONDS * 10
    os.utime(path, (old, old))
    return path


def _worker(h, tmp_path) -> Worker:
    config = AppConfig()
    config.watch_dir = str(tmp_path)
    config.file_glob = "*.json"
    deps = WorkerDeps(
        config=config, state=h.state, pipeline=h.pipeline,
        intake=I.Intake(h.state, FixtureJsonParser(), config),
    )
    return Worker(deps, queue.Queue())


@requires_fixtures
def test_force_only_clears_user_deleted_inside_the_file_window(harness, tmp_path):
    """**완성 절차 1-1.** 9월 파일을 force해도 8월에 직접 지운 일정은 되살아나지 않는다.

    범위 없이 지우면 사용자가 일부러 지운 것을 프로그램이 되돌리는 셈이 된다.
    """
    h = harness()
    h.apply("0907")
    target_id = h.targets[0].id

    #  파일 범위 밖(8월)과 안(9/10)에 직접 삭제 기록을 하나씩 심는다
    h.state.mark_user_deleted(target_id, "key-8월", when=date(2026, 8, 15))
    h.state.mark_user_deleted(target_id, "key-9월", when=date(2026, 9, 10))

    shutil.copy(fixture("0907"), tmp_path / "0907.json")
    source = _aged(tmp_path / "0907.json")
    _worker(h, tmp_path).force_refresh(source, restore_deleted=True)

    remaining = h.state.user_deleted_keys(target_id)
    assert "key-8월" in remaining, "파일 범위 밖의 직접 삭제 기록이 지워졌다"
    assert "key-9월" not in remaining, "파일 범위 안의 기록은 지워져야 한다"


@requires_fixtures
def test_force_without_the_option_keeps_every_user_deleted_record(harness, tmp_path):
    h = harness()
    h.apply("0907")
    target_id = h.targets[0].id
    h.state.mark_user_deleted(target_id, "key-9월", when=date(2026, 9, 10))

    shutil.copy(fixture("0907"), tmp_path / "0907.json")
    _aged(tmp_path / "0907.json")
    _worker(h, tmp_path).force_refresh(tmp_path / "0907.json")

    assert "key-9월" in h.state.user_deleted_keys(target_id)


@requires_fixtures
def test_candidate_window_matches_the_pipeline_window(tmp_path):
    """Force가 쓰는 범위는 파이프라인이 캘린더를 조회하는 범위와 **같아야** 한다."""
    from jejusched.core.normalize import normalize
    from jejusched.core.pipeline import Pipeline

    shutil.copy(FIXTURES / "0907.json", tmp_path / "0907.json")
    state = State()
    config = AppConfig()
    intake = I.Intake(state, FixtureJsonParser(), config)
    chosen = intake.evaluate([tmp_path / "0907.json"]).chosen
    assert chosen is not None

    parsed = FixtureJsonParser().parse(tmp_path / "0907.json")
    file_date, events, _, _ = normalize(parsed, 2026)
    assert chosen.window() == Pipeline._window(events, file_date)


def test_candidate_window_falls_back_to_the_file_date():
    """일정 날짜를 모르면 파일 날짜 하루로 좁힌다 — 전체를 지우지 않는다."""
    cand = I.Candidate(path=Path("x"), sha256="", mtime=0.0, file_date=date(2026, 9, 7))
    assert cand.window() == (date(2026, 9, 6), date(2026, 9, 8))


def test_candidate_window_is_none_when_nothing_is_known():
    cand = I.Candidate(path=Path("x"), sha256="", mtime=0.0)
    assert cand.window() is None


# ------------------------------------------------------- 캘린더 삭제 옵션


def test_fake_client_deletes_a_calendar():
    from jejusched.gcal.fake import FakeCalendarClient

    client = FakeCalendarClient()
    cal = client.ensure_calendar("주요일정(자동)")
    assert cal in client.calendars

    client.delete_calendar(cal)
    assert cal not in client.calendars
    assert cal not in client.events
    assert ("delete_calendar", cal) in client.calls


def test_google_client_refuses_to_delete_the_primary_calendar():
    """`primary`는 이 앱이 만든 캘린더가 아니다 — 실수로도 지워지면 안 된다."""
    from jejusched.gcal.client import CalendarError, GoogleCalendarClient

    client = GoogleCalendarClient.__new__(GoogleCalendarClient)  # 인증 없이 메서드만 본다
    for bad in ("primary", ""):
        with pytest.raises(CalendarError):
            client.delete_calendar(bad)
