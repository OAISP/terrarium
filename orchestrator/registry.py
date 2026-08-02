"""Durable session registry — a SQLite index *over* the JSONL event logs.

The per-session JSONL log (``terrarium.events.EventStore``) stays the source of
truth. This registry is a small, rebuildable index whose only job is to let the
orchestrator answer, after a restart, "which sessions were running, and on which
sandbox Pod?" — so it can **reattach** to survivors instead of orphaning them.

If a row and the JSONL ever disagree, the JSONL wins: the table holds only
metadata (never event bodies) and is fully regenerable from the logs.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from terracore.events import now_iso

# statuses we treat as "alive" — worth probing + reattaching on boot
LIVE_STATUSES = ("starting", "running", "idle")

_COLS = (
    "session_id", "agent_id", "memory_volume", "model", "system_mode", "title",
    "runner", "pod_name", "created_ts", "updated_ts", "status", "last_seq",
    "total_cost_usd", "harness_json",
)

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    agent_id       TEXT,
    memory_volume  TEXT,
    model          TEXT,
    system_mode    TEXT,
    title          TEXT,
    runner         TEXT,
    pod_name       TEXT,
    created_ts     TEXT,
    updated_ts     TEXT,
    status         TEXT,
    last_seq       INTEGER DEFAULT -1,
    total_cost_usd REAL DEFAULT 0,
    harness_json   TEXT
);
-- Append-only spend ledger: one row per session, keyed by session_id, that OUTLIVES the
-- session row (deleting a session must not erase its cost from the cumulative agent budget).
CREATE TABLE IF NOT EXISTS spend_ledger (
    session_id TEXT PRIMARY KEY,
    agent_id   TEXT,
    created_ts TEXT,
    cost_usd   REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS spend_by_agent ON spend_ledger(agent_id);
"""


class SessionRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # one connection, shared across the pump thread(s) + the loop — guarded
        # by _lock. WAL keeps the writes cheap and non-blocking for readers.
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_DDL)
            self._db.commit()

    def upsert(self, **row: Any) -> None:
        """Insert or replace a full row (used on session start)."""
        row.setdefault("updated_ts", now_iso())
        cols = [c for c in _COLS if c in row]
        ph = ",".join("?" for _ in cols)
        upd = ",".join(f"{c}=excluded.{c}" for c in cols if c != "session_id")
        sql = (
            f"INSERT INTO sessions ({','.join(cols)}) VALUES ({ph}) "
            f"ON CONFLICT(session_id) DO UPDATE SET {upd}"
        )
        with self._lock:
            self._db.execute(sql, [row[c] for c in cols])
            # Mirror into the durable ledger (survives session deletion). On start the cost is 0;
            # subsequent update()s bump it. agent_id/created_ts are set here and kept on conflict.
            self._db.execute(
                "INSERT INTO spend_ledger(session_id, agent_id, created_ts, cost_usd) "
                "VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                "agent_id=excluded.agent_id, created_ts=excluded.created_ts, cost_usd=excluded.cost_usd",
                [row.get("session_id"), row.get("agent_id"), row.get("created_ts"),
                 float(row.get("total_cost_usd") or 0)])
            self._db.commit()

    def update(self, session_id: str, **fields: Any) -> None:
        """Partial update — ``None`` values are ignored so we never null a column."""
        fields = {k: v for k, v in fields.items() if v is not None and k in _COLS}
        if not fields:
            return
        fields["updated_ts"] = now_iso()
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE sessions SET {sets} WHERE session_id=?",
                [*fields.values(), session_id],
            )
            if "total_cost_usd" in fields:  # keep the durable ledger current
                self._db.execute("UPDATE spend_ledger SET cost_usd=? WHERE session_id=?",
                                 [float(fields["total_cost_usd"]), session_id])
            self._db.commit()

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._db.execute("SELECT * FROM sessions WHERE session_id=?", [session_id])
            row = cur.fetchone()
        return dict(row) if row else None

    def list(self, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if statuses:
                ph = ",".join("?" for _ in statuses)
                cur = self._db.execute(
                    f"SELECT * FROM sessions WHERE status IN ({ph}) ORDER BY created_ts DESC",
                    list(statuses),
                )
            else:
                cur = self._db.execute("SELECT * FROM sessions ORDER BY created_ts DESC")
            return [dict(r) for r in cur.fetchall()]

    def spend(self, agent_id: str, since_iso: str | None = None) -> dict[str, Any]:
        """Cumulative spend for an agent across all its sessions (the durable budget ledger),
        optionally windowed to sessions created at/after ``since_iso``. created_ts is ISO-8601
        (lexically sortable), so a string compare windows correctly."""
        q = ("SELECT COUNT(*) AS sessions, COALESCE(SUM(cost_usd), 0) AS usd "
             "FROM spend_ledger WHERE agent_id=?")
        args: list[Any] = [agent_id]
        if since_iso:
            q += " AND created_ts >= ?"
            args.append(since_iso)
        with self._lock:
            row = self._db.execute(q, args).fetchone()
        return {"sessions": int(row["sessions"]), "total_cost_usd": round(float(row["usd"]), 6)}

    def spend_series(self, since_iso: str) -> list[dict[str, Any]]:
        """Daily spend from the durable ledger, oldest first, for days that had activity.

        Reads ``spend_ledger`` rather than the ``sessions`` table on purpose: the ledger
        OUTLIVES session deletion (see the DDL), so a fleet-cost view built on it doesn't
        silently shrink every time an operator cleans up a session. ``created_ts`` is
        ISO-8601, so ``substr(…,1,10)`` is the UTC day and a string compare windows it."""
        with self._lock:
            cur = self._db.execute(
                "SELECT substr(created_ts,1,10) AS day, COUNT(*) AS sessions, "
                "COALESCE(SUM(cost_usd),0) AS usd FROM spend_ledger "
                "WHERE created_ts IS NOT NULL AND created_ts >= ? "
                "GROUP BY day ORDER BY day", [since_iso])
            return [{"day": r["day"], "sessions": int(r["sessions"]),
                     "total_cost_usd": round(float(r["usd"]), 6)} for r in cur.fetchall()]

    def spend_by_agent(self, since_iso: str | None = None) -> list[dict[str, Any]]:
        """Per-agent spend from the durable ledger, biggest spender first. Sessions launched
        with an inline harness have no agent_id; they're grouped under a None key so the
        totals still add up to the fleet total."""
        q = ("SELECT agent_id, COUNT(*) AS sessions, COALESCE(SUM(cost_usd),0) AS usd "
             "FROM spend_ledger")
        args: list[Any] = []
        if since_iso:
            q += " WHERE created_ts IS NOT NULL AND created_ts >= ?"
            args.append(since_iso)
        q += " GROUP BY agent_id ORDER BY usd DESC"
        with self._lock:
            cur = self._db.execute(q, args)
            return [{"agent_id": r["agent_id"], "sessions": int(r["sessions"]),
                     "total_cost_usd": round(float(r["usd"]), 6)} for r in cur.fetchall()]

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE session_id=?", [session_id])
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
