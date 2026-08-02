"""Session lifecycle + event fan-out.

A Session owns a Runner and an EventStore. A background pump reads events from
the runner, stamps + persists them (the JSONL log is the source of truth), and
broadcasts to live SSE subscribers. Subscribers replay from the store first
(SSE has no native replay) and dedupe by seq.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import time
from dataclasses import replace
from typing import Any, AsyncIterator

from terracore.events import EventStore, now_iso, tail_events
from terracore.harness import Harness
from terracore.protocol import TRANSIENT_EVENT_TYPES, validate_worker_event

from .config import Config
from .notify import Notifier
from .registry import LIVE_STATUSES, SessionRegistry
from .runners import SessionConfig, make_runner

log = logging.getLogger("terrarium.manager")


class CapacityError(RuntimeError):
    """A session was rejected before launch because an admission bound is full."""


def _new_id() -> str:
    # Keep the sortable timestamp prefix operators recognize, but add enough
    # entropy that concurrent creates cannot overwrite one another.
    return time.strftime("%Y%m%d-%H%M%S-") + f"{time.time_ns() // 1_000_000 % 1000:03d}-{secrets.token_hex(4)}"


def _num(v: Any) -> float:
    return v if isinstance(v, (int, float)) else 0


# Per-subscriber SSE backlog cap. A stalled client beyond this gets resync'd
# (drop backlog → overflow sentinel → reconnect+replay) rather than buffering
# the whole session in RAM.
_SUB_QUEUE_MAX = int(os.environ.get("TERRA_SSE_QUEUE_MAX", "2000"))
_STREAM_OVERFLOW = {"type": "_overflow"}    # sentinel: subscriber fell too far behind
_STREAM_HEARTBEAT = {"type": "_heartbeat"}  # sentinel: keepalive tick on an idle live stream
# Emit a keepalive on the live stream after this many idle seconds so intermediaries
# don't cull the connection and the client can tell live-but-idle from dead.
_HEARTBEAT_S = float(os.environ.get("TERRA_SSE_HEARTBEAT_S", "15"))


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold a session's event log into totals.

    The Claude Agent SDK's ``ResultMessage`` carries two differently-scoped
    fields, and conflating them is an easy mistake:

      • ``total_cost_usd`` is the CLI's *cumulative* session cost — it grows
        monotonically across result events (one result per turn). The session
        total is the *latest* value, NOT the sum; summing multiplies the true
        cost by roughly the turn count.
      • ``usage`` is *per-turn*. Summing it yields total tokens processed across
        the session (cache reads are re-counted each turn, since every turn
        re-reads the growing conversation from cache).
    """
    tokens = {"input": 0, "output": 0, "cacheRead": 0, "cacheCreate": 0, "subagent": 0, "total": 0}
    banked = 0.0   # cost of completed CLI segments (a rewind reconnect restarts the counter)
    seg = 0.0      # latest cumulative cost within the current segment
    turns = 0
    tools = 0
    # Sub-agent / workflow tokens are NOT in the per-turn `usage` (that's main-agent only);
    # each completed task reports its total in task_notification.usage.total_tokens (for a
    # Workflow, the aggregate of its agents). Keyed by task_id so re-notifications don't
    # double-count. (Cost already includes sub-agents via total_cost_usd — this is tokens only.)
    sub_by_task: dict[str, int] = {}
    context: dict[str, Any] | None = None  # latest context-window usage (for supervisors)
    # Resume cursor: the seq of the last TURN-BOUNDARY event (ready/status/result), which is
    # deliberately NOT the log's high-water mark. A consumer reattaching seeds its stream at
    # this seq, so it skips history it has already seen without skipping anything unfinished.
    # Boundary events are exactly the safe cut points: every client_tool_call belonging to a
    # COMPLETED turn precedes that turn's `result`, so resuming here never re-runs a handler
    # whose result was already delivered. Mid-turn, the cursor lags to the last boundary and
    # the tail replays — which is correct, since a client_tool_call after it is still awaiting
    # an answer. Lagging is the safe failure direction: a stale cursor costs a little replay,
    # while a too-far-ahead one would silently drop events.
    resume_cursor = -1
    # How the run ENDED, when it didn't end by the agent finishing. The transcript shows this
    # loudly, but a fleet list showed every finished session as the same grey "terminated" —
    # so a budget hard-stop and a dead sandbox were indistinguishable from a clean finish at
    # exactly the altitude an operator scans for trouble.
    terminal: str | None = None
    # When the session began, taken from the log (the source of truth) rather than the
    # registry — so a session whose registry row was reaped still reports an age.
    started_ts: str | None = None
    for e in events:
        t = e.get("type")
        if started_ts is None and e.get("ts"):
            started_ts = str(e["ts"])
        if t in ("ready", "status", "result") and isinstance(e.get("seq"), int):
            resume_cursor = e["seq"]
        if t == "context_usage":
            context = {k: e.get(k) for k in
                       ("percentage", "total_tokens", "max_tokens", "auto_compact", "compact_threshold")}
        elif t == "result":
            if e.get("total_cost_usd") is not None:
                c = _num(e.get("total_cost_usd"))
                if c + 1e-9 < seg:   # cost dropped → a rewind reconnect started a new segment
                    banked += seg
                seg = c
            u = e.get("usage") or {}
            tokens["input"] += _num(u.get("input_tokens"))
            tokens["output"] += _num(u.get("output_tokens"))
            tokens["cacheRead"] += _num(u.get("cache_read_input_tokens"))
            tokens["cacheCreate"] += _num(u.get("cache_creation_input_tokens"))
        elif t == "system" and e.get("subtype") == "task_notification":
            d = e.get("data") or {}
            tt = _num((d.get("usage") or {}).get("total_tokens"))
            tid = d.get("task_id")
            if tt and tid is not None:
                sub_by_task[str(tid)] = int(tt)   # latest wins
        elif t == "budget_exceeded":
            terminal = "budget"
        elif t == "worker_lost":
            terminal = "lost"
        elif t == "user":
            turns += 1
        elif t == "tool_use":
            tools += 1
    tokens["subagent"] = sum(sub_by_task.values())
    tokens["total"] = tokens["input"] + tokens["output"] + tokens["cacheRead"] + tokens["cacheCreate"] + tokens["subagent"]
    return {"user_turns": turns, "tool_calls": tools, "total_cost_usd": banked + seg,
            "tokens": tokens, "context": context, "resume_cursor": resume_cursor,
            "started_ts": started_ts, "terminal": terminal}


def fold_cost(events: list[dict[str, Any]]) -> tuple[float, float]:
    """Fold result events into ``(banked, seg)`` — completed-segment cost + the
    latest segment's cumulative cost (a rewind reconnect restarts the CLI's
    counter, so a drop banks the prior segment). Session total is ``banked + seg``.
    Used to seed cost accounting on reattach so the budget backstop and fleet
    cost survive an orchestrator restart instead of resetting to zero."""
    banked = 0.0
    seg = 0.0
    for e in events:
        if e.get("type") == "result" and e.get("total_cost_usd") is not None:
            c = _num(e.get("total_cost_usd"))
            if c + 1e-9 < seg:
                banked += seg
            seg = c
    return banked, seg


