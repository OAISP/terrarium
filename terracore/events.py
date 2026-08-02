"""Append-only JSONL event log per session, with a single authoritative seq.

This is the durable source of truth *and* the wire format: the worker emits
typed event payloads (no seq), and the orchestrator's EventStore stamps the
authoritative ``seq``/``ts`` as it persists. Reconnecting clients replay via
``read(after=<seq>)`` because SSE has no native replay.

Event ``type`` values are the stable contract with the web inspector and the SDK.
The authoritative sets live in ``terrarium.protocol``: ``WORKER_EVENT_TYPES`` (what
a worker may assert) plus ``ORCH_ONLY_TYPES`` (``session_start``/``session_end``/
``budget_exceeded``, stamped by the orchestrator). Don't re-list them here — it drifts.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def tail_events(path: str | Path, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """Read at most the newest ``limit`` JSONL events without scanning from genesis.

    Returns ``(events, truncated)``. The extra raw line used for the truncation flag
    makes partial log searches explicit without loading an attacker-inflated history.
    """
    p = Path(path)
    if limit <= 0 or not p.exists():
        return [], False
    chunks: list[bytes] = []
    wanted = limit + 1
    try:
        with p.open("rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            newlines = 0
            while pos > 0 and newlines <= wanted:
                size = min(64 * 1024, pos)
                pos -= size
                fh.seek(pos)
                chunk = fh.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
    except OSError:
        return [], False
    raw_lines = b"".join(reversed(chunks)).decode("utf-8", "replace").splitlines()
    if pos > 0 and raw_lines:
        raw_lines = raw_lines[1:]  # first line is partial when the read stopped mid-file
    parsed: list[dict[str, Any]] = []
    for raw in raw_lines[-wanted:]:
        try:
            parsed.append(json.loads(raw))
        except Exception:
            continue
    return parsed[-limit:], len(raw_lines) > limit


class EventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = self._scan_next_seq()

    def _scan_next_seq(self) -> int:
        if not self.path.exists():
            return 0
        # Logs are append-only and seq is monotonic, so read backwards until the
        # first valid record instead of scanning an arbitrarily long history at
        # every EventStore construction. A corrupt/truncated suffix is skipped.
        def next_after(raw: bytes) -> int | None:
            try:
                value = json.loads(raw).get("seq")
                seq = int(value)
                return seq + 1 if seq >= 0 else None
            except Exception:
                return None

        remainder = b""
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, 2)
                pos = fh.tell()
                while pos > 0:
                    size = min(64 * 1024, pos)
                    pos -= size
                    fh.seek(pos)
                    data = fh.read(size) + remainder
                    lines = data.split(b"\n")
                    remainder = lines[0]
                    for raw in reversed(lines[1:]):
                        if not raw.strip():
                            continue
                        if (candidate := next_after(raw)) is not None:
                            return candidate
                if remainder.strip():
                    if (candidate := next_after(remainder)) is not None:
                        return candidate
        except OSError:
            pass
        return 0

    def _write(self, ev: dict[str, Any]) -> dict[str, Any]:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        return ev

    def append(self, type: str, **fields: Any) -> dict[str, Any]:
        """Record an orchestrator-generated event."""
        with self._lock:
            ev = {"seq": self._seq, "ts": now_iso(), "type": type, **fields}
            self._seq += 1
            return self._write(ev)

    def record(self, event: dict[str, Any]) -> dict[str, Any]:
        """Persist a worker-emitted event, stamping the authoritative seq/ts.

        Both ``seq`` AND ``ts`` are host-authoritative: the worker is untrusted,
        and the Logs view sorts/filters by ``ts`` — so we discard any worker-supplied
        timestamp (which could be forged to hide or reorder a tool_use in the operator's
        audit view) and stamp our own receive-time. ``seq`` was already host-stamped;
        ``ts`` now matches that invariant.
        """
        with self._lock:
            payload = {k: v for k, v in event.items() if k not in ("seq", "ts")}
            ev = {
                "seq": self._seq,
                "ts": now_iso(),
                "type": payload.pop("type", "unknown"),
                **payload,
            }
            self._seq += 1
            return self._write(ev)

    def read(self, after: int = -1) -> list[dict[str, Any]]:
        """Return events with seq > ``after`` (use -1 for all)."""
        out: list[dict[str, Any]] = []
        if not self.path.exists():
            return out
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if int(ev.get("seq", 0)) > after:
                    out.append(ev)
        return out
