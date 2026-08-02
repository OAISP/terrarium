"""Async client for the terrarium orchestrator API.

The SDK is **async-only**, mirroring the Claude Agent SDK (``async with``, ``async for``,
awaitable ``can_use_tool``). The synchronous CLI bridges to it with ``asyncio.run`` at its
own entry point.

    import asyncio
    from terrarium import TerrariumClient, TerrariumOptions

    async def main():
        async with TerrariumClient("http://127.0.0.1:8900") as client:
            async with client.session(options=TerrariumOptions(model="sonnet")) as s:
                async for msg in s.receive_response("What is 12 * 9?"):
                    print(msg)

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, AsyncIterator, Callable, Literal

import httpx

from .errors import NotFoundError, TerrariumError, TransportError, from_status, is_transient
from .messages import AssistantMessage, Message, parse_message
from .options import (
    _HARNESS_FIELDS, CanUseTool, PermissionResultAllow, PermissionResultDeny,
    TerrariumOptions, ToolPermissionContext,
)

from . import __version__

# Closed enums — typed so a caller typo ("acceptedits", "allowed") is caught by the type
# checker at the call site instead of failing server-side (or silently mis-applying).
Decision = Literal["allow", "always", "deny"]
PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]
RewindMode = Literal["files", "conversation", "both"]

_DEFAULT_TIMEOUT = 30.0   # a hung orchestrator must not hang the caller forever
_MAX_RETRIES = 3          # transient (connection / 429 / 5xx) retries with backoff
_USER_AGENT = f"terrarium/{__version__}"
# Wire-protocol the SDK speaks. Sent on every request; the orchestrator echoes its own
# on each response (``X-Terrarium-Protocol``). A mismatch is surfaced once as a warning
# rather than a cryptic KeyError downstream — see ``_Http._check_protocol``.
PROTOCOL_VERSION = 1


def _backoff(attempt: int) -> float:
    return min(0.25 * (2 ** attempt), 5.0)


def _turn_ended(ev: dict[str, Any]) -> bool:
    """Does this event end the current turn? (the result, an idle status, or session end)"""
    t = ev.get("type")
    return t == "result" or (t == "status" and ev.get("status") == "idle") or t == "session_end"


async def _resolve(value: Any) -> Any:
    """Await ``value`` if it's awaitable, else return it — lets ``can_use_tool`` be either a
    plain function or a coroutine function (the Claude SDK requires async; we accept both)."""
    return await value if inspect.isawaitable(value) else value


class _Http:
    def __init__(self, base_url: str, token: str | None, timeout: float | None) -> None:
        headers = {"User-Agent": _USER_AGENT, "X-Terrarium-Protocol": str(PROTOCOL_VERSION)}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.c = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=_DEFAULT_TIMEOUT if timeout is None else timeout,
        )
        self._proto_warned = False

    def _check_protocol(self, r: httpx.Response) -> None:
        """Warn once if the orchestrator speaks a different wire protocol — a clearer
        signal than the KeyError/NotFoundError a shape mismatch would otherwise raise."""
        srv = r.headers.get("X-Terrarium-Protocol")
        if srv and srv != str(PROTOCOL_VERSION) and not self._proto_warned:
            self._proto_warned = True
            import warnings
            warnings.warn(
                f"orchestrator protocol v{srv} != SDK v{PROTOCOL_VERSION} — "
                "upgrade the SDK or orchestrator if you hit shape errors.",
                stacklevel=2,
            )

    async def json(self, method: str, path: str, **kw) -> Any:
        # Only auto-retry IDEMPOTENT methods. A transient failure on a POST (e.g. a
        # ReadTimeout after the request reached the server but the response was lost)
        # could otherwise spawn a duplicate session or double-deliver a prompt. GET/
        # DELETE are safe to repeat; the streaming GET has its own resume loop.
        idempotent = method.upper() in ("GET", "DELETE", "HEAD", "OPTIONS")
        last: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                r = await self.c.request(method, path, **kw)
                self._check_protocol(r)
                r.raise_for_status()
                return r.json() if r.content else None
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last = e
                if attempt < _MAX_RETRIES and idempotent and is_transient(e):
                    await asyncio.sleep(_backoff(attempt))
                    continue
                if isinstance(e, httpx.HTTPStatusError):
                    raise from_status(e) from e
                raise TransportError(str(e)) from e
        raise TransportError(str(last))  # unreachable, satisfies type checkers

    async def raw(self, method: str, path: str, **kw) -> bytes:
        """Same request/retry/error mapping as :meth:`json`, but returns the body bytes —
        for endpoints that answer with a file rather than JSON."""
        idempotent = method.upper() in ("GET", "DELETE", "HEAD", "OPTIONS")
        last: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                r = await self.c.request(method, path, **kw)
                self._check_protocol(r)
                r.raise_for_status()
                return r.content
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last = e
                if attempt < _MAX_RETRIES and idempotent and is_transient(e):
                    await asyncio.sleep(_backoff(attempt))
                    continue
                if isinstance(e, httpx.HTTPStatusError):
                    raise from_status(e) from e
                raise TransportError(str(e)) from e
        raise TransportError(str(last))  # unreachable, satisfies type checkers

    async def aclose(self) -> None:
        await self.c.aclose()


class AgentsResource:
    def __init__(self, http: _Http) -> None:
        self._h = http

    async def create(self, name: str, *, memory_scope: str | None = None,
                     template: str | None = None, **harness: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if memory_scope:
            body["memory_scope"] = memory_scope
        if template:
            body["template"] = template
        body.update(_harness_body(harness))
        return await self._h.json("POST", "/v1/agents", json=body)

    async def list(self) -> list[dict[str, Any]]:
        return (await self._h.json("GET", "/v1/agents"))["agents"]

    async def get(self, agent_id: str) -> dict[str, Any]:
        return await self._h.json("GET", f"/v1/agents/{agent_id}")

    async def spend(self, agent_id: str) -> dict[str, Any]:
        """Cumulative budget ledger for the agent — total spend across ALL its sessions
        (all_time + last_24h + last_30d), each ``{sessions, total_cost_usd}``. Poll this to
        enforce a cumulative cap, beyond the per-session ``max_budget_usd``."""
        return await self._h.json("GET", f"/v1/agents/{agent_id}/spend")

    async def update(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        return await self._h.json("PATCH", f"/v1/agents/{agent_id}", json=fields)

    async def delete(self, agent_id: str, *, purge_memory: bool = False) -> dict[str, Any]:
        return await self._h.json("DELETE", f"/v1/agents/{agent_id}", params={"purge_memory": purge_memory})


class SessionsResource:
    def __init__(self, http: _Http) -> None:
        self._h = http

    async def create(self, *, agent_id: str | None = None, title: str | None = None,
                     memory_scope: str | None = None, **harness: Any) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if agent_id:
            body["agent_id"] = agent_id
        if title:
            body["title"] = title
        if memory_scope:
            body["memory_scope"] = memory_scope  # isolate/share the per-session memory volume
        body.update(_harness_body(harness))
        return await self._h.json("POST", "/v1/sessions", json=body)

    async def get(self, session_id: str) -> dict[str, Any]:
        return await self._h.json("GET", f"/v1/sessions/{session_id}")

    async def list(self) -> list[dict[str, Any]]:
        """Every session, newest first.

        The endpoint is paged (sessions accumulate for the life of the deployment), but
        that is an implementation detail here: this follows the cursor to the end so the
        return value is what it has always been. Use :meth:`list_page` to page yourself.
        """
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await self.list_page(limit=500, before=cursor)
            out.extend(page["sessions"])
            cursor = page.get("next_cursor")
            if not cursor:
                return out

    async def list_page(self, limit: int = 100, before: str | None = None) -> dict[str, Any]:
        """One page of sessions: ``{sessions, next_cursor, total, running}``.

        Pass a page's ``next_cursor`` back as ``before`` to continue. ``total``/``running``
        count the whole fleet, not the page."""
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        return await self._h.json("GET", "/v1/sessions", params=params)

    async def delete(self, session_id: str) -> None:
        await self._h.json("DELETE", f"/v1/sessions/{session_id}")

    async def send(self, session_id: str, content: "str | list[dict[str, Any]]") -> None:
        # text → {"text": ...}; a list of Anthropic content blocks (incl. image) → {"content": ...}
        body = {"content": content} if isinstance(content, list) else {"text": content}
        await self._h.json("POST", f"/v1/sessions/{session_id}/messages", json=body)

    async def recover(self, session_id: str) -> dict[str, Any]:
        """Reattach to a session marked terminated whose sandbox is still running.

        The orchestrator's event stream is a client of the sandbox, not the sandbox itself, so
        it can drop (a Docker daemon restart, a host suspend) while the agent is perfectly
        alive. The orchestrator reattaches automatically now; this is the manual counterpart
        for sessions stranded before that, or past the automatic retry budget.

        Raises ConflictError (409) when the sandbox really is gone — the transcript remains
        readable, but the conversation cannot be resumed.
        """
        return await self._h.json("POST", f"/v1/sessions/{session_id}/recover")

    async def interrupt(self, session_id: str) -> None:
        await self._h.json("POST", f"/v1/sessions/{session_id}/interrupt")

    async def answer(self, session_id: str, question_id: str, answers: dict[str, Any]) -> None:
        """Answer a pending AskUserQuestion (a ``question`` event in the stream). ``answers``
        maps each question's text to the chosen option label, a list of labels (multi-select),
        or free text. Build it from the event's ``questions`` array."""
        await self._h.json("POST", f"/v1/sessions/{session_id}/answer",
                           json={"question_id": question_id, "answers": answers})

    async def decide(self, session_id: str, request_id: str, decision: Decision) -> None:
        """Approve/deny a pending tool-permission request (a ``permission`` event).
        ``decision``: "allow" (once) | "always" (allow + remember this session) | "deny"."""
        await self._h.json("POST", f"/v1/sessions/{session_id}/permission",
                           json={"request_id": request_id, "decision": decision})

    async def client_tool_result(self, session_id: str, call_id: str,
                                 content: "str | list[dict[str, Any]]", is_error: bool = False) -> None:
        """Return the result of a client-bridged tool call (a ``client_tool_call`` event) so the
        blocked agent can continue. ``content`` is a string OR a list of Anthropic content blocks
        (so a tool can return an image: ``[{"type":"image","source":{...}}]``). Normally handled
        for you by ``tools=`` on the session."""
        await self._h.json("POST", f"/v1/sessions/{session_id}/tool_result",
                           json={"call_id": call_id, "content": content, "is_error": is_error})

    async def set_model(self, session_id: str, model: str) -> None:
        """Switch a running session's model live (the conversation continues; the next turn
        re-reads context uncached → input-cache penalty). No restart."""
        await self._h.json("POST", f"/v1/sessions/{session_id}/config", json={"model": model})

    async def set_permission_mode(self, session_id: str, mode: PermissionMode) -> None:
        """Switch a running session's permission mode live (default | acceptEdits | plan |
        bypassPermissions). Mirrors the Claude Agent SDK's ``set_permission_mode``."""
        await self._h.json("POST", f"/v1/sessions/{session_id}/config", json={"permission_mode": mode})

    async def verify_egress(self, session_id: str) -> dict[str, Any]:
        """Recompute Warden's tamper-evident egress-audit hash chain for this
        session; returns {ok, checked, first_break_seq, gap_before_seq, reason}."""
        return await self._h.json("GET", f"/v1/sessions/{session_id}/egress/verify")

    async def rewind(self, session_id: str, message_id: str, mode: RewindMode = "files") -> None:
        """Rewind a live session to the turn anchored by `message_id` (the uuid carried
        by a `rewind_point` event). mode: "files" restores the workspace, "conversation"
        truncates the transcript + resumes, "both" does both."""
        await self._h.json("POST", f"/v1/sessions/{session_id}/rewind",
                           json={"message_id": message_id, "mode": mode})

    async def upload_file(self, session_id: str, path: str, dest: str | None = None) -> dict[str, Any]:
        """Upload a local file into the live session's /workspace. Returns {name, size}."""
        import os
        name = dest or os.path.basename(path)
        with open(path, "rb") as fh:
            content = fh.read()
        return await self._h.json("POST", f"/v1/sessions/{session_id}/files/upload",
                                 files={"file": (name, content)}, data={"name": name})

    async def download_file(self, session_id: str, name: str, dest: str | None = None) -> bytes:
        """Read a file back out of the session's /workspace and return its bytes.

        The counterpart to :meth:`upload_file`, and how you collect what an agent
        produced. ``dest`` also writes the bytes to that local path.

        Names are restricted to ``[A-Za-z0-9._-]`` with no path separators, symlinks are
        refused, and the file must be under 25 MiB — the sandbox is untrusted, so both the
        name and its target are attacker-chosen.
        """
        from urllib.parse import quote

        data = await self._h.raw("GET", f"/v1/sessions/{session_id}/files/{quote(name, safe='')}")
        if dest:
            with open(dest, "wb") as fh:
                fh.write(data)
        return data

    async def stream(self, session_id: str, after: int = -1) -> AsyncIterator[dict[str, Any]]:
        """Stream session events, resuming transparently across drops.

        SSE has no native replay, but the orchestrator log does (``after=seq``).
        On a connection drop, an overflow resync, or a clean server close without
        ``session_end``, this reconnects from the last seq seen and dedupes — so a
        proxy idle-timeout mid-turn no longer silently truncates the turn. Returns
        on ``session_end``; raises a typed error on auth/not-found or after the
        reconnect budget is exhausted.
        """
        last = after
        failures = 0
        path = f"/v1/sessions/{session_id}/events"
        while True:
            progressed = False  # did THIS connection deliver a new event?
            try:
                # no read timeout on the stream itself (long-lived); resync handles stalls
                async with self._h.c.stream("GET", path, params={"after": last},
                                            timeout=httpx.Timeout(self._h.c.timeout.connect, read=None)) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        ev = json.loads(line[6:])
                        if ev.get("type") == "_overflow":
                            break  # fell behind → reconnect with after=last and replay
                        seq = ev.get("seq")
                        if isinstance(seq, int):
                            if seq <= last:
                                continue  # dedupe across the reconnect boundary
                            last = seq
                        progressed = True
                        failures = 0
                        yield ev
                        if ev.get("type") == "session_end":
                            return
                # Server closed without session_end. If it replayed nothing new, treat
                # it like a transient drop and count it toward the budget — otherwise a
                # server that keeps cleanly closing an ended-but-unterminated stream
                # would spin us in a tight ~0.25s reconnect loop forever.
                if not progressed:
                    failures += 1
                    if failures > _MAX_RETRIES:
                        raise TransportError(
                            f"event stream for {session_id} closed {failures} times "
                            "without new events or session_end"
                        )
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403, 404):
                    raise from_status(e) from e  # terminal: don't reconnect
                failures += 1
                if failures > _MAX_RETRIES:
                    raise from_status(e) from e
            except httpx.TransportError as e:
                failures += 1
                if failures > _MAX_RETRIES:
                    raise TransportError(str(e)) from e
            await asyncio.sleep(_backoff(failures))


