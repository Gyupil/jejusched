"""Intake/Gate — 설계서 §4-[2]."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from jejusched.config import AppConfig
from jejusched.core.state import State
from jejusched.parsers.fixture_json import FixtureJsonParser
from jejusched.watcher import intake as I

from conftest import requires_fixtures, FIXTURES

pytestmark = requires_fixtures


@pytest.fixture
def folder(tmp_path):
    for name in ("0907", "0908", "0909", "0910"):
        shutil.copy(FIXTURES / f"{name}.json", tmp_path / f"{name}.json")
    #  mtime을 파일 날짜 순서대로 벌려 둔다
    for i, name in enumerate(("0907", "0908", "0909", "0910")):
        path = tmp_path / f"{name}.json"
        import os
        os.utime(path, (1_790_000_000 + i * 86400, 1_790_000_000 + i * 86400))
    return tmp_path


@pytest.fixture
def gate():
    state = State()
    return I.Intake(state, FixtureJsonParser(), AppConfig()), state


def test_batch_reduction_applies_only_the_newest(gate, folder):
    """첫 실행 백로그: 최신 파일 1개만 적용하고 나머지는 skipped_superseded."""
    intake, _ = gate
    result = intake.evaluate(sorted(folder.glob("*.json")))

    assert result.chosen is not None
    assert result.chosen.path.name == "0910.json"
    assert result.chosen.file_date == date(2026, 9, 10)
    assert {c.path.name: c.status for c in result.rejected} == {
        "0907.json": I.SKIPPED_SUPERSEDED,
        "0908.json": I.SKIPPED_SUPERSEDED,
        "0909.json": I.SKIPPED_SUPERSEDED,
    }


def test_already_applied_hash_is_skipped(gate, folder):
    intake, state = gate
    path = folder / "0907.json"
    state.record_file(str(path), I.sha256_of(path), I.APPLIED, date(2026, 9, 7))

    result = intake.evaluate([path])
    assert result.chosen is None
    assert result.rejected[0].status == I.SKIPPED_DUP


def test_force_ignores_duplicate_and_stale_rules(gate, folder):
    intake, state = gate
    path = folder / "0907.json"
    state.record_file(str(path), I.sha256_of(path), I.APPLIED, date(2026, 9, 10))

    assert intake.evaluate([path]).chosen is None
    assert intake.evaluate([path], force=True).chosen is not None


def test_older_file_arriving_late_is_stale(gate, folder):
    intake, state = gate
    newest = folder / "0910.json"
    state.record_file(str(newest), I.sha256_of(newest), I.APPLIED, date(2026, 9, 10))

    result = intake.evaluate([folder / "0908.json"])
    assert result.chosen is None
    assert result.rejected[0].status == I.SKIPPED_STALE


def test_same_date_resave_is_processed(gate, folder):
    """같은 날짜의 재저장본은 정본으로 다시 적용한다(§9)."""
    intake, state = gate
    path = folder / "0910.json"
    state.record_file("/다른/경로/0910.json", "다른해시", I.APPLIED, date(2026, 9, 10))

    result = intake.evaluate([path])
    assert result.chosen is not None and result.chosen.status == I.IN_PROGRESS


def test_unparsable_file_is_failed_not_touched(gate, tmp_path):
    intake, _ = gate
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")

    result = intake.evaluate([broken])
    assert result.chosen is None
    assert result.rejected[0].status == I.FAILED
    assert broken.exists()  # 파일은 그대로 둔다


def test_record_writes_every_decision(gate, folder):
    intake, state = gate
    result = intake.evaluate(sorted(folder.glob("*.json")))
    intake.record(result)

    rows = state.conn.execute("SELECT path, status FROM files ORDER BY path").fetchall()
    assert len(rows) == 4
    assert sum(1 for r in rows if r["status"] == I.SKIPPED_SUPERSEDED) == 3
