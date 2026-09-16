"""폴더 감시·워커 — 설계서 §4-[1], Force.

`folder_watch`에는 그동안 테스트가 하나도 없었다. 파일 잠금을 진짜로 재현하긴
어렵지만 `wait_until_settled`는 `sleep`/`now`를 주입받게 설계돼 있어 **가짜
시계로 전부 검증**할 수 있다. 실제로 기다리는 테스트는 하나도 없다.
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from jejusched.config import AppConfig
from jejusched.core.state import State
from jejusched.gcal.fake import FakeCalendarClient
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


# --------------------------------------------- 스레드 경계 (v0.1.0 회귀)

#  트레이는 메인 스레드에서 State를 만들고 워커·설정 창·강제 새로고침은 각자
#  다른 스레드에서 쓴다. 기존 테스트는 `Worker.process`를 **테스트 스레드에서
#  그대로** 불러서 이 경계를 한 번도 넘지 않았다 — 172개가 통과하는 동안
#  윈도우에서는 계정 추가 말고 아무것도 되지 않았다.


def test_state_is_usable_from_another_thread(tmp_path):
    """**v0.1.0을 망가뜨린 바로 그것.**

    `check_same_thread=False`가 빠지면 `ProgrammingError`로 죽는다.
    파일 DB여야 재현된다 — `:memory:`는 스레드 의미가 다르다.
    """
    state = State(tmp_path / "state.db")
    state.upsert_target("dev@example.com", "cal1", "주요일정(자동)")

    caught: list[BaseException] = []
    emails: list[str] = []

    def background():
        try:
            emails.extend(t.email for t in state.active_targets())
            state.mark_user_deleted(1, "key", when=date(2026, 9, 10))
            state.record_file(path="x", sha256="s", status="applied",
                              file_date=date(2026, 9, 7), mtime=0.0)
        except BaseException as exc:  # noqa: BLE001
            caught.append(exc)

    thread = threading.Thread(target=background)
    thread.start()
    thread.join()

    assert not caught, f"다른 스레드에서 State를 쓰지 못한다: {caught[0]!r}"
    assert emails == ["dev@example.com"]


@requires_fixtures
def test_worker_runs_in_a_background_thread(harness, tmp_path):
    """워커를 **진짜 스레드에서** 돌린다 — 트레이가 하는 그대로."""
    state = State(tmp_path / "state.db")
    client = FakeCalendarClient()
    cal = client.ensure_calendar("주요일정(자동)")
    state.upsert_target("dev@example.com", cal, "주요일정(자동)")

    from jejusched.core.pipeline import Pipeline
    from jejusched.llm.resolver import NullResolver

    config = AppConfig()
    config.watch_dir = str(tmp_path)
    config.file_glob = "*.json"
    pipeline = Pipeline(config, state, FixtureJsonParser(), client, NullResolver())
    deps = WorkerDeps(config=config, state=state, pipeline=pipeline,
                      intake=I.Intake(state, FixtureJsonParser(), config))
    worker = Worker(deps, queue.Queue())

    shutil.copy(fixture("0907"), tmp_path / "0907.json")
    _aged(tmp_path / "0907.json")

    result: list = []
    thread = threading.Thread(
        target=lambda: result.append(worker.process([tmp_path / "0907.json"])),
        name="jjsched-worker",
    )
    thread.start()
    thread.join(timeout=30)

    assert not thread.is_alive()
    assert result and result[0] is not None, "워커 스레드가 아무것도 적용하지 못했다"
    assert result[0].totals()["created"] == 63
    #  Journal이 남아야 설정 창의 "최근 처리"가 읽을 수 있다
    assert len(state.recent_runs()) == 1


def test_worker_loop_survives_an_unexpected_exception(tmp_path):
    """예외 하나로 감시가 영영 멈추면 안 된다 — 재시작 전까지 동기화가 죽는다."""
    state = State(tmp_path / "state.db")
    config = AppConfig()
    config.watch_dir = str(tmp_path)
    config.file_glob = "*.json"

    class Exploding(I.Intake):
        calls = 0

        def evaluate(self, paths, *, force=False):
            Exploding.calls += 1
            raise RuntimeError("생각 못한 오류")

    deps = WorkerDeps(config=config, state=state, pipeline=None,
                      intake=Exploding(state, FixtureJsonParser(), config))
    work_queue: "queue.Queue[Path]" = queue.Queue()
    worker = Worker(deps, work_queue)

    bad = tmp_path / "터지는파일.json"
    bad.write_text("{}", encoding="utf-8")
    _aged(bad)

    thread = threading.Thread(target=worker.run_forever, kwargs={"poll_seconds": 0.01},
                              name="jjsched-worker", daemon=True)
    thread.start()

    def wait_for(count: int) -> bool:
        for _ in range(300):
            if Exploding.calls >= count:
                return True
            time.sleep(0.01)
        return False

    #  시작 시 스캔이 파일을 물어 와 첫 예외가 터진다
    assert wait_for(1), "시작 시 스캔이 파일을 집지 못했다"
    #  **여기가 요점**: 첫 예외 뒤에도 루프가 살아 다음 파일을 처리한다
    work_queue.put(bad)
    assert wait_for(2), "첫 예외에서 워커 루프가 죽었다"
    assert thread.is_alive()

    worker.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


@requires_fixtures
def test_force_refresh_runs_in_a_background_thread(harness, tmp_path):
    """트레이의 "지금 새로고침"이 하는 그대로 — 별도 스레드에서 `force_refresh`.

    이 경로가 v0.1.0 로그를 설명한다. `evaluate`의 첫 DB 접근은
    `if not force and self.state.has_applied(...)`인데 **force면 파이썬이
    단락 평가로 건너뛴다.** 그래서 파싱까지는 가서 "63건 파싱"이 로그에 남고,
    바로 다음 줄 `max_applied_file_date()`에서 죽었다.
    평소 파일 투입(force=False)은 파싱 전에 죽어 로그에 아무것도 남기지 않는다 —
    사용자 로그에 파싱 줄이 **하나만** 있었던 이유다.
    """
    state = State(tmp_path / "state.db")
    client = FakeCalendarClient()
    cal = client.ensure_calendar("주요일정(자동)")
    state.upsert_target("dev@example.com", cal, "주요일정(자동)")

    from jejusched.core.pipeline import Pipeline
    from jejusched.llm.resolver import NullResolver

    config = AppConfig()
    config.watch_dir = str(tmp_path)
    config.file_glob = "*.json"
    pipeline = Pipeline(config, state, FixtureJsonParser(), client, NullResolver())
    deps = WorkerDeps(config=config, state=state, pipeline=pipeline,
                      intake=I.Intake(state, FixtureJsonParser(), config))
    worker = Worker(deps, queue.Queue())

    shutil.copy(fixture("0907"), tmp_path / "0907.json")
    _aged(tmp_path / "0907.json")

    result: list = []
    thread = threading.Thread(target=lambda: result.append(worker.force_refresh()),
                              name="jjsched-force")
    thread.start()
    thread.join(timeout=30)

    assert not thread.is_alive()
    assert result and result[0] is not None, "새로고침 스레드가 적용까지 가지 못했다"
    assert result[0].totals()["created"] == 63
    assert len(client.events[cal]) == 63


# ------------------------------------------------- doctor가 거짓말하지 않게

#  `doctor`의 `_why_not_interesting`은 `is_interesting`의 판정을 사람 말로 옮긴 것이다.
#  둘이 어긋나면 진단이 **거짓말을 한다** — 윈도우에서 그것만 보고 쫓게 되므로
#  가장 나쁜 종류의 버그다.


@pytest.mark.parametrize(
    "name, make",
    [
        ("0907_주요일정.hwpx", "file"),
        ("0907_주요일정 (3).hwpx", "file"),
        ("0907_주요일정.HWPX", "file"),
        ("~$0907_주요일정.hwpx", "file"),
        (".숨김.hwpx", "file"),
        ("0907.hwpx.tmp", "file"),
        ("0907.crdownload", "file"),
        ("0907.part", "file"),
        ("메모.txt", "file"),
        ("빈파일.hwpx", "empty"),
        ("폴더.hwpx", "dir"),
    ],
)
def test_doctor_agrees_with_is_interesting(tmp_path, name, make):
    from jejusched.main import _why_not_interesting

    path = tmp_path / name
    if make == "dir":
        path.mkdir()
    elif make == "empty":
        path.write_bytes(b"")
    else:
        path.write_bytes(b"x" * 100)

    accepted = is_interesting(path, "*.hwpx")
    reason = _why_not_interesting(path, "*.hwpx")
    assert accepted == (reason is None), (
        f"{name}: is_interesting={accepted} 인데 doctor는 {reason!r}라고 말한다"
    )


def test_doctor_explains_a_glob_with_stray_whitespace(tmp_path):
    """설정 창에 붙여 넣다 들어간 공백은 눈에 안 보이는데 전부 걸러 버린다."""
    from jejusched.main import _why_not_interesting

    path = tmp_path / "0907_주요일정.hwpx"
    path.write_bytes(b"x" * 100)
    assert not is_interesting(path, "*.hwpx ")
    assert "맞지 않는다" in (_why_not_interesting(path, "*.hwpx ") or "")