class Session:
    def __init__(
        self, id: str, config: Config, sess: SessionConfig,
        registry: "SessionRegistry | None" = None,
        notifier: "Notifier | None" = None,
    ) -> None:
        self.id = id
        self.config = config
        self.sess = sess
        self.registry = registry
        self.notifier = notifier
        self.store = EventStore(config.logs_dir / f"{id}.jsonl")
        self.runner = make_runner(config, id, sess)
        self.status = "starting"
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._pump_task: asyncio.Task[None] | None = None
        self._detaching = False
        self._stopping = False   # an INTENTIONAL stop (delete / budget kill / graceful detach) — so
        #                          the pump can tell a deliberate end from an unexpected worker death
        self._budget_killed = False
        # cost is cumulative within a CLI segment but restarts on a rewind reconnect —
        # bank completed segments so the recorded cost is the true session total.
        self._cost_banked = 0.0
        self._cost_seg = 0.0
        # Count accepted result messages. This is useful operationally, but result
        # messages still originate in the untrusted worker and are not an authority.
        self._result_count = 0
        self._turn_started: float | None = None  # monotonic ts the current RUNNING turn began
        # Wall-clock creation time, reported in the summary so the console can show an age and
        # order the list. ONE value shared with the registry row (_register writes this, not a
        # fresh now_iso()), so the list's ordering key and the displayed age can't disagree.
        # reattach() overwrites it with the persisted value — a restart must not reset the age.
        self.created_ts = now_iso()

    async def start(self) -> None:
        await self.runner.start()
        self.store.append(
            "session_start",
            session_id=self.id,
            model=self.sess.model,
            system_mode=self.sess.system_mode,
            title=self.sess.title,
        )
        self._register("starting")
        self._pump_task = asyncio.create_task(self._pump())

    async def reattach(self, status: str = "idle", created_ts: str | None = None) -> None:
        """Resume a session whose sandbox Pod survived an orchestrator restart:
        reattach to the running worker and replay the pump. No ``session_start``
        — the JSONL already holds the prior history. Seed the last-known status so
        an idle reattached session doesn't mis-report as ``starting`` forever."""
        self.status = status if status in ("running", "idle") else "idle"
        # Restore cost accounting from the durable log so the budget backstop and
        # fleet cost survive the restart (otherwise both silently reset to zero).
        prior = self.store.read()
        self._cost_banked, self._cost_seg = fold_cost(prior)
        self._result_count = sum(1 for e in prior if e.get("type") == "result")
        # Keep the ORIGINAL creation time (registry row, else the log's first event). Without
        # this a restart would re-date every surviving session to the restart, so an hours-old
        # run would read as "just now" and sort to the top.
        self.created_ts = created_ts or (str(prior[0].get("ts")) if prior and prior[0].get("ts")
                                        else self.created_ts)
        await self.runner.reattach()
        self._pump_task = asyncio.create_task(self._pump())

    def _safe_harness_json(self) -> str:
        """Harness for the registry with secret-bearing fields stripped. The
        sandbox Pod already holds the full harness (``TERRA_HARNESS``); the host
        only needs model/persona to summarize + reattach, so we never persist
        inline-session ``env``/``extra_options``/``mcp_servers`` to the PVC."""
        d = json.loads(self.sess.harness.to_json())
        for k in ("env", "extra_options", "mcp_servers"):
            d.pop(k, None)
        return json.dumps(d)

    def _register(self, status: str) -> None:
        if not self.registry:
            return
        self.registry.upsert(
            session_id=self.id,
            agent_id=self.sess.agent_id,
            memory_volume=self.sess.memory_volume,
            model=self.sess.model,
            system_mode=self.sess.system_mode,
            title=self.sess.title,
            runner=self.config.runner,
            pod_name=getattr(self.runner, "pod_name", None),
            created_ts=self.created_ts,
            status=status,
            harness_json=self._safe_harness_json(),
        )

    def _track_registry(self, ev: dict[str, Any]) -> None:
        if not self.registry:
            return
        t = ev.get("type")
        if t in ("status", "ready"):
            self.registry.update(self.id, status=self.status, last_seq=ev.get("seq"))
        elif t == "result":
            # cost already folded by _bank() (called once per event in _pump)
            self.registry.update(
                self.id, last_seq=ev.get("seq"),
                total_cost_usd=self._cost_banked + self._cost_seg,
            )

    async def _enforce_budget(self, ev: dict[str, Any]) -> None:
        """Hard-kill the session if it blows past a backstop. Three independent bounds:

        - **cost** (``result`` events): cumulative spend > cap × hard-multiplier. Worker-
          reported and thus forgeable, so it's backed by:
        - **turns** (``result`` events): accepted result messages > max_turns.
        - **runtime** (ANY event): a turn that has been continuously RUNNING past
          ``budget_max_run_seconds`` without ever closing. The first two only fire on
          ``result`` events, so an agent that streams tokens forever without finishing a
          turn would evade them; this host timer runs on every event once a running
          transition has been accepted. These are operational guards, not an
          unforgeable accounting boundary.

        Stopping the runner closes the stream, which ends the pump and writes
        session_end. We never await our own pump task here (that would deadlock) — only
        the runner."""
        if self._budget_killed:
            return

        # runtime backstop — checked on EVERY event (and by the manager watchdog, which covers
        # a turn wedged with NO events, since this event-driven path can't fire without one).
        if await self.enforce_runtime_backstop():
            return

        if ev.get("type") != "result":
            return
        cap = self.sess.harness.max_budget_usd
        if not cap:
            return
        cost = self._cost_banked + self._cost_seg  # folded by _bank()
        hard = cap * self.config.budget_hard_mult
        # Cost and result messages come from the untrusted worker. Counting accepted
        # results is still a useful independent runaway guard, not an authority.
        max_turns = self.config.budget_max_turns
        over_cost = cost > hard
        over_turns = bool(max_turns) and self._result_count > max_turns
        if not (over_cost or over_turns):
            return
        await self._kill_over_budget("cost" if over_cost else "turns", cost, cap=cap, hard=hard)

    async def enforce_runtime_backstop(self) -> bool:
        """Wall-clock hard-kill for a turn that has been RUNNING past ``budget_max_run_seconds``.
        Returns True if it killed. Safe to call with no event — the manager's periodic watchdog
        uses it to catch a turn wedged with NO events (which the event-driven pump can't see)."""
        if self._budget_killed:
            return False
        max_run = self.config.budget_max_run_seconds
        if max_run and self._turn_started is not None and \
                (time.monotonic() - self._turn_started) > max_run:
            await self._kill_over_budget("runtime", self._cost_banked + self._cost_seg)
            return True
        return False

    async def enforce_storage_limits(self, *, check_audit: bool = True) -> bool:
        """Terminate a producer before one session can fill control-plane storage.

        Evidence is never rotated or silently truncated because that would break
        replay/audit integrity. Zero disables the corresponding bound.
        """
        if self._stopping:
            return False
        checks: list[tuple[str, int, int]] = []
        if self.config.max_event_log_bytes:
            try:
                checks.append((
                    "event_log", self.store.path.stat().st_size,
                    self.config.max_event_log_bytes,
                ))
            except OSError:
                pass
        if check_audit and self.config.max_audit_log_bytes:
            from .egress import session_audit_path
            try:
                checks.append((
                    "egress_audit",
                    session_audit_path(self.config, self.id).stat().st_size,
                    self.config.max_audit_log_bytes,
                ))
            except OSError:
                pass
        for resource, size, limit in checks:
            if size <= limit:
                continue
            self._stopping = True
            ev = self.store.append(
                "error", subtype="storage_limit", resource=resource,
                size_bytes=size, limit_bytes=limit,
                message=f"{resource} exceeded its {limit}-byte limit",
            )
            self._broadcast(ev)
            if self.registry:
                self.registry.update(self.id, status="terminated")
            try:
                await self.runner.stop()
            except Exception:
                pass
            return True
        return False

    async def _kill_over_budget(self, reason: str, cost: float,
                                cap: float | None = None, hard: float | None = None) -> None:
        self._budget_killed = True
        self._stopping = True   # deliberate budget kill — not a worker crash
        kill = self.store.append(
            "budget_exceeded", cost_usd=round(cost, 4), cap_usd=cap,
            hard_cap_usd=round(hard, 4) if hard is not None else None,
            reason=reason, result_count=self._result_count,
        )
        self._broadcast(kill)
        if self.registry:
            self.registry.update(self.id, status="terminated")
        try:
            await self.runner.stop()  # closes the stream → pump ends → session_end
        except Exception:
            pass

    def _bank(self, ev: dict[str, Any]) -> None:
        """Fold a result event's cumulative cost into banked+seg (once per event).
        A rewind reconnect restarts the CLI counter, so a drop banks the prior
        segment. ``total_cost_usd`` is already clamped ≥ 0 by validate_worker_event."""
        if ev.get("type") != "result":
            return
        self._result_count += 1
        c = ev.get("total_cost_usd")
        if isinstance(c, (int, float)):
            if c + 1e-9 < self._cost_seg:
                self._cost_banked += self._cost_seg
            self._cost_seg = c

    # A dropped event stream is not proof the agent died. For a durable runner the stream is
    # a client of the sandbox (a `docker attach` subprocess), not the sandbox itself — and it
    # dies on its own for reasons the agent knows nothing about: a Docker daemon restart, a
    # host suspend, the CLI closing an idle connection. Reattach that many times before
    # concluding the worker is really gone.
    _MAX_REATTACH = 5

    async def _pump(self) -> None:
        """Consume the runner's events, reattaching across transient stream loss.

        Terminating on the first EOF is what marked healthy, still-running sandboxes as
        `terminated` after a day or two of idling: the container was up, the agent was fine,
        and the orchestrator had simply lost its pipe to it. `probe_state()` is the
        discriminator — the sandbox's worker IS its main process, so a running container
        means a running worker.
        """
        attempt = 0
        while True:
            await self._pump_once()
            if self._detaching:
                # Graceful release of a durable sandbox: write NOTHING terminal, or the next
                # boot's rehydrate finds a "terminated" row for a sandbox that is still alive.
                return
            if self._stopping:
                break  # we asked for this (delete / budget kill) — close it out, no worker_lost
            if not getattr(self.runner, "durable", False):
                break  # a non-durable worker cannot be reattached; it is genuinely gone
            probe = "unknown"
            try:
                probe = await self.runner.probe_state()
            except Exception:  # noqa: BLE001 — an unreadable probe is not evidence of death
                log.warning("session %s: probe failed after stream loss", self.id, exc_info=True)
            if probe != "running" or attempt >= self._MAX_REATTACH:
                break
            attempt += 1
            # Back off a little: a daemon restart takes a moment to accept connections again,
            # and an immediate retry would burn the whole budget inside a second.
            await asyncio.sleep(min(2 ** attempt, 15))
            try:
                await self.runner.reattach()
            except Exception:  # noqa: BLE001
                log.warning("session %s: reattach %d/%d failed", self.id, attempt,
                            self._MAX_REATTACH, exc_info=True)
                break
            log.info("session %s: event stream dropped but the sandbox is alive — "
                     "reattached (%d/%d)", self.id, attempt, self._MAX_REATTACH)
            self._broadcast(self.store.append("reattached", attempt=attempt))
        self._write_terminal()

    def _write_terminal(self) -> None:
        """Close the session out: the sandbox really is gone (or we stopped it)."""
        if not self._stopping:
            # The stream ended, the sandbox is not running, and we did not ask for that —
            # the worker died (OOM, evict, crash). Distinct from a clean session_end so a
            # supervisor/SDK can react.
            lost = self.store.append("worker_lost", reason="stream_ended",
                                     mid_turn=self._turn_started is not None)
            self._broadcast(lost)
        end = self.store.append("session_end")
        self.status = "terminated"
        if self.registry:
            self.registry.update(self.id, status="terminated")
        self._broadcast(end)

    async def _pump_once(self) -> None:
        try:
            async for ev in self.runner.events():
                # The sandbox is untrusted — sanitize before we persist/act on it.
                sanitized = validate_worker_event(ev)
                if sanitized.get("type") in TRANSIENT_EVENT_TYPES:
                    # Live-only (assistant text deltas): broadcast to subscribers but NEVER
                    # persist/replay — the canonical record is the final assistant_text. No
                    # seq → the SSE generator yields it without touching the replay cursor.
                    self._broadcast(sanitized)
                    continue
                stored = self.store.record(sanitized)
                self._bank(stored)
                self._track(stored)
                self._track_registry(stored)
                self._broadcast(stored)
                if await self.enforce_storage_limits(check_audit=False):
                    break
                await self._enforce_budget(stored)
                # Turn end → persist /memory (memory_mode="synced" only; a no-op otherwise).
                # This is what bounds the loss window to ONE turn rather than a whole session:
                # without it, an abruptly-killed pod would drop everything written since launch.
                # Fire-and-forget so a slow exec never stalls the event pump.
                if stored.get("type") == "status" and stored.get("status") == "idle":
                    asyncio.create_task(self._snapshot_memory_safe())
        finally:
            # A raising stream leaves the exception in flight through this block. Log it —
            # a broken detach must not look identical to a clean one — but do NOT decide the
            # session's fate here; _pump owns that, because only it can tell a dropped pipe
            # from a dead sandbox.
            if (exc := sys.exc_info()[1]) is not None:
                log.warning("session %s event stream ended with an error", self.id, exc_info=exc)

    async def _snapshot_memory_safe(self) -> None:
        """Turn-end memory snapshot that can never take the session down with it."""
        try:
            await self.runner.snapshot_memory()
        except Exception:  # noqa: BLE001
            log.warning("memory snapshot failed for session %s", self.id, exc_info=True)

    def _track(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "ready":
            self.status = "idle"
        elif t == "status":
            self.status = str(ev.get("status", self.status))
        elif t == "error":
            pass
        # Mark when a turn is actively running so the runaway-turn backstop can bound
        # a turn that streams forever without ever emitting a `result` (see
        # _enforce_budget). Cleared the moment the session leaves "running".
        if self.status == "running":
            if self._turn_started is None:
                self._turn_started = time.monotonic()
        else:
            self._turn_started = None

    def _broadcast(self, ev: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # Slow/stalled SSE consumer: don't grow memory unbounded. Drop its
                # backlog and hand it an overflow sentinel so it reconnects and
                # replays from the durable log (the source of truth) — no events
                # are lost, and one stuck client can't OOM the orchestrator.
                try:
                    while True:
                        q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(_STREAM_OVERFLOW)
                except asyncio.QueueFull:
                    pass
        if self.notifier:
            self.notifier.notify(self.id, ev)  # fire-and-forget; filters by event type

    async def send_message(self, content: "str | list") -> None:
        from terracore import protocol as P

        await self.runner.send(P.query_cmd(content))

    async def interrupt(self) -> None:
        from terracore import protocol as P

        await self.runner.send(P.interrupt_cmd())

    async def answer(self, question_id: str, answers: dict[str, Any]) -> None:
        """Deliver the operator's answer to a pending AskUserQuestion in the worker."""
        from terracore import protocol as P

        await self.runner.send(P.answer_cmd(question_id, answers))

    async def decide(self, request_id: str, decision: str) -> None:
        """Deliver the operator's allow/always/deny verdict on a pending tool-permission
        request in the worker."""
        from terracore import protocol as P

        await self.runner.send(P.decision_cmd(request_id, decision))

    async def client_tool_result(self, call_id: str, content: "str | list", is_error: bool = False) -> None:
        """Deliver the SDK client's result for a client-bridged tool call back to the worker
        (which hands it to the blocked bridge tool → the agent)."""
        from terracore import protocol as P

        await self.runner.send(P.client_tool_result_cmd(call_id, content, is_error))

    async def reconfigure(self, model: str | None = None, permission_mode: str | None = None) -> None:
        """Change this running session's config live (model and/or permission_mode). Updates
        the live harness so reattach/summary reflect it, then pushes the change to the worker."""
        from terracore import protocol as P

        if model:
            self.sess.harness.model = model  # SessionConfig.model is a property over harness.model
        if permission_mode:
            self.sess.harness.permission_mode = permission_mode
        if (model or permission_mode) and self.registry:
            # Persist so an orchestrator restart reattaches with the LIVE config.
            # reattach() rebuilds the harness from harness_json (not the model column),
            # so we must rewrite harness_json — otherwise a live model/permission_mode
            # switch silently reverts to the launch-time value after a restart.
            self.registry.update(
                self.id,
                model=self.sess.harness.model,  # keep the column in sync for summaries
                harness_json=self._safe_harness_json(),
            )
        await self.runner.send(P.reconfig_cmd(model=model, permission_mode=permission_mode))

    async def rewind(self, message_id: str, mode: str = "files") -> None:
        from terracore import protocol as P

        await self.runner.send(P.rewind_cmd(message_id, mode))

    async def stop(self) -> None:
        self._stopping = True   # deliberate — the pump must not flag this as a worker death
        await self.runner.stop()
        if self._pump_task:
            try:
                await asyncio.wait_for(self._pump_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._pump_task.cancel()

    async def detach(self) -> None:
        """Release the worker on graceful orchestrator shutdown.

        For a **durable** runner (k8s) leave the Pod running and suppress the
        terminal write so the next boot can ``reattach``. For a non-durable
        runner the worker is gone for good, so DON'T suppress ``session_end`` —
        otherwise the row dangles as "running" forever (rehydrate is k8s-only)."""
        self._detaching = bool(getattr(self.runner, "durable", False))
        self._stopping = True   # graceful release, not a crash
        try:
            await self.runner.detach()
        finally:
            if self._pump_task:
                try:
                    await asyncio.wait_for(self._pump_task, timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._pump_task.cancel()

    async def stream(
        self, after: int = -1, replay_limit: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # subscribe FIRST so nothing emitted during replay is lost, then dedupe.
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SUB_QUEUE_MAX)
        self._subs.add(q)
        try:
            last = after
            if after < 0 and replay_limit:
                replay, truncated = tail_events(self.store.path, replay_limit)
            else:
                replay, truncated = self.store.read(after), False
            if truncated and replay:
                yield {
                    "seq": int(replay[0].get("seq", 0)) - 1,
                    "ts": now_iso(),
                    "type": "_history_truncated",
                    "retained": len(replay),
                }
            for ev in replay:
                last = int(ev["seq"])
                yield ev
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield _STREAM_HEARTBEAT  # idle keepalive (rendered as an SSE comment)
                    continue
                if ev is _STREAM_OVERFLOW:
                    return  # fell behind → end stream; client reconnects with after=last + replays
                if ev.get("seq") is None:
                    yield ev  # transient (live-only) — yield without advancing the replay cursor
                    continue
                if int(ev["seq"]) > last:
                    last = int(ev["seq"])
                    yield ev
                if ev.get("type") == "session_end":
                    return
        finally:
            self._subs.discard(q)

    def summary(self, folded: "tuple[int, dict[str, Any]] | None" = None) -> dict[str, Any]:
        # ``folded`` = a pre-computed (event_count, agg) from the manager's mtime-keyed cache,
        # so the polled list path doesn't re-read + re-fold an unchanged log per request.
        if folded is None:
            events = self.store.read()
            event_count, agg = len(events), summarize_events(events)
        else:
            event_count, agg = folded
        return {
            "id": self.id,
            "status": self.status,
            "agent_id": self.sess.agent_id,
            "memory_volume": self.sess.memory_volume,
            "memory_isolated": self.sess.memory_isolated,
            "model": self.sess.model,
            "system_mode": self.sess.system_mode,
            "title": self.sess.title,
            # When this session started (ISO-8601 UTC). The list had no time field at all, so the
            # console could show neither an age nor a meaningful order.
            "created_ts": self.created_ts or agg["started_ts"],
            "user_turns": agg["user_turns"],
            "tool_calls": agg["tool_calls"],
            "event_count": event_count,
            "total_cost_usd": agg["total_cost_usd"],
            "tokens": agg["tokens"],
            "context": agg["context"],  # latest context-window usage (None until a turn completes)
            # Opaque to clients: seed a reattached stream's `after=` with it. -1 = start of log.
            "resume_cursor": agg["resume_cursor"],
            # null when the run ended normally; "budget" / "lost" when it didn't.
            "terminal": agg["terminal"],
        }


class SessionManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.sessions: dict[str, Session] = {}
        self.registry = SessionRegistry(config.runtime_dir / "sessions.db")
        self.notifier = Notifier(config.notify_webhook_url, config.notify_on)
        # sid -> ((mtime_ns, size), event_count, agg): memoizes the JSONL fold for the polled
        # list path so an unchanged log (any terminated session; a live one between turns)
        # isn't re-read + re-summarized every request.
        self._fold_cache: dict[str, tuple[tuple[int, int], int, dict[str, Any]]] = {}
        # runtime-backstop watchdog: catches a turn wedged with NO events (the event-driven
        # budget check can't fire without an event). Started explicitly by the API lifespan.
        self._wd_task: asyncio.Task | None = None
        self._wd_stop = asyncio.Event()
        # audit-drain sweep: mirrors each live session's Warden audit onto our own volume.
        self._audit_task: asyncio.Task | None = None
        self._audit_stop = asyncio.Event()
        # Wired by the API lifespan (optional so SessionManager(config) stays test-cheap).
        self.egress_store: Any = None    # EgressPolicyStore (global)
        self.profile_store: Any = None   # EgressProfileStore (per-agent bundles)
        self.environment_store: Any = None  # EnvironmentStore ({secrets, egress profile} bundles)

    def _env_secret_names(self, sess: SessionConfig) -> set[str]:
        """The operator-secret names explicitly granted by attached environments.

        No environments and dangling environments both resolve to an empty set. Secret
        access is therefore opt-in and adding a global secret cannot silently expand an
        unrelated agent's authority."""
        env_ids = getattr(sess.harness, "environments", None)
        if not env_ids:
            return set()
        store = self.environment_store
        names: set[str] = set()
        if store is not None:
            for eid in env_ids:
                env = store.get(eid)
                if env:
                    names.update(env.get("secrets", []))
        return names

    def _effective_egress_base(self, sess: SessionConfig) -> "dict[str, Any] | None":
        """The merged egress rule bundle for a session (before folding in secret-scope
        hosts + the global kill). The egress profiles of the agent's attached ENVIRONMENTS
        are the sole source (there is no per-agent egress pin). If any resolve, they MERGE
        (enforce wins over monitor — stricter; allow/deny/inspect union — attaching an
        environment can only widen the reachable set, never silently narrow it). If none
        resolve, the GLOBAL policy is the base."""
        prof_store = self.profile_store
        env_ids = list(getattr(sess.harness, "environments", None) or [])
        ids: list[str] = []
        if self.environment_store is not None:
            for eid in env_ids:
                env = self.environment_store.get(eid)
                if env is None:
                    return {"mode": "enforce", "rules": [], "hosts": []}
                if env and env.get("egress_profile"):
                    ids.append(env["egress_profile"])
        profs = [p for p in (prof_store.get(pid) for pid in dict.fromkeys(ids)) if p] if prof_store else []
        if ids and len(profs) != len(dict.fromkeys(ids)):
            return {"mode": "enforce", "rules": [], "hosts": []}
        if not profs:
            return self.egress_store.get() if self.egress_store else None
        # merge: concat each profile's rules, de-duped by (action, dest, ports); any enforce ⇒
        # enforce (stricter wins). Attaching an environment can only widen the rule set.
        mode = "monitor"
        merged: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        hosts: list[dict[str, str]] = []
        seen_hosts: set[str] = set()
        for p in profs:
            if p.get("mode") == "enforce":
                mode = "enforce"
            for r in p.get("rules", []):
                key = (r.get("action"), r.get("dest"), tuple(r.get("ports") or ()))
                if key not in seen:
                    seen.add(key)
                    merged.append(r)
            for h in p.get("hosts", []):  # host→ip overrides; first profile wins on a conflict
                if h.get("host") not in seen_hosts:
                    seen_hosts.add(h["host"])
                    hosts.append(h)
        return {"mode": mode, "rules": merged, "hosts": hosts}

    def _resolve_session_policy(self, sess: SessionConfig) -> str | None:
        """The effective Warden policy JSON for a session: the merged egress bundle from the
        agent's profile + environments, else the GLOBAL policy. The global KILL switch always
        wins (panic button). Returns None when no stores are wired (tests) so the runner
        falls back to its default."""
        base = self._effective_egress_base(sess)
        if base is None:
            return None
        glob = self.egress_store.get() if self.egress_store else None
        rules = list(base.get("rules", []))
        # Operator-secret scope hosts must be MITM'd for injection to happen — append them as
        # inspect rules (MITM + scan, no auto-block), like the always-MITM Anthropic hosts. Only
        # THIS session's granted secrets count (environments scope the injection map).
        for h in self._secret_scope_hosts(sess):
            rules.append({"action": "inspect", "dest": h, "ports": None, "enabled": True,
                          "note": "operator-secret injection"})
        # Static host→ip overrides (internal DNS) are infrastructural, not per-profile: an operator
        # who sets one on the GLOBAL policy expects every session to resolve that name. So union the
        # global overrides in (like kill/allow_metadata below), not just the resolved profiles'.
        # A profile-specific override wins on a name conflict (more specific); global fills the rest.
        # When no profiles resolved, base already IS the global policy, so this is an idempotent no-op.
        hosts = list(base.get("hosts", []) or [])
        seen_hosts = {h.get("host") for h in hosts if h.get("host")}
        for h in ((glob.get("hosts", []) if glob else []) or []):
            if h.get("host") and h["host"] not in seen_hosts:
                hosts.append(h)
                seen_hosts.add(h["host"])
        return json.dumps({
            "mode": base.get("mode", "enforce"),
            "rules": rules,
            "hosts": hosts,   # static host→ip resolve overrides (internal DNS): profile ∪ global
            "kill": bool(glob.get("kill")) if glob else False,
            # cloud-metadata reachability is a single operator-level decision (the global policy),
            # not per-profile — a broad internal allow CIDR must not silently reopen the pivot.
            "allow_metadata": bool(glob.get("allow_metadata")) if glob else False,
        })

    def _secret_scope_hosts(self, sess: SessionConfig) -> list[str]:
        store = getattr(self, "secret_store", None)
        return store.scope_hosts(self._env_secret_names(sess)) if store is not None else []

    def _session_uses_profile(self, sess: SessionConfig, profile_id: str) -> bool:
        """True if any of the session's attached environments references this egress
        profile — so editing a profile re-pushes only to the sessions it actually affects."""
        if self.environment_store is None:
            return False
        for eid in (getattr(sess.harness, "environments", None) or []):
            env = self.environment_store.get(eid)
            if env and env.get("egress_profile") == profile_id:
                return True
        return False

    def _resolve_session_secrets(self, sess: SessionConfig) -> str | None:
        """The WARDEN_SECRETS payload for a session — operator injection secrets with the
        value templated in, scoped to the agent's environments. No environment grants no
        secret. None when nothing applies (Warden then has no rules)."""
        store = getattr(self, "secret_store", None)
        if store is None:
            return None
        payload = store.warden_payload(self._env_secret_names(sess))
        return json.dumps(payload) if payload.get("secrets") else None

    def _memory_busy(self, memory_volume: str) -> bool:
        """Is a live session already holding this (single-writer RWO) memory volume?

        Checks the IN-MEMORY sessions first — they're registered synchronously at
        create (before the first await), whereas the registry row isn't written
        until the Pod reaches Running (~seconds later). Without the in-memory check
        two concurrent creates for the same agent both read 'free' and both bind the
        RWO volume → the second Pod hangs forever on Multi-Attach (the create-time
        TOCTOU). asyncio runs create()'s pre-await section atomically, so an
        in-memory reservation serializes concurrent creates correctly."""
        if any(s.status in LIVE_STATUSES and s.sess.memory_volume == memory_volume
               for s in self.sessions.values()):
            return True
        return any(r.get("memory_volume") == memory_volume
                   for r in self.registry.list(statuses=LIVE_STATUSES))

    def _resolve_memory(self, memory_volume: str, sid: str) -> tuple[str, bool]:
        """An agent is just config, so it can run many sessions at once — but the memory
        volume is single-writer: concurrent writers corrupt it, and on k8s a second mount
        additionally hangs forever (Multi-Attach). So if the agent's shared memory is already
        live, give this session its OWN isolated memory (runs concurrently, just doesn't share
        live memory). Returns (volume, isolated).

        This used to be gated on runner == "k8s", because that is where the symptom is loud:
        Kubernetes refuses the second mount, while Docker happily attaches the same volume to
        two sandboxes and lets them interleave writes. The corruption risk is the same on both
        — Docker just fails silently — so the guard applies everywhere."""
        if self._memory_busy(memory_volume):
            return f"{memory_volume}-{sid.rsplit('-', 1)[-1]}", True
        return memory_volume, False

    def _check_capacity(self, agent_id: str | None) -> None:
        # Include durable rows that survived a control-plane restart, de-duplicated
        # against their reattached in-memory Session objects.
        live: dict[str, str | None] = {
            sid: s.sess.agent_id for sid, s in self.sessions.items()
            if s.status in LIVE_STATUSES
        }
        for row in self.registry.list(statuses=LIVE_STATUSES):
            live.setdefault(row["session_id"], row.get("agent_id"))
        total_cap = self.config.max_live_sessions
        if total_cap and len(live) >= total_cap:
            raise CapacityError(f"live session capacity reached ({total_cap})")
        agent_cap = self.config.max_live_sessions_per_agent
        if agent_id and agent_cap and sum(aid == agent_id for aid in live.values()) >= agent_cap:
            raise CapacityError(
                f"live session capacity reached for agent {agent_id!r} ({agent_cap})"
            )

    async def create(self, sess_cfg: SessionConfig) -> Session:
        # create() runs synchronously until its first await, so this check and the
        # reservation below are atomic with respect to concurrent API requests.
        self._check_capacity(sess_cfg.agent_id)
        sid = _new_id()
        volume, isolated = self._resolve_memory(sess_cfg.memory_volume, sid)
        if isolated:
            sess_cfg = replace(sess_cfg, memory_volume=volume, memory_isolated=True)
        # Resolve the per-session Warden policy (agent egress profile, else global).
        sess_cfg = replace(sess_cfg, egress_policy_json=self._resolve_session_policy(sess_cfg),
                           warden_secrets_json=self._resolve_session_secrets(sess_cfg))
        session = Session(sid, self.config, sess_cfg, self.registry, self.notifier)
        # Register before the first await to reserve an RWO memory scope against a
        # concurrent create. Roll it back if any launch step fails.
        self.sessions[sid] = session
        try:
            await session.start()
        except BaseException:
            self.sessions.pop(sid, None)
            try:
                await session.runner.stop()
            except Exception:  # noqa: BLE001
                log.warning("failed-start cleanup failed for session %s", sid, exc_info=True)
            raise
        return session

    async def propagate_egress_policy(self, profile_id: str | None = None) -> None:
        """Re-resolve + push the effective policy to live sessions' Warden, so a console
        change reaches RUNNING sessions (k8s ConfigMaps), not just new ones.

        ``profile_id=None`` (a global-policy edit) re-resolves EVERY session — the global
        kill switch must reach every session too. A profile edit re-resolves only the
        sessions whose attached environments reference that profile."""
        for s in list(self.sessions.values()):
            if profile_id is not None and not self._session_uses_profile(s.sess, profile_id):
                continue
            policy_json = self._resolve_session_policy(s.sess)
            if policy_json is None:
                continue
            s.sess.egress_policy_json = policy_json
            try:
                await s.runner.update_warden_policy(policy_json)
            except Exception:  # noqa: BLE001
                # A policy push that didn't reach a live session is security-relevant
                # (a kill switch / tightened allow-list the operator believes applied).
                log.warning("egress policy push failed for session %s", s.id, exc_info=True)

    async def propagate_agent_harness(self, agent_id: str, harness_updates: dict[str, Any]) -> list[str]:
        """Live-apply an agent edit to that agent's RUNNING sessions, for the fields that
        can take effect WITHOUT relaunching the sandbox. Most harness fields (model,
        prompt, tools, thinking…) are baked into the container at launch and need a new
        session; only two apply hot:

        - ``max_budget_usd`` — enforced host-side in the pump (``_enforce_budget`` reads
          ``sess.harness`` live), so updating the live session's value is enough.
        - ``environments`` — re-resolve the effective egress policy AND the injected-secret
          map (environments scope both) and push both to the session's Warden (the same
          hot-reload path a console egress/secret edit uses).

        ``harness_updates`` is the explicitly-provided set (the api passes
        model_dump(exclude_unset=True)), so a key present with value ``None`` is a real
        clear (e.g. budget → unlimited, environments → none) and must apply — test key
        presence, not truthiness, or the clear silently won't reach running sessions.
        Returns the session ids that were updated, so the caller can tell the operator
        what took effect live."""
        touched: list[str] = []
        for s in list(self.sessions.values()):
            if s.sess.agent_id != agent_id:
                continue
            changed = False
            if "max_budget_usd" in harness_updates:
                s.sess.harness.max_budget_usd = harness_updates["max_budget_usd"]
                changed = True
            if harness_updates.get("model"):  # live model switch (set_model — cache penalty)
                await s.reconfigure(model=harness_updates["model"])
                changed = True
            if "environments" in harness_updates:
                s.sess.harness.environments = harness_updates["environments"]
                policy_json = self._resolve_session_policy(s.sess)
                if policy_json is not None:
                    s.sess.egress_policy_json = policy_json
                    try:
                        await s.runner.update_warden_policy(policy_json)
                    except Exception:  # noqa: BLE001
                        log.warning("egress policy push failed for session %s", s.id, exc_info=True)
                secrets_json = self._resolve_session_secrets(s.sess) or json.dumps({"secrets": []})
                s.sess.warden_secrets_json = secrets_json
                try:
                    await s.runner.update_warden_secrets(secrets_json)
                except Exception:  # noqa: BLE001
                    log.warning("secret push failed for session %s", s.id, exc_info=True)
                changed = True
            if changed:
                touched.append(s.id)
        return touched

    async def propagate_credentials(self) -> None:
        """Push the current credential to every live session's Warden, so a refresh or
        console re-paste reaches RUNNING sessions (otherwise they 401 once their
        start-time token expires)."""
        for s in list(self.sessions.values()):
            try:
                await s.runner.update_warden_cred()
            except Exception:  # noqa: BLE001
                # A credential that didn't reach a live session means it will 401 once its
                # start-time token expires — surface it rather than swallow (H2).
                log.warning("credential push failed for session %s", s.id, exc_info=True)

    async def propagate_secrets(self) -> None:
        """An operator secret OR environment changed — re-resolve each live session's
        injection map AND policy and push both to its Warden. Resolution is now per-session
        (an environment scopes which secrets/hosts a given agent gets), so the payload can
        differ across sessions — no single global payload anymore."""
        for s in list(self.sessions.values()):
            try:
                policy_json = self._resolve_session_policy(s.sess)
                if policy_json is not None:
                    s.sess.egress_policy_json = policy_json
                    await s.runner.update_warden_policy(policy_json)
                secrets_json = self._resolve_session_secrets(s.sess) or json.dumps({"secrets": []})
                s.sess.warden_secrets_json = secrets_json
                await s.runner.update_warden_secrets(secrets_json)
            except Exception:  # noqa: BLE001
                log.warning("secret push failed for session %s", s.id, exc_info=True)

    # An environment change (secrets set or egress profile) affects live sessions exactly
    # like a secret change — re-resolve + push. Alias so the API reads intently.
    propagate_environments = propagate_secrets

    def get(self, sid: str) -> Session | None:
        return self.sessions.get(sid)

    async def recover(self, sid: str) -> str | None:
        """Reattach to a TERMINATED session whose sandbox is still running.

        Same machinery as boot-time rehydrate, aimed at one session. Returns the restored
        status, or None when the sandbox really is gone. Raises LookupError for an unknown id.
        """
        row = self.registry.get(sid)
        if row is None and not (self.config.logs_dir / f"{sid}.jsonl").exists():
            raise LookupError(sid)
        # Drop any stale in-memory Session for this id. It is terminated (the caller checked)
        # and its dead runner would otherwise shadow the fresh one we are about to attach.
        self.sessions.pop(sid, None)
        row = row or {"session_id": sid}
        try:
            harness = Harness.from_json(row.get("harness_json") or "{}")
        except Exception:  # noqa: BLE001 — a torn row must not block a live sandbox's recovery
            harness = Harness()
        sess_cfg = SessionConfig(
            harness=harness, title=row.get("title"),
            memory_volume=row.get("memory_volume") or self.config.memory_volume,
            agent_id=row.get("agent_id"),
        )
        session = Session(sid, self.config, sess_cfg, self.registry, self.notifier)
        if await session.runner.probe_state() != "running":
            return None
        await session.reattach(status="idle", created_ts=row.get("created_ts"))
        self.sessions[sid] = session
        session.store.append("recovered", reason="manual")
        self.registry.update(sid, status=session.status)
        log.info("session %s recovered — sandbox was alive", sid)
        return session.status

    async def rehydrate(self) -> set[str]:
        """Reattach to sandbox Pods that outlived an orchestrator restart.

        Returns the set of session **ids** to keep (reattached + transiently
        unreadable), so the caller spares their Pods + creds Secrets from
        ``cleanup_orphans``. Runs for any *durable* runner (docker detached
        container or k8s Pod); the local dev runner is a bare child subprocess
        that cannot survive the orchestrator process dying.
        """
        kept: set[str] = set()
        if self.config.runner == "local":
            return kept
        for row in self.registry.list(statuses=LIVE_STATUSES):
            sid = row["session_id"]
            if sid in self.sessions:
                continue
            try:
                try:
                    harness = Harness.from_json(row["harness_json"] or "{}")
                except Exception:
                    harness = Harness()  # a torn registry row must not reap a live Pod
                sess_cfg = SessionConfig(
                    harness=harness,
                    title=row["title"],
                    memory_volume=row["memory_volume"] or self.config.memory_volume,
                    agent_id=row["agent_id"],
                )
                # Reconstruct the security state before the runner/controller is
                # built. In particular Docker reattach needs deterministic Warden
                # paths plus the current resolved policy and secret payload.
                sess_cfg = replace(
                    sess_cfg,
                    egress_policy_json=self._resolve_session_policy(sess_cfg),
                    warden_secrets_json=self._resolve_session_secrets(sess_cfg),
                )
                session = Session(sid, self.config, sess_cfg, self.registry, self.notifier)
                state = await session.runner.probe_state()
                if state == "running":
                    await session.reattach(status=row["status"], created_ts=row.get("created_ts"))
                    self.sessions[sid] = session
                    kept.add(sid)
                elif state == "unknown":
                    kept.add(sid)  # transient API error / Pending — keep, retry next boot
                else:  # gone (404 / Failed / Succeeded)
                    self._mark_orphaned(sid, session)
            except Exception:
                self._mark_orphaned(sid, None)
        return kept

    def _mark_orphaned(self, sid: str, session: "Session | None") -> None:
        """The Pod is gone — close the session out in the JSONL + registry so it
        stays inspectable instead of dangling as 'running' forever."""
        try:
            store = session.store if session else EventStore(self.config.logs_dir / f"{sid}.jsonl")
            store.append("session_end", reason="orphaned_restart")
        except Exception:
            pass
        self.registry.update(sid, status="terminated")

    def _fold_log(self, sid: str) -> "tuple[int, dict[str, Any]]":
        """Fold a session's JSONL into (event_count, agg), memoized by the file's (mtime, size).
        An append changes both, so a live session re-folds only after it grows; a terminated
        log never changes → always a cache hit. Caches the small agg, never the events list."""
        path = self.config.logs_dir / f"{sid}.jsonl"
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        hit = self._fold_cache.get(sid)
        if hit is not None and key is not None and hit[0] == key:
            return hit[1], hit[2]
        events = EventStore(path).read()
        count, agg = len(events), summarize_events(events)
        if key is not None:
            self._fold_cache[sid] = (key, count, agg)
        return count, agg

    def _ordered_index(self) -> list[tuple[str, str, Any]]:
        """``(created_ts, session_id, source)`` for every session, newest first.

        Deliberately does NOT summarize: ordering only needs the timestamp, so a paged
        listing folds the page's logs, not the whole fleet's. Sorted on the one key that
        means something, with the id as a tiebreak so the order is total (ids are
        timestamp-prefixed, and equal timestamps must not order randomly).

        ``source`` is the live Session or the registry row, whichever the summary will
        come from.
        """
        out: list[tuple[str, str, Any]] = []
        for sid, s in self.sessions.items():
            out.append((str(s.created_ts or ""), sid, s))
        for row in self.registry.list():
            sid = row["session_id"]
            if sid in self.sessions:
                continue
            ts = row.get("created_ts")
            if not ts:
                # Pre-created_ts row: recover the time from the log's first event, as the
                # summary does. Memoized by mtime, so this costs one fold per legacy row
                # ever, not per request.
                ts = self._fold_log(sid)[1]["started_ts"]
            out.append((str(ts or ""), sid, row))
        out.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return out

    def _summarize(self, sid: str, source: Any) -> dict[str, Any]:
        return (source.summary(folded=self._fold_log(sid)) if isinstance(source, Session)
                else self._summary_from_log(source))

    def list(self) -> list[dict[str, Any]]:
        """Every session, newest first. Folds every log — used by the internal fan-out in
        /v1/logs (itself capped). Prefer ``list_page`` for anything client-facing."""
        return [self._summarize(sid, src) for _ts, sid, src in self._ordered_index()]

    def list_metadata(self) -> list[dict[str, Any]]:
        """Session fields needed to select/facet logs, without folding event files."""
        rows: list[dict[str, Any]] = []
        for created_ts, sid, source in self._ordered_index():
            if isinstance(source, Session):
                rows.append({
                    "id": sid, "agent_id": source.sess.agent_id,
                    "title": source.sess.title, "status": source.status,
                    "created_ts": created_ts,
                })
            else:
                rows.append({
                    "id": sid, "agent_id": source.get("agent_id"),
                    "title": source.get("title"), "status": source.get("status"),
                    "created_ts": created_ts,
                })
        return rows

    def list_page(self, limit: int, before: str | None = None) -> dict[str, Any]:
        """One page of sessions, newest first, plus the counts a client needs.

        Sessions are durable and accumulate forever, so an unbounded listing grows without
        limit in payload and in render cost — on a 5s poll. The cursor is opaque
        (``created_ts|id``, the same total order the index sorts on), which is why it can
        page a list that mixes live sessions with registry rows.

        ``running``/``total`` are counted over the WHOLE fleet, not the page: the console's
        live badge and "N sessions" must not shrink as you page.
        """
        index = self._ordered_index()
        total = len(index)
        running = sum(1 for _ts, sid, src in index
                      if (src.status if isinstance(src, Session) else src.get("status")) in LIVE_STATUSES)
        start = 0
        if before:
            # Resume strictly AFTER the cursor row. Comparing the composite key (rather than
            # searching for the id) keeps paging stable when the row it names has since been
            # deleted — the page picks up at the same position instead of restarting.
            key = tuple(before.split("|", 1)) if "|" in before else (before, "")
            start = next((i for i, (ts, sid, _s) in enumerate(index) if (ts, sid) < key), total)
        page = index[start:start + limit]
        nxt = f"{page[-1][0]}|{page[-1][1]}" if page and start + limit < total else None
        return {
            "sessions": [self._summarize(sid, src) for _ts, sid, src in page],
            "next_cursor": nxt,
            "total": total,
            "running": running,
        }

    def usage_totals(self, since_iso: str) -> dict[str, Any]:
        """Token + tool-call totals across sessions created in the window.

        Separate from the spend ledger on purpose, and the difference is worth stating: cost
        is recorded in the ledger and survives session deletion, while tokens live only in the
        per-session JSONL. So a deleted session keeps contributing to cost and stops
        contributing to tokens — which is the truth, not a bug to paper over.

        Lives here rather than in the console because the console now pages the session list,
        and folding a page would silently under-report the fleet.
        """
        tokens = {"input": 0, "output": 0, "cacheRead": 0, "cacheCreate": 0, "subagent": 0, "total": 0}
        tools = 0
        by_model: dict[str, float] = {}
        for ts, sid, src in self._ordered_index():
            if ts and ts < since_iso:
                break  # newest-first, so everything below is older too
            summary = self._summarize(sid, src)
            tools += summary["tool_calls"]
            for k in ("input", "output", "cacheRead", "cacheCreate", "subagent"):
                tokens[k] += summary["tokens"].get(k, 0)
            model = summary.get("model") or "unknown"
            by_model[model] = by_model.get(model, 0.0) + float(summary.get("total_cost_usd") or 0)
        tokens["total"] = sum(tokens[k] for k in ("input", "output", "cacheRead", "cacheCreate", "subagent"))
        return {
            "tokens": tokens,
            "tool_calls": tools,
            "by_model": sorted(({"model": m, "total_cost_usd": round(c, 6)} for m, c in by_model.items()),
                               key=lambda r: r["total_cost_usd"], reverse=True),
        }

    def _summary_from_log(self, row: dict[str, Any]) -> dict[str, Any]:
        """Read-only summary for a session not held in memory (terminated, or
        reaped on restart) — folds its JSONL log + the registry metadata."""
        events_count, agg = self._fold_log(row["session_id"])
        return {
            "id": row["session_id"],
            "status": row.get("status") or "terminated",
            "agent_id": row.get("agent_id"),
            "memory_volume": row.get("memory_volume"),
            "model": row.get("model"),
            "system_mode": row.get("system_mode"),
            "title": row.get("title"),
            # Registry row if we have one, else the log's first event — so a session whose row
            # was reaped (or that only ever existed as a log) still reports when it ran.
            "created_ts": row.get("created_ts") or agg["started_ts"],
            "user_turns": agg["user_turns"],
            "tool_calls": agg["tool_calls"],
            "event_count": events_count,
            "total_cost_usd": agg["total_cost_usd"],
            "tokens": agg["tokens"],
            "context": agg["context"],  # latest context-window usage (None until a turn completes)
            # Terminated sessions still carry a cursor so a consumer can drain the tail
            # (incl. session_end) instead of guessing whether it missed anything.
            "resume_cursor": agg["resume_cursor"],
            # null when the run ended normally; "budget" / "lost" when it didn't.
            "terminal": agg["terminal"],
        }

    def summary_of(self, sid: str) -> dict[str, Any] | None:
        """Summary for any session id — live, or read-only from the log/registry."""
        live = self.sessions.get(sid)
        if live:
            return live.summary(folded=self._fold_log(sid))
        row = self.registry.get(sid)
        if row:
            return self._summary_from_log(row)
        if (self.config.logs_dir / f"{sid}.jsonl").exists():
            return self._summary_from_log({"session_id": sid})
        return None

    async def read_only_stream(
        self, sid: str, after: int = -1, replay_limit: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Replay a non-live session's JSONL once (no live subscription).

        If the log has no terminal ``session_end`` (the session ended uncleanly —
        crashed, killed, detached non-durably), emit a SYNTHETIC one so the client
        stops. Otherwise the SDK/console can't tell "done" from "dropped mid-stream"
        and reconnect-loops forever, pegging a core and hammering the orchestrator."""
        saw_end = False
        last = after
        path = self.config.logs_dir / f"{sid}.jsonl"
        if after < 0 and replay_limit:
            replay, truncated = tail_events(path, replay_limit)
        else:
            replay, truncated = EventStore(path).read(after), False
        if truncated and replay:
            yield {
                "seq": int(replay[0].get("seq", 0)) - 1,
                "ts": now_iso(),
                "type": "_history_truncated",
                "retained": len(replay),
            }
        for ev in replay:
            last = int(ev.get("seq", last))
            if ev.get("type") == "session_end":
                saw_end = True
            yield ev
        if not saw_end:
            yield {"seq": last + 1, "ts": now_iso(), "type": "session_end",
                   "reason": "stream_closed", "synthetic": True}

    async def delete(self, sid: str) -> bool:
        session = self.sessions.pop(sid, None)
        log = self.config.logs_dir / f"{sid}.jsonl"
        if not session and not self.registry.get(sid) and not log.exists():
            return False
        if session:
            await session.stop()
        self.registry.remove(sid)
        self._fold_cache.pop(sid, None)  # drop the memoized fold for the now-deleted log
        try:
            log.unlink(missing_ok=True)  # so a deleted session 404s, not 200s via the log fallback
        except Exception:
            pass
        return True

    def start_background(self) -> None:
        """Start the periodic sweeps (idempotent): the runtime backstop and the egress-audit
        drain. Each no-ops itself when its config disables it, so nothing spins for nothing."""
        if self._wd_task is None and self.config.budget_max_run_seconds:
            self._wd_stop.clear()
            self._wd_task = asyncio.create_task(self._run_watchdog())
        if self._audit_task is None and (
            self.config.audit_drain_seconds or self.config.max_audit_log_bytes
        ):
            self._audit_stop.clear()
            self._audit_task = asyncio.create_task(self._run_audit_drain())

    async def _run_audit_drain(self) -> None:
        """Mirror live sessions' Warden audits onto our runtime volume on a fixed interval.

        This is what makes the audit durable *and* cheap. It replaces a per-request,
        per-session pod-exec fanout: the console polled /v1/logs and /v1/egress/audit every
        6s, so N open tabs × M live sessions execs hit the cluster, serialized inside the
        request handler. Now it's one exec per session per interval regardless of how many
        clients are watching, and the reads are local file reads."""
        interval = max(2, self.config.audit_drain_seconds or 10)
        while not self._audit_stop.is_set():
            try:
                await asyncio.wait_for(self._audit_stop.wait(), timeout=interval)
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            for s in list(self.sessions.values()):
                try:
                    if self.config.audit_drain_seconds:
                        await s.runner.drain_audit()
                    await s.enforce_storage_limits()
                except Exception:  # noqa: BLE001 — one unreachable session must not stop the sweep
                    log.debug("audit drain: failed for %s", s.id, exc_info=True)

    async def _run_watchdog(self) -> None:
        # Sweep often enough to bound overrun without busy-work: a quarter of the limit,
        # clamped to [15s, 60s]. A wedged turn is then killed within ~one interval of its cap.
        interval = max(15, min(60, self.config.budget_max_run_seconds // 4))
        while not self._wd_stop.is_set():
            try:
                await asyncio.wait_for(self._wd_stop.wait(), timeout=interval)
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            for s in list(self.sessions.values()):
                try:
                    await s.enforce_runtime_backstop()
                except Exception:  # noqa: BLE001 — one wedged session must not stop the sweep
                    log.warning("runtime watchdog: enforce failed for %s", s.id, exc_info=True)

    async def shutdown(self) -> None:
        self._wd_stop.set()
        self._audit_stop.set()
        for task in (self._wd_task, self._audit_task):
            if task:
                try:
                    await asyncio.wait_for(task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    task.cancel()
        # detach (don't destroy) so k8s sessions survive for the next boot
        # — Session.detach drains the audit first, so nothing is lost here.
        await asyncio.gather(*(s.detach() for s in self.sessions.values()), return_exceptions=True)
        self.sessions.clear()
        self.registry.close()