class SchedulesResource:
    def __init__(self, http: _Http) -> None:
        self._h = http

    async def create(self, *, name: str, agent_id: str, prompt: str, cron: str,
                     enabled: bool = True, max_budget_usd: float | None = None) -> dict[str, Any]:
        return await self._h.json("POST", "/v1/schedules", json={
            "name": name, "agent_id": agent_id, "prompt": prompt, "cron": cron,
            "enabled": enabled, "max_budget_usd": max_budget_usd,
        })

    async def list(self) -> list[dict[str, Any]]:
        return (await self._h.json("GET", "/v1/schedules"))["schedules"]

    async def update(self, schedule_id: str, **fields: Any) -> dict[str, Any]:
        return await self._h.json("PATCH", f"/v1/schedules/{schedule_id}", json=fields)

    async def delete(self, schedule_id: str) -> None:
        await self._h.json("DELETE", f"/v1/schedules/{schedule_id}")

    async def run(self, schedule_id: str) -> dict[str, Any]:
        return await self._h.json("POST", f"/v1/schedules/{schedule_id}/run")


class TokensResource:
    def __init__(self, http: _Http) -> None:
        self._h = http

    async def create(self, name: str, scopes: list[str] | tuple[str, ...] = ("run",)) -> dict[str, Any]:
        return await self._h.json("POST", "/v1/tokens", json={"name": name, "scopes": list(scopes)})

    async def list(self) -> list[dict[str, Any]]:
        return (await self._h.json("GET", "/v1/tokens"))["tokens"]

    async def delete(self, token_id: str) -> None:
        await self._h.json("DELETE", f"/v1/tokens/{token_id}")


