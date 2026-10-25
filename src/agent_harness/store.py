from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class RunStore:
    """SQLite WAL store for resumable, idempotent agent runs."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    result_json TEXT,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    run_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at_ns INTEGER NOT NULL,
                    finished_at_ns INTEGER,
                    result_json TEXT,
                    error TEXT,
                    PRIMARY KEY (run_id, attempt),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS effects (
                    run_id TEXT NOT NULL,
                    effect_key TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    PRIMARY KEY (run_id, effect_key),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
                """
            )

    def initialize_run(self, run_id: str, task_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        now = time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO runs
                    (run_id, task_id, status, spec_json, created_at_ns, updated_at_ns)
                VALUES (?, ?, 'pending', ?, ?, ?)
                """,
                (run_id, task_id, json.dumps(spec, sort_keys=True), now, now),
            )
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            connection.commit()
        assert row is not None
        return dict(row)

    def completed_run_ids(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT run_id FROM runs WHERE status = 'completed'").fetchall()
        return {row["run_id"] for row in rows}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def start_attempt(self, run_id: str, attempt: int) -> bool:
        """Claim one attempt. Returns False when another process already claimed it."""
        now = time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO attempts (run_id, attempt, status, started_at_ns)
                VALUES (?, ?, 'running', ?)
                """,
                (run_id, attempt, now),
            )
            if cursor.rowcount:
                connection.execute(
                    "UPDATE runs SET status = 'running', updated_at_ns = ? WHERE run_id = ?",
                    (now, run_id),
                )
            connection.commit()
        return bool(cursor.rowcount)

    def finish_attempt(
        self,
        run_id: str,
        attempt: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE attempts
                SET status = ?, finished_at_ns = ?, result_json = ?, error = ?
                WHERE run_id = ? AND attempt = ?
                """,
                (
                    status,
                    now,
                    json.dumps(result, sort_keys=True) if result is not None else None,
                    error,
                    run_id,
                    attempt,
                ),
            )
            connection.execute("UPDATE runs SET updated_at_ns = ? WHERE run_id = ?", (now, run_id))
            connection.commit()

    def finish_run(self, run_id: str, status: str, result: dict[str, Any]) -> None:
        now = time.time_ns()
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, result_json = ?, updated_at_ns = ? WHERE run_id = ?",
                (status, json.dumps(result, sort_keys=True), now, run_id),
            )

    def completed_attempts(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM attempts WHERE run_id = ? ORDER BY attempt",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_effect_once(self, run_id: str, effect_key: str, result: dict[str, Any]) -> bool:
        """Record a side effect exactly once. Returns True only for the first writer."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO effects (run_id, effect_key, result_json, created_at_ns)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, effect_key, json.dumps(result, sort_keys=True), time.time_ns()),
            )
        return bool(cursor.rowcount)
