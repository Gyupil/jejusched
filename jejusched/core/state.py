"""로컬 상태(SQLite) — 설계서 §3.

**캘린더가 진실의 원천이고 이 DB는 캐시/인덱스다.** 유일하게 캘린더에서
복원할 수 없는 것은 `user_deleted`(사용자가 직접 지운 기록)뿐이다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import EventStatus, ExistingEvent, Section

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    calendar_id   TEXT,
    calendar_name TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    needs_reauth  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    file_date    TEXT,
    mtime        REAL,
    status       TEXT NOT NULL,
    processed_at TEXT,
    summary_json TEXT
);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE TABLE IF NOT EXISTS events (
    logical_id          TEXT NOT NULL,
    target_id           INTEGER NOT NULL,
    google_event_id     TEXT NOT NULL,
    event_key           TEXT NOT NULL,
    content_hash        TEXT,
    date                TEXT NOT NULL,
    section             TEXT NOT NULL,
    slot                TEXT,
    status              TEXT NOT NULL,
    src_file_date       TEXT,
    last_seen_file_date TEXT,
    updated_at          TEXT,
    PRIMARY KEY (logical_id, target_id)
);
CREATE INDEX IF NOT EXISTS events_lookup ON events(target_id, date, section);
CREATE TABLE IF NOT EXISTS user_deleted (
    target_id  INTEGER NOT NULL,
    event_key  TEXT NOT NULL,
    logical_id TEXT,
    date       TEXT,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (target_id, event_key)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER,
    target_id   INTEGER,
    started_at  TEXT,
    finished_at TEXT,
    created     INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    marked      INTEGER DEFAULT 0,
    restored    INTEGER DEFAULT 0,
    held        INTEGER DEFAULT 0,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS llm_cache (
    pair_key   TEXT PRIMARY KEY,
    verdict    INTEGER NOT NULL,
    model      TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage (
    day_pt TEXT PRIMARY KEY,
    calls  INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Target:
    id: int
    email: str
    calendar_id: str | None
    calendar_name: str | None
    enabled: bool
    needs_reauth: bool


class State:
    def __init__(self, path: Path | str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        #  `check_same_thread=False`가 **반드시** 있어야 한다.
        #  트레이 앱은 메인 스레드에서 State를 만들고 워커·설정 창·강제 새로고침은
        #  각자 다른 스레드에서 쓴다. 기본값(True)이면 그 스레드들이 전부
        #  `ProgrammingError: SQLite objects created in a thread ...`로 죽는다.
        #  마법사만 메인 스레드라 "계정 추가는 되는데 그 뒤로 아무것도 안 되는"
        #  모습이 된다 — 실제로 v0.1.0에서 그렇게 나왔다.
        #
        #  잠금은 따로 두지 않는다. `sqlite3.threadsafety == 3`(serialized)이라
        #  연결 자체가 뮤텍스를 쥐고, 설계상 쓰는 쪽은 직렬 워커 하나뿐이다.
        #  `busy_timeout`은 WAL에서 읽기와 쓰기가 겹칠 때 즉시 실패하지 않게 한다.
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> State:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------- targets

    def upsert_target(
        self, email: str, calendar_id: str | None = None, calendar_name: str | None = None
    ) -> Target:
        self.conn.execute(
            """INSERT INTO targets (email, calendar_id, calendar_name, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET
                 calendar_id   = COALESCE(excluded.calendar_id, targets.calendar_id),
                 calendar_name = COALESCE(excluded.calendar_name, targets.calendar_name)""",
            (email, calendar_id, calendar_name, _now()),
        )
        return self.get_target(email)  # type: ignore[return-value]

    def get_target(self, email: str) -> Target | None:
        row = self.conn.execute("SELECT * FROM targets WHERE email=?", (email,)).fetchone()
        return self._target(row) if row else None

    def active_targets(self) -> list[Target]:
        rows = self.conn.execute(
            "SELECT * FROM targets WHERE enabled=1 AND needs_reauth=0 ORDER BY id"
        ).fetchall()
        return [self._target(r) for r in rows]

    def set_needs_reauth(self, target_id: int, value: bool = True) -> None:
        self.conn.execute(
            "UPDATE targets SET needs_reauth=? WHERE id=?", (1 if value else 0, target_id)
        )

    def delete_target(self, target_id: int) -> None:
        for table in ("events", "user_deleted"):
            self.conn.execute(f"DELETE FROM {table} WHERE target_id=?", (target_id,))
        self.conn.execute("DELETE FROM targets WHERE id=?", (target_id,))

    @staticmethod
    def _target(row: sqlite3.Row) -> Target:
        return Target(
            id=row["id"],
            email=row["email"],
            calendar_id=row["calendar_id"],
            calendar_name=row["calendar_name"],
            enabled=bool(row["enabled"]),
            needs_reauth=bool(row["needs_reauth"]),
        )

    # ------------------------------------------------------------ files

    def file_status(self, sha256: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM files WHERE sha256=? ORDER BY id DESC LIMIT 1", (sha256,)
        ).fetchone()
        return row["status"] if row else None

    def has_applied(self, sha256: str) -> bool:
        """이 내용을 이미 적용한 적이 있는가.

        같은 내용이 다른 경로로 다시 오면 `skipped_dup` 행이 새로 생겨 최신 행만
        보면 `applied`가 가려진다. 그래서 최신 상태가 아니라 존재 여부를 본다.
        """
        row = self.conn.execute(
            "SELECT 1 FROM files WHERE sha256=? AND status='applied' LIMIT 1", (sha256,)
        ).fetchone()
        return row is not None

    def record_file(
        self,
        path: str,
        sha256: str,
        status: str,
        file_date: date | None = None,
        mtime: float | None = None,
        summary: dict[str, Any] | None = None,
    ) -> int:
        row = self.conn.execute(
            "SELECT id, status FROM files WHERE sha256=? AND path=?", (sha256, path)
        ).fetchone()
        payload = (
            file_date.isoformat() if file_date else None,
            mtime,
            status,
            _now(),
            json.dumps(summary, ensure_ascii=False) if summary else None,
        )
        if row:
            #  이미 적용한 파일을 건너뛰었다고 해서 `applied`를 내리면 안 된다.
            #  내리면 max_applied_file_date()가 비어 오래된 파일 규칙이 무력해진다.
            if row["status"] == "applied" and status.startswith("skipped"):
                return int(row["id"])
            self.conn.execute(
                """UPDATE files SET file_date=COALESCE(?, file_date), mtime=COALESCE(?, mtime),
                   status=?, processed_at=?, summary_json=COALESCE(?, summary_json) WHERE id=?""",
                (*payload, row["id"]),
            )
            return int(row["id"])
        cur = self.conn.execute(
            """INSERT INTO files (path, sha256, file_date, mtime, status, processed_at, summary_json)
               VALUES (?,?,?,?,?,?,?)""",
            (path, sha256, *payload),
        )
        return int(cur.lastrowid)

    def max_applied_file_date(self) -> date | None:
        row = self.conn.execute(
            "SELECT MAX(file_date) AS d FROM files WHERE status='applied'"
        ).fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def files_in_progress(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM files WHERE status IN ('in_progress','held') ORDER BY file_date, mtime"
        ).fetchall()

    # ----------------------------------------------------------- events

    def replace_events(
        self,
        target_id: int,
        events: Iterable[ExistingEvent],
        start: date | None = None,
        end: date | None = None,
    ) -> None:
        """Reconcile 결과로 이 대상의 인덱스를 갈아 끼운다.

        Reconcile은 파일 날짜 범위만 조회하므로 범위를 주면 **그 구간만** 교체한다.
        범위를 주지 않으면 전체를 교체한다(force/전량 재동기화).
        """
        rows = [
            (
                e.logical_id, target_id, e.google_event_id, e.event_key, e.content_hash,
                e.date.isoformat(), e.section.value, e.slot, e.status.value,
                e.src_file_date.isoformat() if e.src_file_date else None,
                e.last_seen_file_date.isoformat() if e.last_seen_file_date else None,
                _now(),
            )
            for e in events
        ]
        if start and end:
            self.conn.execute(
                "DELETE FROM events WHERE target_id=? AND date BETWEEN ? AND ?",
                (target_id, start.isoformat(), end.isoformat()),
            )
        else:
            self.conn.execute("DELETE FROM events WHERE target_id=?", (target_id,))
        self.conn.executemany(
            """INSERT OR REPLACE INTO events (logical_id, target_id, google_event_id, event_key,
                   content_hash, date, section, slot, status, src_file_date,
                   last_seen_file_date, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )

    def upsert_event(
        self,
        target_id: int,
        *,
        logical_id: str,
        google_event_id: str,
        event_key: str,
        content_hash: str,
        when: date,
        section: Section,
        slot: str,
        status: str,
        file_date: date | None = None,
    ) -> None:
        """연산 직후 인덱스의 한 행을 갱신한다(설계서 §4-[9]).

        이 갱신이 있어야 다음 Reconcile에서 "로컬에는 있는데 캘린더에 없다"
        = 사용자가 직접 지웠다를 판별할 수 있다.
        """
        iso = file_date.isoformat() if file_date else None
        self.conn.execute(
            """INSERT INTO events (logical_id, target_id, google_event_id, event_key,
                   content_hash, date, section, slot, status, src_file_date,
                   last_seen_file_date, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(logical_id, target_id) DO UPDATE SET
                   google_event_id=excluded.google_event_id,
                   event_key=excluded.event_key,
                   content_hash=excluded.content_hash,
                   date=excluded.date,
                   section=excluded.section,
                   slot=excluded.slot,
                   status=excluded.status,
                   last_seen_file_date=COALESCE(excluded.last_seen_file_date,
                                                events.last_seen_file_date),
                   updated_at=excluded.updated_at""",
            (logical_id, target_id, google_event_id, event_key, content_hash,
             when.isoformat(), section.value, slot, status, iso, iso, _now()),
        )

    def known_event_keys(self, target_id: int) -> dict[str, str]:
        """event_key → logical_id (Reconcile 전의 로컬 스냅숏)."""
        rows = self.conn.execute(
            "SELECT event_key, logical_id FROM events WHERE target_id=?", (target_id,)
        ).fetchall()
        return {r["event_key"]: r["logical_id"] for r in rows}

    def local_events(
        self, target_id: int, start: date | None = None, end: date | None = None
    ) -> list[ExistingEvent]:
        if start and end:
            rows = self.conn.execute(
                """SELECT * FROM events WHERE target_id=? AND date BETWEEN ? AND ?
                   ORDER BY date, section""",
                (target_id, start.isoformat(), end.isoformat()),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE target_id=? ORDER BY date, section", (target_id,)
            ).fetchall()
        return [
            ExistingEvent(
                logical_id=r["logical_id"],
                google_event_id=r["google_event_id"],
                event_key=r["event_key"],
                content_hash=r["content_hash"] or "",
                date=date.fromisoformat(r["date"]),
                section=Section.from_text(r["section"]),
                slot=r["slot"] or "",
                title="",
                title_norm="",
                status=EventStatus(r["status"]),
            )
            for r in rows
        ]

    # ----------------------------------------------------- user_deleted

    def mark_user_deleted(
        self, target_id: int, event_key: str, logical_id: str = "", when: date | None = None
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO user_deleted (target_id, event_key, logical_id, date, deleted_at)
               VALUES (?,?,?,?,?)""",
            (target_id, event_key, logical_id, when.isoformat() if when else None, _now()),
        )

    def user_deleted_keys(self, target_id: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT event_key FROM user_deleted WHERE target_id=?", (target_id,)
        ).fetchall()
        return {r["event_key"] for r in rows}

    def clear_user_deleted(
        self, target_id: int, start: date | None = None, end: date | None = None
    ) -> int:
        """force 시 '직접 삭제한 일정도 복구' 옵션. 날짜 범위를 주면 그만큼만 지운다."""
        if start and end:
            cur = self.conn.execute(
                "DELETE FROM user_deleted WHERE target_id=? AND date IS NOT NULL AND date BETWEEN ? AND ?",
                (target_id, start.isoformat(), end.isoformat()),
            )
        else:
            cur = self.conn.execute("DELETE FROM user_deleted WHERE target_id=?", (target_id,))
        return cur.rowcount

    # -------------------------------------------------------- llm cache

    def cached_verdicts(self, pair_keys: Iterable[str]) -> dict[str, bool]:
        keys = list(pair_keys)
        if not keys:
            return {}
        out: dict[str, bool] = {}
        for i in range(0, len(keys), 400):  # SQLite 변수 한도
            chunk = keys[i : i + 400]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT pair_key, verdict FROM llm_cache WHERE pair_key IN ({marks})", chunk
            ).fetchall()
            out.update({r["pair_key"]: bool(r["verdict"]) for r in rows})
        return out

    def store_verdicts(self, verdicts: dict[str, bool], model: str) -> None:
        self.conn.executemany(
            """INSERT OR REPLACE INTO llm_cache (pair_key, verdict, model, created_at)
               VALUES (?,?,?,?)""",
            [(k, int(v), model, _now()) for k, v in verdicts.items()],
        )

    # ------------------------------------------------------- llm usage

    def get_calls(self, day_pt: str) -> int:
        row = self.conn.execute("SELECT calls FROM llm_usage WHERE day_pt=?", (day_pt,)).fetchone()
        return int(row["calls"]) if row else 0

    def add_call(self, day_pt: str) -> None:
        self.conn.execute(
            """INSERT INTO llm_usage (day_pt, calls) VALUES (?, 1)
               ON CONFLICT(day_pt) DO UPDATE SET calls = calls + 1""",
            (day_pt,),
        )

    # -------------------------------------------------------- sync_runs

    def record_run(self, file_id: int | None, target_id: int | None, counts: dict[str, int],
                   started_at: str, error: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO sync_runs (file_id, target_id, started_at, finished_at,
                   created, updated, marked, restored, held, error)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (file_id, target_id, started_at, _now(), counts.get("created", 0),
             counts.get("updated", 0), counts.get("marked", 0), counts.get("restored", 0),
             counts.get("held", 0), error),
        )

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
