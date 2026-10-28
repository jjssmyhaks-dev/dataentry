"""The agent's local queue (Appendix E).

A small on-disk store of commands and results, so nothing is lost when the
connection drops, Tally is closed, or the PC is switched off after office hours.
Standard-library SQLite only: the agent must run on a plain Windows PC with no
extra services.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

SCHEMA = """
create table if not exists commands (
  command_id     text primary key,
  kind           text not null,
  idempotency_key text,
  payload        text not null,
  expires_at     text,
  received_at    real not null,
  attempts       integer not null default 0,
  status         text not null default 'pending',
  last_error     text,
  updated_at     real
);
create table if not exists results (
  command_id   text primary key,
  status       text not null,
  body         text not null,
  created_at   real not null,
  delivered_at real
);
create index if not exists idx_commands_status on commands (status, received_at);
create index if not exists idx_results_pending on results (delivered_at);
create table if not exists events (
  id         integer primary key autoincrement,
  at         real not null,
  level      text not null,
  message    text not null,
  detail     text
);
"""


@dataclass
class QueuedCommand:
    command_id: str
    kind: str
    payload: dict[str, Any]
    idempotency_key: str | None = None
    expires_at: str | None = None
    attempts: int = 0
    status: str = "pending"
    last_error: str | None = None


class LocalQueue:
    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── commands ────────────────────────────────────────────────────────────

    def enqueue(self, command: dict[str, Any]) -> bool:
        """Store a command. Returns False if it was already handled."""
        command_id = command.get("command_id") or command.get("id")
        if not command_id:
            return False
        row = self._conn.execute(
            "select status from commands where command_id = ?", (command_id,)
        ).fetchone()
        if row:
            return False
        self._conn.execute(
            """
            insert into commands (command_id, kind, idempotency_key, payload, expires_at,
                                  received_at, status)
            values (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                command_id,
                command.get("command_kind") or command.get("kind") or "post_voucher",
                command.get("idempotency_key"),
                json.dumps(command),
                command.get("expires_at"),
                time.time(),
            ),
        )
        self._conn.commit()
        return True

    def pending(self, limit: int = 20) -> list[QueuedCommand]:
        rows = self._conn.execute(
            "select * from commands where status = 'pending' order by received_at limit ?",
            (limit,),
        ).fetchall()
        return [
            QueuedCommand(
                command_id=row["command_id"],
                kind=row["kind"],
                payload=json.loads(row["payload"]),
                idempotency_key=row["idempotency_key"],
                expires_at=row["expires_at"],
                attempts=row["attempts"],
                status=row["status"],
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def mark(self, command_id: str, status: str, *, error: str | None = None) -> None:
        self._conn.execute(
            "update commands set status = ?, attempts = attempts + 1, last_error = ?, "
            "updated_at = ? where command_id = ?",
            (status, error, time.time(), command_id),
        )
        self._conn.commit()

    def defer(self, command_id: str, *, error: str | None = None) -> None:
        """Keep a command pending: Tally is closed, busy, or the period is locked."""
        self._conn.execute(
            "update commands set attempts = attempts + 1, last_error = ?, updated_at = ? "
            "where command_id = ?",
            (error, time.time(), command_id),
        )
        self._conn.commit()

    def expire(self, command_id: str, *, reason: str) -> None:
        self._conn.execute(
            "update commands set status = 'expired', last_error = ?, updated_at = ? "
            "where command_id = ?",
            (reason, time.time(), command_id),
        )
        self._conn.commit()

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "select status, count(*) as n from commands group by status"
        ).fetchall()
        counts = {row["status"]: row["n"] for row in rows}
        counts["undelivered_results"] = self._conn.execute(
            "select count(*) as n from results where delivered_at is null"
        ).fetchone()["n"]
        return counts

    # ── results ─────────────────────────────────────────────────────────────

    def store_result(self, command_id: str, status: str, body: dict[str, Any]) -> None:
        self._conn.execute(
            """
            insert into results (command_id, status, body, created_at)
            values (?, ?, ?, ?)
            on conflict (command_id) do update
              set status = excluded.status, body = excluded.body, created_at = excluded.created_at
            """,
            (command_id, status, json.dumps(body), time.time()),
        )
        self._conn.commit()

    def undelivered_results(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "select * from results where delivered_at is null order by created_at limit ?",
            (limit,),
        ).fetchall()
        return [
            {
                "command_id": row["command_id"],
                "status": row["status"],
                "body": json.loads(row["body"]),
            }
            for row in rows
        ]

    def mark_result_delivered(self, command_id: str) -> None:
        self._conn.execute(
            "update results set delivered_at = ? where command_id = ?", (time.time(), command_id)
        )
        self._conn.commit()

    # ── log ─────────────────────────────────────────────────────────────────

    def log(self, level: str, message: str, detail: Any = None) -> None:
        """Local logs hold no document content, only what happened."""
        self._conn.execute(
            "insert into events (at, level, message, detail) values (?, ?, ?, ?)",
            (time.time(), level, message, None if detail is None else json.dumps(detail, default=str)),
        )
        self._conn.commit()

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "select * from events order by id desc limit ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
