"""Scheduled / recurring agents — cron-driven sessions.

A schedule fires an existing agent with a fixed prompt on a 5-field cron cadence.
Each firing is just an ordinary session with a synthetic first message, so it
reuses the whole session machinery (durability, budget guard, notifications).

Deliberately small for a single-tenant deployment: a JSON store mirroring
``AgentStore``, a hand-rolled 5-field UTC cron matcher (no ``croniter`` dep), and
one ``asyncio`` loop. No per-schedule timezone, no backfill/catch-up, no retries —
miss-a-run is fine.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from terracore.events import now_iso

from .store import JsonStore

log = logging.getLogger("terrarium.schedules")


# ----- cron (5 fields: minute hour day-of-month month day-of-week) -----
def _field_match(spec: str, value: int, *, sunday_alias: bool = False) -> bool:
    candidates = {value, 7} if sunday_alias and value == 0 else {value}
    for part in spec.split(","):
        part = part.strip()
        if part in ("*", "?"):
            return True
        if part.startswith("*/"):
            try:
                if value % int(part[2:]) == 0:
                    return True
            except ValueError:
                pass
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                if any(int(a) <= candidate <= int(b) for candidate in candidates):
                    return True
            except ValueError:
                pass
            continue
        try:
            if int(part) in candidates:
                return True
        except ValueError:
            pass
    return False


def cron_match(expr: str, dt: datetime) -> bool:
    """True if ``dt`` matches standard 5-field cron semantics. dow: 0/7=Sunday."""
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    dow_val = (dt.weekday() + 1) % 7  # python Mon=0 → cron Sun=0
    dom_matches = _field_match(dom, dt.day)
    dow_matches = _field_match(dow, dow_val, sunday_alias=True)
    dom_open = any(part.strip() in ("*", "?") for part in dom.split(","))
    dow_open = any(part.strip() in ("*", "?") for part in dow.split(","))
    # Vixie/POSIX cron: when both day fields are restricted, either may match.
    day_matches = (
        dom_matches and dow_matches
        if dom_open or dow_open
        else dom_matches or dow_matches
    )
    return (
        _field_match(minute, dt.minute)
        and _field_match(hour, dt.hour)
        and day_matches
        and _field_match(month, dt.month)
    )


@dataclass
class Schedule:
    id: str
    name: str
    agent_id: str
    prompt: str
    cron: str
    enabled: bool = True
    max_budget_usd: float | None = None
    last_run: str | None = None
    last_session_id: str | None = None
    last_tick: str | None = None  # durable dedupe key (YYYYMMDDHHMM) of the last fired cron tick
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FIELDS = {f.name for f in fields(Schedule)}


class ScheduleStore(JsonStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._items: dict[str, Schedule] = {
            sid: Schedule(**{k: v for k, v in raw.items() if k in _FIELDS})
            for sid, raw in (self._read({}) or {}).items()
        }

    def _save(self) -> None:
        self._write({sid: asdict(s) for sid, s in self._items.items()})

    def create(self, *, name: str, agent_id: str, prompt: str, cron: str,
               enabled: bool = True, max_budget_usd: float | None = None) -> Schedule:
        if not cron_valid(cron):
            raise ValueError(f"invalid cron expression: {cron!r}")
        with self.lock:
            sid = "sch_" + uuid.uuid4().hex[:8]
            s = Schedule(id=sid, name=name, agent_id=agent_id, prompt=prompt,
                         cron=cron, enabled=enabled, max_budget_usd=max_budget_usd)
            self._items[sid] = s
            self._save()
            return s

    def get(self, sid: str) -> Schedule | None:
        return self._items.get(sid)

    def list(self) -> list[Schedule]:
        return list(self._items.values())

    def update(self, sid: str, **changes: Any) -> Schedule | None:
        with self.lock:
            s = self._items.get(sid)
            if not s:
                return None
            if "cron" in changes and changes["cron"] is not None and not cron_valid(changes["cron"]):
                raise ValueError(f"invalid cron expression: {changes['cron']!r}")
            for k, v in changes.items():
                if v is not None and k in _FIELDS and k not in ("id", "created_at"):
                    setattr(s, k, v)
            self._save()
            return s

    def delete(self, sid: str) -> bool:
        with self.lock:
            if sid in self._items:
                del self._items[sid]
                self._save()
                return True
            return False


# Per-field inclusive [min, max] — kept in lockstep with what _field_match honors.
_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))  # minute hour dom month dow (7=Sun)


def _field_valid(spec: str, lo: int, hi: int) -> bool:
    """Validate one cron field against [lo, hi], accepting ONLY the forms the
    matcher actually supports (`*`, `?`, `*/N`, `a-b`, `N`, comma-joined). An
    out-of-range or non-numeric token is rejected so a typo can't pass validation
    and then silently never fire."""
    spec = spec.strip()
    if not spec:
        return False
    for part in spec.split(","):
        part = part.strip()
        if part in ("*", "?"):
            continue
        if part.startswith("*/"):
            step = part[2:]
            if not step.isdigit() or int(step) < 1:
                return False
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return False
            ai, bi = int(a), int(b)
            if not (lo <= ai <= bi <= hi):
                return False
            continue
        if not part.isdigit() or not (lo <= int(part) <= hi):
            return False
    return True


def cron_valid(expr: str) -> bool:
    parts = expr.split()
    if len(parts) != 5:
        return False
    # strict=True documents (and enforces) that the 5-field guard above and _CRON_RANGES agree.
    # Without it, adding a 6th range would silently stop validating the extra field.
    return all(_field_valid(p, lo, hi) for p, (lo, hi) in zip(parts, _CRON_RANGES, strict=True))


class Scheduler:
    """Background loop: wakes a few times a minute, fires due schedules. Each
    firing creates a session via the existing SessionManager and sends the
    schedule's prompt."""

    def __init__(self, *, store: ScheduleStore, manager: Any, agents: Any) -> None:
        self.store = store
        self.manager = manager
        self.agents = agents
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Strong refs to in-flight fires: create_task() alone is weakly held, so a
        # firing could be GC'd mid-flight before the session is created.
        self._fires: set[asyncio.Task] = set()

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def run(self) -> None:
        seen: set[tuple[str, str]] = set()
        while not self._stop.is_set():
            # The product currently has one explicit scheduling timezone: UTC.
            # Never inherit a container/node's local timezone implicitly.
            now = datetime.now(timezone.utc)
            minute = now.strftime("%Y%m%d%H%M")
            seen = {k for k in seen if k[1] == minute}  # only this minute's fires
            for s in self.store.list():
                if not s.enabled:
                    continue
                key = (s.id, minute)
                if key in seen:
                    continue
                if cron_match(s.cron, now):
                    seen.add(key)
                    fire = asyncio.create_task(self.fire(s.id, tick=minute))
                    self._fires.add(fire)
                    fire.add_done_callback(self._fires.discard)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass

    async def fire(self, sid: str, tick: str | None = None) -> str | None:
        """Run a schedule now (also the /run endpoint). Returns the session id. ``tick`` is the
        cron tick key (YYYYMMDDHHMM) for cron-driven fires — it makes firing IDEMPOTENT: a tick
        already recorded in ``last_tick`` is dropped, so a missed/retried tick or an orchestrator
        restart within the same minute can't double-act. Manual fires (tick=None) always run."""
        from .runners import SessionConfig

        s = self.store.get(sid)
        if not s:
            return None
        if tick is not None and s.last_tick == tick:
            return None  # this tick already fired (durable, survives restarts) — at most once
        spec = self.agents.get(s.agent_id)
        if not spec:
            log.warning("schedule %s references missing agent %s", s.id, s.agent_id)
            return None
        if tick is not None:
            self.store.update(s.id, last_tick=tick)  # CLAIM the tick before the heavy work, so a
            #                                          crash mid-fire can't re-fire on restart
        try:
            sc = SessionConfig.from_agent(spec, title=f"scheduled: {s.name}")
            if s.max_budget_usd:
                sc.harness.max_budget_usd = s.max_budget_usd
            session = await self.manager.create(sc)
            if s.prompt:
                await session.send_message(s.prompt)
            self.store.update(s.id, last_run=now_iso(), last_session_id=session.id)
            return session.id
        except Exception as exc:  # noqa: BLE001
            log.warning("schedule %s fire failed: %s", s.id, exc)
            return None