class EgressProfilesResource:
    """Named firewall-rule bundles. Applied to an agent by attaching an ENVIRONMENT that
    references the profile (``client.environments``) — there is no direct per-agent pin.

    A profile is a list of ``rules`` — each ``{"action", "dest", "ports", "enabled", "note"}``
    where action is ``allow`` / ``deny`` / ``inspect``, dest is a domain, IP, or CIDR, and
    ``ports`` (allow/inspect) lifts Warden's default 80/443 wall for that destination — plus
    optional ``hosts`` overrides (``{"host", "ip"}``) that resolve an internal name to a fixed
    address, bypassing DNS (for a name only your internal DNS knows)."""

    def __init__(self, http: _Http) -> None:
        self._h = http

    async def list(self) -> list[dict[str, Any]]:
        return (await self._h.json("GET", "/v1/egress/profiles"))["profiles"]

    async def presets(self) -> list[dict[str, Any]]:
        """The built-in egress presets (developer / python / node / data-science /
        anthropic-only / web-audit). Each carries a ``key`` usable with :meth:`create`."""
        return (await self._h.json("GET", "/v1/egress/presets"))["presets"]

    async def create(self, *, name: str | None = None, preset: str | None = None, mode: str = "enforce",
                     rules: list[dict[str, Any]] | None = None,
                     hosts: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """Create a profile. Pass ``preset`` (e.g. "developer") to instantiate a built-in
        bundle, optionally with a custom ``name``; otherwise pass ``name`` + ``rules`` (and
        optional ``hosts`` overrides). See the class docstring for the rule/host shapes, e.g.::

            rules=[{"action": "allow", "dest": "git.internal", "ports": [443]}],
            hosts=[{"host": "git.internal", "ip": "10.1.20.50"}]
        """
        if preset:
            return await self._h.json("POST", "/v1/egress/profiles", json={"preset": preset, "name": name})
        if not name:
            raise ValueError("create() needs either a preset or a name")
        return await self._h.json("POST", "/v1/egress/profiles", json={
            "name": name, "mode": mode, "rules": rules or [], "hosts": hosts or []})

    async def update(self, profile_id: str, **fields: Any) -> dict[str, Any]:
        """Patch ``name`` / ``mode`` / ``rules`` / ``hosts`` (unset fields are left unchanged)."""
        return await self._h.json("PATCH", f"/v1/egress/profiles/{profile_id}", json=fields)

    async def delete(self, profile_id: str) -> None:
        await self._h.json("DELETE", f"/v1/egress/profiles/{profile_id}")


class SecretsResource:
    """Operator injection secrets — Warden injects the templated value into a header on
    every request to a scoped host, so the value lives only in the vault + Warden, never in
    the sandbox. Group them into environments (see :class:`EnvironmentsResource`) to scope
    which agents receive which secrets. Admin scope required."""

    def __init__(self, http: _Http) -> None:
        self._h = http

    async def list(self) -> list[dict[str, Any]]:
        """Metadata only — values are never returned (they leave only via Warden)."""
        return (await self._h.json("GET", "/v1/secrets"))["secrets"]

    async def put(self, name: str, *, scopes: list[str], value: str | None = None,
                  header: str = "Authorization", template: str = "Bearer {value}",
                  enabled: bool = True) -> dict[str, Any]:
        """Create or edit by name. ``value`` is required to create, optional to edit (keeps
        the stored one). ``scopes`` are the hosts the secret is injected on; ``template``
        must contain ``{value}``."""
        return await self._h.json("POST", "/v1/secrets", json={
            "name": name, "scopes": scopes, "value": value, "header": header,
            "template": template, "enabled": enabled})

    async def delete(self, name: str) -> None:
        await self._h.json("DELETE", f"/v1/secrets/{name}")


class EnvironmentsResource:
    """Named bundles of {secrets, egress profile} an agent attaches to via harness
    ``environments`` for least-privilege scoping. An agent with no environments receives no
    operator secrets; attached environments grant the union of their named secrets.
    Admin scope required."""

    def __init__(self, http: _Http) -> None:
        self._h = http

    async def list(self) -> list[dict[str, Any]]:
        return (await self._h.json("GET", "/v1/environments"))["environments"]

    async def create(self, *, name: str, secrets: list[str] | None = None,
                     egress_profile: str | None = None, description: str = "") -> dict[str, Any]:
        return await self._h.json("POST", "/v1/environments", json={
            "name": name, "description": description,
            "secrets": secrets or [], "egress_profile": egress_profile})

    async def update(self, environment_id: str, **fields: Any) -> dict[str, Any]:
        return await self._h.json("PATCH", f"/v1/environments/{environment_id}", json=fields)

    async def delete(self, environment_id: str) -> None:
        await self._h.json("DELETE", f"/v1/environments/{environment_id}")


class Session:
    """An async handle to one session — an ``async with`` context manager (connects on enter).
    By default the session is *ephemeral* and deleted on exit; pass ``ephemeral=False`` (or use
    :meth:`TerrariumClient.attach`) to leave it running server-side for a later reattach — the
    durable, long-running pattern. The session is created lazily on ``connect`` from the stored
    args (or bound to an existing id via :meth:`TerrariumClient.attach`)."""

    def __init__(self, client: "TerrariumClient", *, create_kw: dict[str, Any] | None = None,
                 session_id: str | None = None, agent_id: str | None = None, tools=None,
                 ephemeral: bool = True, resume: bool = False) -> None:
        self._client = client
        self._create_kw = create_kw or {}
        self.id = session_id
        self.agent_id = agent_id
        self._ephemeral = ephemeral  # delete on __aexit__? False = persist for client.attach(id)
        self._resume = resume        # attach(): seed the cursor instead of replaying history
        self._last_seq = -1
        # client-bridged tools (name -> ClientTool): their handlers run HERE when the agent
        # calls them (see _iter_turn). The schemas already travelled to the worker via create_kw.
        self._tools = {t.name: t for t in (tools or [])}

    async def connect(self) -> "Session":
        """Create the session (if not bound to one already) and drain until the worker is
        ready. Called automatically by ``async with``.

        Raises :class:`TerrariumError` if the session ends before it becomes ready (e.g. an
        invalid harness) instead of returning a dead session — a later ``receive_response``
        would otherwise send into a worker that is already gone."""
        if self.id is None:
            created = await self._client.sessions.create(**self._create_kw)
            self.id = created["id"]
            self.agent_id = created.get("agent_id")
        elif self._resume and self._last_seq < 0:
            # Reattach: seed the cursor from the orchestrator's durable resume point instead of
            # streaming from seq 0. Draining from the start would stop at the session's ORIGINAL
            # `ready` and leave every later event to be replayed by the next turn — re-running
            # completed client-tool handlers (real side effects, in this process) and posting
            # stale results back. The cursor is registry/log-derived, so this survives an
            # orchestrator restart.
            summary = await self._client.sessions.get(self.id)
            if summary.get("status") == "terminated":
                raise TerrariumError(
                    f"session {self.id} is terminated — attach cannot drive new turns. "
                    "Use sessions.events()/stream() to read its log, or create a new session.")
            self.agent_id = self.agent_id or summary.get("agent_id")
            cursor = summary.get("resume_cursor")
            # Clamp: a cursor past the tail can only mean the log was truncated behind us, and
            # a missing key means an older orchestrator. Both fall back to -1 (full replay) —
            # the safe direction, since over-seeking would silently swallow live events.
            if isinstance(cursor, int) and cursor >= 0:
                self._last_seq = cursor
                return self  # already past `ready`; nothing to drain
            # cursor -1/absent → never reached a turn boundary (still starting): drain normally.
        last_error: str | None = None
        async for ev in self._client.sessions.stream(self.id, after=self._last_seq):
            if isinstance(ev.get("seq"), int):  # transient events (e.g. assistant_delta) carry no seq
                self._last_seq = ev["seq"]
            etype = ev.get("type")
            if etype == "ready":
                return self
            if etype == "error":
                # An error during startup MAY be non-fatal (e.g. "client tools disabled" still
                # reaches ready), so don't fail yet — remember it and keep draining. If the session
                # actually ends before ready we surface this; if ready follows, it was benign.
                last_error = ev.get("message") or ev.get("error") or last_error
            elif etype == "session_end":
                reason = last_error or ev.get("reason") or "session ended before it became ready"
                raise TerrariumError(f"session {self.id} failed to start: {reason}")
        # Stream closed without ready or session_end (orchestrator dropped the SSE early).
        raise TerrariumError(
            f"session {self.id} failed to start: {last_error or 'stream ended before ready'}")

    async def _iter_turn(
        self,
        *,
        can_use_tool: "CanUseTool | None" = None,
        on_question: "Callable[[dict[str, Any]], dict[str, Any] | None] | None" = None,
        on_permission: "Callable[[dict[str, Any]], str | None] | None" = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield this turn's raw events until it completes, auto-handling human-in-the-loop
        prompts via a Claude-SDK-style ``can_use_tool`` (preferred) or the older
        ``on_question`` / ``on_permission`` callbacks. ``can_use_tool`` may be sync or async."""
        started = False  # has THIS turn begun? A reused session's previous turn emits a trailing
        #                  'status: idle' just AFTER its result (higher seq), which can lead this
        #                  turn's after= window — _turn_ended would then end turn 2 before it starts.
        async for ev in self._client.sessions.stream(self.id, after=self._last_seq):
            t = ev["type"]
            # Transport control sentinels (_heartbeat keepalive, _overflow) carry no turn
            # semantics — drop them so raw ask() consumers don't see them (receive_response
            # already ignores them via parse_message → None).
            if t.startswith("_"):
                continue
            if not started and t == "status" and ev.get("status") == "idle":
                if isinstance(ev.get("seq"), int):  # stale leading idle: advance the cursor and skip
                    self._last_seq = ev["seq"]
                continue
            if t in ("user", "assistant_text", "assistant_delta", "thinking", "tool_use") or \
                    (t == "status" and ev.get("status") in ("running", "requesting")):
                started = True
            if isinstance(ev.get("seq"), int):  # transient events (e.g. assistant_delta) carry no seq
                self._last_seq = ev["seq"]
            yield ev
            if t == "question":
                answers = None
                if can_use_tool is not None:
                    ctx = ToolPermissionContext(title=ev.get("title"), description=ev.get("description"), raw=ev)
                    res = await _resolve(can_use_tool("AskUserQuestion", {"questions": ev.get("questions") or []}, ctx))
                    if isinstance(res, PermissionResultAllow) and res.updated_input:
                        answers = res.updated_input.get("answers")
                elif on_question is not None:
                    answers = on_question(ev)
                if answers:
                    await self._client.sessions.answer(self.id, ev.get("question_id", ""), answers)
            elif t == "permission":
                decision = None
                if can_use_tool is not None:
                    ctx = ToolPermissionContext(request_id=str(ev.get("request_id", "")), title=ev.get("title"), description=ev.get("description"), raw=ev)
                    res = await _resolve(can_use_tool(str(ev.get("tool_name", "")), ev.get("input") or {}, ctx))
                    if isinstance(res, PermissionResultAllow):
                        decision = "always" if res.always else "allow"
                    elif isinstance(res, PermissionResultDeny):
                        decision = "deny"
                elif on_permission is not None:
                    decision = on_permission(ev)
                if decision:
                    await self._client.sessions.decide(self.id, ev.get("request_id", ""), decision)
            elif t == "client_tool_call":
                # The agent invoked a tool whose handler lives HERE. Run it in this process
                # (with the dev's app context), then hand the result back to the sandbox.
                ctool = self._tools.get(str(ev.get("name", "")))
                content, is_error = "", False
                if ctool is None:
                    content, is_error = f"No client tool named {ev.get('name')!r} is registered.", True
                else:
                    try:
                        res = await _resolve(ctool.handler(ev.get("input") or {}))
                        if isinstance(res, dict):
                            raw, is_error = res.get("content", ""), bool(res.get("is_error"))
                            # keep a list of Anthropic content blocks (text/image) intact so a tool
                            # can return a screenshot; a bare value collapses to a string.
                            content = raw if isinstance(raw, list) else str(raw)
                        elif isinstance(res, list):
                            content = res
                        else:
                            content = "" if res is None else str(res)
                    except Exception as exc:  # noqa: BLE001 — surface the dev's error to the agent, don't wedge
                        content, is_error = f"client tool error: {exc}", True
                await self._client.sessions.client_tool_result(self.id, str(ev.get("call_id", "")), content, is_error)
            if _turn_ended(ev):
                return

    async def ask(
        self,
        text: "str | list[dict[str, Any]]",
        on_question: "Callable[[dict[str, Any]], dict[str, Any] | None] | None" = None,
        on_permission: "Callable[[dict[str, Any]], str | None] | None" = None,
        *,
        can_use_tool: "CanUseTool | None" = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a message and yield this turn's raw events until it completes. ``text`` may be a
        string OR a list of Anthropic content blocks (text + image) for a vision turn. Prefer
        ``can_use_tool`` (Claude-SDK-style) for questions/permissions; ``on_question`` /
        ``on_permission`` remain for backward compatibility."""
        await self._client.sessions.send(self.id, text)
        async for ev in self._iter_turn(can_use_tool=can_use_tool, on_question=on_question, on_permission=on_permission):
            yield ev

    async def query(self, text: "str | list[dict[str, Any]]") -> None:
        """Send a message (no wait). ``text`` may be a string or a list of Anthropic content
        blocks (incl. image). Mirrors ``ClaudeSDKClient.query``; iterate ``receive_response()``."""
        await self._client.sessions.send(self.id, text)

    async def receive_response(
        self,
        prompt: "str | list[dict[str, Any]] | None" = None,
        *,
        can_use_tool: "CanUseTool | None" = None,
    ) -> AsyncIterator[Message]:
        """Yield this turn's typed messages (AssistantMessage / ToolUse / ResultMessage / …),
        the Claude-Agent-SDK shape. Optionally send ``prompt`` first. Pass ``can_use_tool``
        to auto-answer AskUserQuestion / permission prompts.

        A turn's consecutive assistant blocks (text, thinking, tool_use) are **coalesced into
        one multi-block ``AssistantMessage``** — matching the Claude Agent SDK — and flushed
        before any tool_result/result/system message and at turn end. (The coalesced message's
        ``raw`` is empty; iterate :meth:`ask` if you need the underlying per-event stream.)"""
        if prompt is not None:
            await self.query(prompt)
        pending: list[Any] = []  # assistant blocks accumulating into the current message
        pending_model: str | None = None  # the responding model, carried onto the coalesced message
        async for ev in self._iter_turn(can_use_tool=can_use_tool):
            msg = parse_message(ev)
            if msg is None:
                continue
            if isinstance(msg, AssistantMessage):
                pending.extend(msg.content)
                pending_model = pending_model or msg.model
                continue
            if pending:  # an assistant message closes when a non-assistant message arrives
                yield AssistantMessage(content=pending, model=pending_model)
                pending, pending_model = [], None
            yield msg
        if pending:
            yield AssistantMessage(content=pending, model=pending_model)

    async def run(
        self,
        text: str,
        on_question: "Callable[[dict[str, Any]], dict[str, Any] | None] | None" = None,
        on_permission: "Callable[[dict[str, Any]], str | None] | None" = None,
        *,
        can_use_tool: "CanUseTool | None" = None,
    ) -> dict[str, Any]:
        """Send a message and return the collected reply (text + cost + events). Pass
        ``can_use_tool`` (or ``on_question`` / ``on_permission``) to auto-handle prompts."""
        events = [e async for e in self.ask(text, on_question=on_question, on_permission=on_permission, can_use_tool=can_use_tool)]
        texts = [e["text"] for e in events if e["type"] == "assistant_text"]
        result = next((e for e in reversed(events) if e["type"] == "result"), {})
        return {
            "text": "\n".join(texts),
            "cost_usd": result.get("total_cost_usd"),
            "events": events,
        }

    async def interrupt(self) -> None:
        await self._client.sessions.interrupt(self.id)

    async def answer(self, question_id: str, answers: dict[str, Any]) -> None:
        """Answer a pending AskUserQuestion seen in the stream."""
        await self._client.sessions.answer(self.id, question_id, answers)

    async def decide(self, request_id: str, decision: Decision) -> None:
        """Approve/deny a pending tool-permission request ("allow" | "always" | "deny")."""
        await self._client.sessions.decide(self.id, request_id, decision)

    async def set_model(self, model: str) -> None:
        """Switch this running session's model live (continues the conversation)."""
        await self._client.sessions.set_model(self.id, model)

    async def set_permission_mode(self, mode: PermissionMode) -> None:
        """Switch this running session's permission mode live (default | acceptEdits | plan |
        bypassPermissions). Mirrors ``ClaudeSDKClient.set_permission_mode``."""
        await self._client.sessions.set_permission_mode(self.id, mode)

    async def rewind(self, message_id: str, mode: RewindMode = "files") -> None:
        """Rewind this session to the turn anchored by ``message_id`` (files | conversation | both)."""
        await self._client.sessions.rewind(self.id, message_id, mode)

    async def upload_file(self, path: str, dest: str | None = None) -> dict[str, Any]:
        """Upload a local file into this session's /workspace. Returns {name, size}."""
        return await self._client.sessions.upload_file(self.id, path, dest)

    async def verify_egress(self) -> dict[str, Any]:
        """Recompute this session's tamper-evident egress-audit hash chain."""
        return await self._client.sessions.verify_egress(self.id)

    async def summary(self) -> dict[str, Any]:
        return await self._client.sessions.get(self.id)

    async def close(self) -> None:
        if self.id is None:
            return
        try:
            await self._client.sessions.delete(self.id)
        except NotFoundError:
            pass  # already gone — fine; any other failure (auth/server) surfaces

    async def __aenter__(self) -> "Session":
        return await self.connect()

    async def __aexit__(self, *exc: Any) -> None:
        if self._ephemeral:
            await self.close()
        # else: leave it running server-side — reattach later with client.attach(self.id)


class TerrariumClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8900",
        token: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._http = _Http(base_url, token, timeout)
        self.agents = AgentsResource(self._http)
        self.sessions = SessionsResource(self._http)
        self.schedules = SchedulesResource(self._http)
        self.tokens = TokensResource(self._http)
        self.egress_profiles = EgressProfilesResource(self._http)
        self.secrets = SecretsResource(self._http)
        self.environments = EnvironmentsResource(self._http)

    async def health(self) -> dict[str, Any]:
        return await self._http.json("GET", "/healthz")

    async def fleet(self) -> dict[str, Any]:
        return await self._http.json("GET", "/v1/fleet")

    async def templates(self) -> list[dict[str, Any]]:
        return (await self._http.json("GET", "/v1/templates"))["templates"]

    def session(self, *, options: "TerrariumOptions | None" = None, agent_id: str | None = None,
                title: str | None = None, memory_scope: str | None = None,
                ephemeral: bool = True, **harness: Any) -> Session:
        """Build a session handle (no I/O yet). ``async with`` it to create + connect.
        Pass a ``TerrariumOptions`` and/or loose harness kwargs; explicit ``agent_id`` /
        ``title`` / ``memory_scope`` override the ones on ``options``. ``ephemeral=False``
        keeps the session alive on exit so you can ``client.attach(id)`` it later."""
        if options is not None:
            harness = {**options.to_harness(), **harness}
            agent_id = agent_id or options.agent_id
            title = title or options.title
            memory_scope = memory_scope or options.memory_scope
        create_kw = {"agent_id": agent_id, "title": title, "memory_scope": memory_scope, **harness}
        return Session(self, create_kw=create_kw, tools=(options.tools if options else None),
                       ephemeral=ephemeral)

    def attach(self, session_id: str, *, tools=None, replay: bool = False) -> Session:
        """Build a handle to an EXISTING session id (``async with`` to drain to ready). Never
        deletes on exit (it's not ours to discard). Pass ``tools`` to re-register client-tool
        handlers for a session whose schemas were sent at creation.

        Resumes by default: the handle seeds its stream cursor from the session's durable
        resume point, so the next turn yields only NEW events and already-completed
        client-tool calls are not re-executed. This is what you want for reconnecting to a
        live session after your own process restarts.

        ``replay=True`` restores the older full-replay behavior — stream from the beginning
        of the log — for consumers that want to rebuild state from the whole history. Note
        that a replayed ``client_tool_call`` WILL re-run its handler, so only use it with
        idempotent tools (or none registered).
        """
        return Session(self, session_id=session_id, tools=tools, ephemeral=False,
                       resume=not replay)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "TerrariumClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


# Harness fields accepted by the create/session **kwargs helpers — one source of truth,
# shared with options.TerrariumOptions (which mirrors terrarium/harness.py).
# client_tools is a harness KEY (emitted by TerrariumOptions.to_harness from `tools=`) but
# has no TerrariumOptions attribute of that name, so it isn't in _HARNESS_FIELDS — add it here.
_HARNESS_KEYS = frozenset(_HARNESS_FIELDS) | {"client_tools"}


def _harness_body(harness: dict[str, Any]) -> dict[str, Any]:
    """Validate **harness kwargs against the known fields, raising on a typo rather than
    silently dropping it — a typo like ``mdoel=`` must fail, not quietly no-op."""
    unknown = set(harness) - _HARNESS_KEYS
    if unknown:
        raise TypeError(
            f"unknown harness option(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(sorted(_HARNESS_KEYS))}"
        )
    return dict(harness)


async def query(
    *,
    prompt: str,
    options: "TerrariumOptions | None" = None,
    base_url: str = "http://127.0.0.1:8900",
    token: str | None = None,
    client: "TerrariumClient | None" = None,
    keep_session: bool = False,
) -> AsyncIterator[Message]:
    """One-shot helper mirroring ``claude_agent_sdk.query``: open an ephemeral Terrarium
    session from ``options``, send ``prompt``, and yield the turn's typed messages.

        from terrarium import query, TerrariumOptions

        async for msg in query(prompt="Summarise the repo", options=TerrariumOptions(model="opus")):
            print(msg)

    The session is created fresh (or attached to ``options.agent_id``) and deleted on exit
    unless ``keep_session=True``. Pass an existing ``client`` to reuse a connection/token.
    """
    opts = options or TerrariumOptions()
    own_client = client is None
    c = client or TerrariumClient(base_url=base_url, token=token)
    sess = c.session(options=opts)
    try:
        await sess.connect()
        async for msg in sess.receive_response(prompt, can_use_tool=opts.can_use_tool):
            yield msg
    finally:
        if not keep_session:
            await sess.close()
        if own_client:
            await c.aclose()
