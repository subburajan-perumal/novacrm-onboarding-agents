"""SQLite run store: state machine position, idempotency keys and results per deal email."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import RunState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    message_id TEXT UNIQUE NOT NULL,
    thread_id TEXT,
    state TEXT NOT NULL,
    customer TEXT,
    tier TEXT,
    project_id INTEGER,
    channel_id TEXT,
    call_attempts INTEGER DEFAULT 0,
    data TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_thread ON runs(thread_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunStore:
    def __init__(self, path: Path | str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    # ---- create / lookup ---------------------------------------------------
    def get_by_message(self, message_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM runs WHERE message_id=?", (message_id,)).fetchone()
        return self._row(row)

    def get(self, run_id: str) -> Optional[dict]:
        return self._row(self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

    def open_run_for_thread(self, thread_id: str, state: RunState) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE thread_id=? AND state=? ORDER BY created_at DESC LIMIT 1",
            (thread_id, state.value),
        ).fetchone()
        return self._row(row)

    def create(self, message_id: str, thread_id: str) -> dict:
        run_id = "run_" + uuid.uuid4().hex[:10]
        self.conn.execute(
            "INSERT INTO runs(run_id,message_id,thread_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (run_id, message_id, thread_id, RunState.RECEIVED.value, _now(), _now()),
        )
        self.conn.commit()
        return self.get(run_id)

    def list(self, states: list[RunState] | None = None) -> list[dict]:
        if states:
            marks = ",".join("?" for _ in states)
            rows = self.conn.execute(
                f"SELECT * FROM runs WHERE state IN ({marks}) ORDER BY created_at", [s.value for s in states]
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM runs ORDER BY created_at").fetchall()
        return [self._row(r) for r in rows]

    # ---- updates -----------------------------------------------------------
    def update(self, run_id: str, *, state: RunState | None = None, data: dict | None = None, **cols: Any) -> dict:
        current = self.get(run_id)
        sets, vals = ["updated_at=?"], [_now()]
        if state is not None:
            sets.append("state=?")
            vals.append(state.value)
        if data:
            merged = {**current["data"], **data}
            sets.append("data=?")
            vals.append(json.dumps(merged, default=str))
        for col, val in cols.items():
            sets.append(f"{col}=?")
            vals.append(val)
        vals.append(run_id)
        self.conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", vals)
        self.conn.commit()
        return self.get(run_id)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Optional[dict]:
        if row is None:
            return None
        d = dict(row)
        d["data"] = json.loads(d.get("data") or "{}")
        return d
