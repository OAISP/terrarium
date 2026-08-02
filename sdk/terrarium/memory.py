"""Structured, retrieval-backed memory for long-running agents.

The sandbox's ``/memory`` is an FS volume — no retrieval, and on k8s its RWO PVC can't
multi-attach, so concurrent sessions of one agent silently fork. This module offers memory as
**client tools** instead: the agent calls ``memory_search`` / ``memory_write`` / ``memory_get``,
and the handlers run in YOUR process against a real store. That gives keyword/vector retrieval
at scale AND sidesteps multi-attach entirely (no shared volume) — concurrent sessions and
scheduled jobs share one store.

    from terrarium import TerrariumClient, TerrariumOptions
    from terrarium.memory import SqliteMemory, memory_tools

    store = SqliteMemory("assistant.db")            # durable across sessions/processes
    opts = TerrariumOptions(agent_id=agent_id, tools=memory_tools(store))

Bring your own backend (pgvector, Redis, an API) by implementing ``MemoryStore``; the reference
``SqliteMemory`` needs only the stdlib (SQLite FTS5, with a LIKE fallback).
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
from typing import Any, Protocol, runtime_checkable

from .options import ClientTool, tool

__all__ = ["MemoryStore", "SqliteMemory", "memory_tools"]


def _coerce_tags(tags: Any) -> list[str]:
    """Models routinely pass `tags` as a comma-joined STRING despite the array schema. Coerce
    to a clean list (split a string on commas/whitespace); ignore non-iterables. Without this,
    `" ".join(a_string)` silently stores it character-by-character."""
    if tags is None:
        return []
    if isinstance(tags, str):
        return [t for t in re.split(r"[,\s]+", tags.strip()) if t]
    if isinstance(tags, (list, tuple)):
        return [str(t) for t in tags if t is not None and str(t)]
    return []


@runtime_checkable
class MemoryStore(Protocol):
    """A pluggable memory backend. Implement these three async methods over your own store
    (pgvector, Redis, an internal API) and pass it to :func:`memory_tools`."""

    async def write(self, content: str, tags: list[str] | None = None) -> str:
        """Persist a memory; return its id."""
        ...

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return up to ``limit`` memories matching ``query`` (most relevant first)."""
        ...

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        """Fetch one memory by id, or ``None``."""
        ...


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fts_match(query: str) -> str:
    # Build an FTS5 query that RECALLS well. Three things matter:
    #  - quote each term so user text can't inject FTS5 operators;
    #  - prefix `*` (terms ≥3 chars) + the porter tokenizer so inflection doesn't lose a hit
    #    ("language" finds "languages", "cat" finds "cats");
    #  - join with OR, not the default implicit AND. An LLM's recall query is a bag of terms
    #    spread across MANY memories ("timezone language preference" where each fact lives in a
    #    different row); AND requires all terms in ONE row, so a broad query recalls nothing.
    #    OR surfaces every partial match and `ORDER BY rank` (in _search) floats the best up.
    terms = [t for t in query.replace('"', " ").split() if t]
    return " OR ".join((f'"{t}"*' if len(t) >= 3 else f'"{t}"') for t in terms)


class SqliteMemory:
    """Reference :class:`MemoryStore` over SQLite. Uses FTS5 for ranked full-text search when
    available, falling back to ``LIKE``. Thread-safe (one connection + lock); pass a file path
    for durability across processes, or the default ``:memory:`` for ephemeral use."""

    def __init__(self, path: str = ":memory:") -> None:
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._fts = True
        try:
            # porter stemming so "languages"/"language", "preferences"/"preference", etc. recall
            # each other (an LLM rarely recalls with the inflection it wrote). New tables only —
            # an existing table keeps its tokenizer, but prefix matching (see _fts_match) still helps.
            self._c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories "
                "USING fts5(content, tags, created_at UNINDEXED, tokenize='porter unicode61')")
        except sqlite3.OperationalError:  # SQLite built without FTS5 — degrade to LIKE
            self._fts = False
            self._c.execute(
                "CREATE TABLE IF NOT EXISTS memories "
                "(id INTEGER PRIMARY KEY, content TEXT, tags TEXT, created_at TEXT)")
        self._c.commit()

    # --- sync core (run off-loop via asyncio.to_thread) ---
    def _write(self, content: str, tags: list[str] | None) -> str:
        with self._lock:
            cur = self._c.execute(
                "INSERT INTO memories(content, tags, created_at) VALUES(?,?,?)",
                (content, " ".join(_coerce_tags(tags)), _now()))  # coerce: a stray str must not char-join
            self._c.commit()
            return str(cur.lastrowid)

    def _search(self, query: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            if self._fts and (m := _fts_match(query)):
                rows = self._c.execute(
                    "SELECT rowid AS id, content, tags, created_at FROM memories "
                    "WHERE memories MATCH ? ORDER BY rank LIMIT ?", (m, limit)).fetchall()
            else:
                rows = self._c.execute(
                    "SELECT rowid AS id, content, tags, created_at FROM memories "
                    "WHERE content LIKE ? ORDER BY rowid DESC LIMIT ?",
                    (f"%{query}%", limit)).fetchall()
            return [dict(r) for r in rows]

    def _get(self, memory_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._c.execute(
                "SELECT rowid AS id, content, tags, created_at FROM memories WHERE rowid=?",
                (memory_id,)).fetchone()
            return dict(row) if row else None

    async def write(self, content: str, tags: list[str] | None = None) -> str:
        return await asyncio.to_thread(self._write, content, tags)

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._search, query, limit)

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get, memory_id)


def memory_tools(store: MemoryStore) -> list[ClientTool]:
    """Ready-made client tools (``memory_write`` / ``memory_search`` / ``memory_get``) backed by
    ``store``. Pass the result to ``TerrariumOptions(tools=...)``; the handlers run in your
    process, so the store and its credentials never enter the sandbox."""

    @tool("memory_write", "Save a durable memory — a fact, preference, or decision — for later recall.",
          {"content": {"type": "string", "description": "The thing to remember"},
           "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional labels"}})
    async def memory_write(args: dict[str, Any]) -> str:
        # Coerce at the protocol boundary so EVERY MemoryStore backend gets a clean list, even
        # when the model passes tags as a comma-joined string (it routinely does).
        mid = await store.write(str(args["content"]), _coerce_tags(args.get("tags")))
        return f"saved memory {mid}"

    @tool("memory_search", "Search durable memory by keyword or topic. Call this BEFORE answering "
          "to recall relevant past context.",
          {"query": {"type": "string"}, "limit": {"type": "integer", "description": "max results (default 5)"}})
    async def memory_search(args: dict[str, Any]) -> str:
        hits = await store.search(str(args["query"]), int(args.get("limit") or 5))
        return json.dumps(hits) if hits else "no memories matched"

    @tool("memory_get", "Fetch one memory by its id.", {"id": {"type": "string"}})
    async def memory_get(args: dict[str, Any]) -> str:
        m = await store.get(str(args["id"]))
        return json.dumps(m) if m else "not found"

    return [memory_write, memory_search, memory_get]
