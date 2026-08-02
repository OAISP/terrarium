"""Fire-and-forget webhook notifications on session events.

One URL + an event allow-list (config). Hooked into the session pump so an
unattended/scheduled agent that finishes, errors, or blows its budget can ping a
generic JSON webhook (Discord/Slack/ntfy/Gotify). No templating, no retry queue.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("terrarium.notify")

# fields we surface per event type (never bodies/secrets)
_EXTRA = {
    "error": ("message",),
    "budget_exceeded": ("cost_usd", "cap_usd", "hard_cap_usd"),
    "session_end": ("reason",),
}


class Notifier:
    def __init__(self, url: str | None, events: tuple[str, ...] | list[str]) -> None:
        self.url = url
        self.events = set(events)
        # Hold strong refs to scheduled POSTs: asyncio only keeps a weak ref to a
        # bare create_task(), so an in-flight webhook can be GC'd before it sends.
        self._tasks: set[asyncio.Task] = set()

    @property
    def active(self) -> bool:
        return bool(self.url)

    def build_payload(self, session_id: str, ev: dict[str, Any]) -> dict[str, Any] | None:
        """The JSON we'd POST for this event, or None if it isn't subscribed."""
        t = ev.get("type")
        if not self.url or t not in self.events:
            return None
        payload = {"session_id": session_id, "type": t, "ts": ev.get("ts"), "seq": ev.get("seq")}
        for k in _EXTRA.get(t, ()):
            if ev.get(k) is not None:
                payload[k] = ev.get(k)
        return payload

    def notify(self, session_id: str, ev: dict[str, Any]) -> None:
        """Called from the pump for every stamped event — schedules a POST if the
        event is subscribed. Never raises into the pump."""
        payload = self.build_payload(session_id, ev)
        if payload is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._post(payload))
        except RuntimeError:
            return  # no running loop (e.g. unit context) — nothing to schedule
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, payload: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(self.url, json=payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("notify failed: %s", exc)
