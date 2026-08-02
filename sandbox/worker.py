#!/usr/bin/env python3
"""In-container agent worker.

Runs the Claude Agent SDK (`ClaudeSDKClient`) inside the sandbox and speaks the
stdio JSON-lines protocol with the host orchestrator:

  • reads commands (one JSON object per line) from stdin
  • emits events (one JSON object per line) to stdout
  • logs to stderr

The host stamps the authoritative seq/ts when it persists each event, so the
worker emits bare ``{"type": ..., ...}`` payloads.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import time
import sys
from pathlib import Path
from typing import Any

# Concealment: re-exec once to scrub the orchestrator/Warden tells from this
# worker's own /proc/<pid>/environ before importing/spawning anything the agent
# could touch (a same-uid agent can read the parent environ; os.environ.pop is
# insufficient — see terrarium/conceal.py). Runs BEFORE the heavy SDK import so
# the throwaway first process stays cheap.
from terracore.conceal import conceal_env, prepare_ca_bundle, write_decoy_creds

conceal_env()

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

try:  # partial-message streaming (token-by-token); absent on older SDKs
    from claude_agent_sdk import StreamEvent
except ImportError:  # pragma: no cover
    StreamEvent = None

# terrarium is on PYTHONPATH (=/opt/runtime) in the image
from terracore import protocol as P
from terracore.harness import Harness
from terracore.personas import build_system_prompt
from terracore.toolset import DEFAULT_BUILTINS

# Env the CLI + its toolchain (node, git, curl, python tools) legitimately need. Anything
# outside this allowlist — host secrets, orchestrator/Warden tell-tales, path fingerprints
# like PYTHONPATH — is NOT handed to the agent. Extend per-agent via the harness `env`.
_CLI_ENV_ALLOW = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "TERM", "TZ", "TMPDIR",
    "HOSTNAME", "LANG", "LANGUAGE",
    # egress proxy (functional — the agent must route through Warden)
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy",
    "NODE_USE_ENV_PROXY",
    # trust store (functional — validate the proxy CA + real roots)
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "GIT_SSL_CAINFO",
    # the inert DECOY api key (subscription mode uses a ~/.claude stub instead)
    "ANTHROPIC_API_KEY",
})
# Prefixes that are safe to pass through (locale, CLI config, XDG dirs).
_CLI_ENV_ALLOW_PREFIXES = ("LC_", "CLAUDE_", "XDG_")

# `extra_options` exists as a forward-compatible escape hatch for new SDK fields,
# not as a second way to replace Terrarium's security/lifecycle decisions.
_PROTECTED_OPTION_KEYS = frozenset({
    "model", "system_prompt", "mcp_servers", "allowed_tools", "permission_mode",
    "cwd", "can_use_tool", "hooks", "thinking", "effort", "max_turns",
    "max_budget_usd", "setting_sources", "env", "tools", "skills", "resume",
    "fork_session", "continue_conversation",
})


def _cli_allowlisted_env(environ: "os._Environ[str] | dict[str, str]") -> dict[str, str]:
    return {k: v for k, v in environ.items()
            if k in _CLI_ENV_ALLOW or k.startswith(_CLI_ENV_ALLOW_PREFIXES)}


def emit(type: str, **fields: Any) -> None:
    sys.stdout.write(json.dumps({"type": type, **fields}, default=str) + "\n")
    sys.stdout.flush()


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append("[image]")  # elide base64 — the real image went to the model, not the log
            else:
                parts.append(json.dumps(block, default=str))
        return "\n".join(parts)
    return json.dumps(content, default=str)


def _blocks_echo(blocks: Any) -> str:
    """A short text echo of a multimodal user turn for the transcript (the real content blocks —
    text + image — go to the model; the transcript just needs something readable)."""
    if not isinstance(blocks, list):
        return str(blocks)
    parts = []
    for b in blocks:
        if isinstance(b, dict):
            parts.append(str(b.get("text", "")) if b.get("type") == "text" else f"[{b.get('type', 'block')}]")
    return " ".join(p for p in parts if p) or "[image]"


# Live token-streaming state. Incremental assistant text is emitted as TRANSIENT
# assistant_delta events (live-only, never persisted); the final assistant_text stays
# canonical. `sid` increments per text block so the UI can keep blocks distinct.
_stream = {"sid": 0, "active": False}


def _handle_message(msg: Any, cost: dict[str, float]) -> None:
    if StreamEvent is not None and isinstance(msg, StreamEvent):
        # Stream ONLY the main agent's text — not a subagent's internal token stream
        # (which would scramble the main transcript).
        if msg.parent_tool_use_id is not None:
            return
        e = msg.event or {}
        et = e.get("type")
        if et == "content_block_start" and (e.get("content_block") or {}).get("type") == "text":
            _stream["sid"] += 1
            _stream["active"] = True
        elif et == "content_block_delta" and _stream["active"]:
            d = e.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                emit(P.EV_ASSISTANT_DELTA, stream_id=_stream["sid"], text=d["text"])
        elif et == "content_block_stop":
            _stream["active"] = False
        return
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                emit(P.EV_ASSISTANT_TEXT, text=block.text, model=msg.model)
            elif isinstance(block, ThinkingBlock):
                if (block.thinking or "").strip():  # omitted reasoning is empty — don't surface a blank step
                    emit(P.EV_THINKING, text=block.thinking)
            elif isinstance(block, ToolUseBlock):
                emit(P.EV_TOOL_USE, id=block.id, name=block.name, input=block.input)
    elif isinstance(msg, UserMessage):
        content = msg.content if isinstance(msg.content, list) else []
        is_tool_result = any(isinstance(b, ToolResultBlock) for b in content)
        uid = getattr(msg, "uuid", None)
        # A replayed user PROMPT (text, not tool results) carries the uuid that
        # anchors a rewind to this turn — surface it so the UI can offer "rewind here".
        if uid and not is_tool_result:
            emit(P.EV_REWIND_POINT, message_id=uid)
        for block in content:
            if isinstance(block, ToolResultBlock):
                emit(
                    P.EV_TOOL_RESULT,
                    tool_use_id=block.tool_use_id,
                    content=_stringify(block.content),
                    is_error=bool(block.is_error),
                )
    elif isinstance(msg, ResultMessage):
        # total_cost_usd is cumulative within a CLI session, but a rewind reconnects a
        # fresh client whose counter restarts at ~0. Add the banked baseline so the
        # emitted cost is the TRUE session total across reconnects (never resets).
        seg = float(msg.total_cost_usd or 0.0)
        cost["last"] = seg
        emit(
            P.EV_RESULT,
            subtype=msg.subtype,
            session_id=msg.session_id,
            num_turns=msg.num_turns,
            duration_ms=msg.duration_ms,
            duration_api_ms=getattr(msg, "duration_api_ms", None),
            is_error=msg.is_error,
            total_cost_usd=cost["baseline"] + seg,
            usage=msg.usage,
            # Why the turn ended: "completed" | "max_turns" | "aborted_streaming" |
            # "aborted_tools" | … (agent SDK >= 0.2.126). Without it, an interrupted turn and
            # a finished one are indistinguishable in the log — both just go idle — so an
            # operator reviewing a session couldn't tell "the agent stopped" from "I stopped
            # it". getattr keeps an older SDK from crashing the worker.
            terminal_reason=getattr(msg, "terminal_reason", None),
        )
    elif isinstance(msg, SystemMessage):
        # thinking_tokens is a transient progress estimate streamed many times per turn —
        # don't persist the chain (the result event carries the real token usage).
        if msg.subtype == "thinking_tokens":
            return
        emit(P.EV_SYSTEM, subtype=msg.subtype, data=msg.data)


async def _emit_context_usage(client: Any) -> None:
    """Surface context-window usage after a turn so a supervisor can decide when to
    checkpoint+compact. The CLI auto-compacts by default; this just makes the gauge visible.
    Guarded: an older CLI without the control request (or any probe error) is silently skipped."""
    try:
        u = await client.get_context_usage()
    except Exception:  # noqa: BLE001 — a usage probe must never break the turn
        return
    if not u:
        return
    emit(
        P.EV_CONTEXT_USAGE,
        percentage=u.get("percentage"),
        total_tokens=u.get("totalTokens"),
        max_tokens=u.get("maxTokens"),
        auto_compact=bool(u.get("isAutoCompactEnabled")),
        compact_threshold=u.get("autoCompactThreshold"),
    )


async def _wait_or_stop(evt: asyncio.Event, stop: asyncio.Event) -> None:
    """Wait until ``evt`` is set, returning early if ``stop`` fires — so a query that's
    serialized behind the previous turn still yields promptly on shutdown."""
    if evt.is_set() or stop.is_set():
        return
    waiters = [asyncio.ensure_future(evt.wait()), asyncio.ensure_future(stop.wait())]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()


def _load_harness() -> Harness:
    raw = os.environ.get("TERRA_HARNESS")
    return Harness.from_json(raw) if raw else Harness()


ASK_TOOL = "AskUserQuestion"  # the Claude Code built-in for structured operator questions
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}  # the "edits" approval scope


def _agent_definitions(specs: dict[str, Any]) -> dict[str, Any]:
    """Harness `agents` dicts → SDK ``AgentDefinition``s. Accepts snake_case or the SDK's
    camelCase keys; unknown keys are dropped (same tolerance as ``Harness.from_dict``).
    A spec missing the required description/prompt raises — surfaced to the host as an
    invalid-harness error before the session starts."""
    import dataclasses

    from claude_agent_sdk import AgentDefinition

    known = {f.name for f in dataclasses.fields(AgentDefinition)}

    def camel(k: str) -> str:
        head, *rest = k.split("_")
        return head + "".join(p.title() for p in rest)

    out: dict[str, Any] = {}
    for name, spec in specs.items():
        d = {camel(k): v for k, v in (spec or {}).items()}
        out[name] = AgentDefinition(**{k: v for k, v in d.items() if k in known})
    return out


def _build_options(h: Harness, resume_id: str | None = None, can_use_tool=None,
                   client_bridge=None, bridge_tools=None, pre_tool_hook=None) -> ClaudeAgentOptions:
    # in-process demo tools, plus any developer-declared MCP servers
    mcp: dict = {}
    if client_bridge is not None:  # SDK-provided custom tools, bridged to the dev's process
        mcp["client"] = client_bridge
    if h.mcp_servers:
        mcp.update(h.mcp_servers)

    allowed = h.allowed_tools if h.allowed_tools is not None else [*DEFAULT_BUILTINS]
    # Interactive sessions may ask the operator structured questions. AskUserQuestion is
    # NOT in the default tool set, so an agent can't ask unless the harness opts in — that
    # keeps unattended/scheduled agents from ever blocking on a human.
    if h.interactive and ASK_TOOL not in allowed:
        allowed = [*allowed, ASK_TOOL]
    # Make the bridged client tools usable even under a restrictive allowlist.
    for t in bridge_tools or []:
        if t not in allowed:
            allowed = [*allowed, t]

    setting_sources = h.setting_sources
    if h.skills and not setting_sources:
        setting_sources = ["project"]  # skills/ is mounted at /workspace/.claude/skills

    # The OS sandbox is the security boundary, so the agent runs unattended without
    # permission prompts. Default: bypassPermissions (no callback). Interactive: use a
    # callback-firing mode + a can_use_tool that auto-allows EVERY tool EXCEPT
    # AskUserQuestion (which it routes to the operator) — so normal tools stay un-gated.
    permission_mode = "default" if (h.interactive and can_use_tool) else h.permission_mode

    # `approval` gating CANNOT ride on can_use_tool alone: any bare tool name in
    # allowed_tools auto-approves that tool before the callback is consulted, so the
    # gate silently never fires (verified — Bash ran with the callback untouched, and
    # removing it from allowed_tools did NOT restore the gate either). A PreToolUse
    # hook runs ahead of that auto-approval; returning "ask" hands the call back to
    # can_use_tool, so all the operator-prompt logic below stays in one place.
    # AskUserQuestion is deliberately NOT routed through the hook — the callback does
    # fire for it (also verified), and it is an interaction primitive, not a gate.
    # Unattended sessions install neither, so `approval` stays a no-op there by design.

    kwargs: dict = {
        "model": h.model,
        "system_prompt": build_system_prompt(h.system_mode, h.custom_prompt),
        "mcp_servers": mcp,
        "allowed_tools": allowed,
        "permission_mode": permission_mode,
        "cwd": os.environ.get("TERRA_WORKSPACE", "/workspace"),
    }
    if h.interactive and can_use_tool:
        kwargs["can_use_tool"] = can_use_tool
    if h.interactive and pre_tool_hook is not None and h.approval != "off":
        # matcher=None → every tool; the hook itself decides which ones need the operator.
        kwargs["hooks"] = {"PreToolUse": [HookMatcher(hooks=[pre_tool_hook])]}
    if h.thinking:
        thinking = h.thinking
        if isinstance(thinking, dict) and "display" not in thinking:
            # return a readable summary of the reasoning instead of omitting it (which
            # surfaces as empty "thinking" steps in the UI)
            thinking = {**thinking, "display": "summarized"}
        kwargs["thinking"] = thinking
    if h.effort:
        kwargs["effort"] = h.effort
    if h.max_turns:
        kwargs["max_turns"] = h.max_turns
    if h.max_budget_usd:
        kwargs["max_budget_usd"] = h.max_budget_usd
    if setting_sources:
        kwargs["setting_sources"] = setting_sources
    # Deception + secret hygiene: the CLI (the agent) inherits an ALLOWLISTED env, not
    # "everything minus a few prefixes". A denylist leaks anything unforeseen — an
    # operator-injected AWS_*/GCP/GITHUB_TOKEN, or a path tell like PYTHONPATH=/opt/runtime
    # — straight to a compromised agent. Pass only what the CLI/toolchain actually needs:
    # the functional proxy + CA vars, the decoy key, locale/paths. Everything else (the
    # orchestrator/Warden tell-tales AND any stray host secret) is dropped. Extra env an
    # agent legitimately needs is declared explicitly via the harness `env`.
    cli_env = _cli_allowlisted_env(os.environ)
    # IMPORTANT: keep subprocess env-scrub OFF. The official CLAUDE_CODE_SUBPROCESS_ENV_SCRUB
    # feature requires `bubblewrap` to re-launch subprocesses, and the CLI FATALLY ERRORS at
    # startup if it's set without bwrap present. Terrarium's hardened image has no bwrap (the
    # CONTAINER + firewall + Warden are the isolation boundary, not bwrap — and bwrap can't get
    # the namespaces it needs under cap-drop-ALL + no-new-privileges anyway). Enabling it broke
    # all new sessions. Disable explicitly; an operator who installs bwrap can re-enable via env.
    cli_env.setdefault("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", "0")
    # Client-bridged tools must be ACTIVE, not lazy-loaded behind ToolSearch — otherwise the
    # agent has to discover-then-load them first (which weaker models / vaguer prompts won't
    # reliably do). ENABLE_TOOL_SEARCH=0 disables tool-search deferral so every tool (incl.
    # mcp__client__*) is directly callable. Scoped to client-tool sessions only (deferral still
    # keeps other sessions' requests lean); a dev can override via harness `env`.
    if h.client_tools:
        cli_env.setdefault("ENABLE_TOOL_SEARCH", "0")
    if h.env:
        cli_env.update(h.env)  # dev override wins
    kwargs["env"] = cli_env
    if h.extra_options:
        protected = sorted(set(h.extra_options) & _PROTECTED_OPTION_KEYS)
        if protected:
            raise ValueError(
                "extra_options cannot override Terrarium-managed option(s): "
                + ", ".join(protected)
            )
        kwargs.update(h.extra_options)

    # --- rewind support (guarded: tolerate an older Agent SDK that lacks these,
    # so a version skew can never crash an ordinary session) ---
    import dataclasses as _dc
    opt_fields = {f.name for f in _dc.fields(ClaudeAgentOptions)}
    # builtin_tools = the AVAILABILITY allowlist (the SDK's native `tools` field): the base set of
    # built-in tools the agent HAS. None = all defaults; a list / [] / {preset} restricts it. This
    # is the real "which tools can the agent use" knob (allowed_tools only auto-approves).
    if h.builtin_tools is not None and "tools" in opt_fields:
        kwargs["tools"] = h.builtin_tools
    # Skill trimming/selection (SDK `skills`): "all" | [names] | []. An explicit []
    # hides EVERY skill — including the CLI's built-ins (code-review, deep-research, …),
    # which otherwise load regardless. A bool keeps the legacy mount-and-discover path.
    if not isinstance(h.skills, bool) and "skills" in opt_fields:
        kwargs["skills"] = h.skills
        # skills=[] means BARE — but without an explicit setting_sources the SDK
        # defaults it to ["user","project"] (its skills-discovery convenience),
        # dragging filesystem settings back into a deliberately stripped agent.
        # Pin it empty (the CLI accepts --setting-sources= as "no sources");
        # an explicit harness setting_sources still wins.
        if h.skills == [] and h.setting_sources is None:
            kwargs["setting_sources"] = []
    # Programmatic subagents (SDK `agents`), sent to the CLI via the initialize request.
    if h.agents and "agents" in opt_fields:
        kwargs["agents"] = _agent_definitions(h.agents)
    # Claude-SDK-aligned extras, each guarded against an older bundled SDK that lacks the field.
    if h.fallback_model and "fallback_model" in opt_fields:
        kwargs["fallback_model"] = h.fallback_model
    if h.max_thinking_tokens and "max_thinking_tokens" in opt_fields:
        kwargs["max_thinking_tokens"] = h.max_thinking_tokens
    if h.betas and "betas" in opt_fields:
        kwargs["betas"] = h.betas
    # The SDK's stdout transport buffers a WHOLE JSON message; its 1 MiB default overflows on
    # image content (vision / computer-use) → "JSON message exceeded maximum buffer size". Bump it
    # (tunable via TERRA_MAX_BUFFER_SIZE). setdefault so a per-session extra_options value wins.
    if "max_buffer_size" in opt_fields:
        kwargs.setdefault("max_buffer_size", int(os.environ.get("TERRA_MAX_BUFFER_SIZE", str(32 << 20))))
    # Token-by-token assistant text streaming (live assistant_delta events).
    if StreamEvent is not None and "include_partial_messages" in opt_fields:
        kwargs["include_partial_messages"] = True
    if "enable_file_checkpointing" in opt_fields:
        # Checkpoint files per user turn, and tag each user message with a uuid we
        # surface (EV_REWIND_POINT) as the rewind anchor. Merge, don't clobber, extra_args.
        kwargs["enable_file_checkpointing"] = True
        extra_args = dict(kwargs.get("extra_args") or {})
        extra_args.setdefault("replay-user-messages", None)
        kwargs["extra_args"] = extra_args
    # Conversation rewind/branch: resume a (truncated) transcript the orchestrator
    # prepared. fork_session keeps the original session file intact.
    resume = resume_id or os.environ.get("TERRA_RESUME")
    if "resume" in opt_fields and resume:
        kwargs["resume"] = resume
        if "fork_session" in opt_fields and not resume_id:
            # only honor the one-shot env fork on the initial connect, not on rewinds
            kwargs["fork_session"] = os.environ.get("TERRA_RESUME_FORK") == "1"
    # Prefer the npm-installed Claude CLI on PATH over the SDK's older BUNDLED binary:
    # newer CLIs emit structured `workflow_progress` (per-phase subagent batches) that the
    # console's phased workflow view renders. Existence-checked + gated, so a missing or
    # SDK-only CLI just falls back to the bundled one — never crashes a session.
    # TERRA_CLI_PATH overrides (set it empty-then-unset to force the bundled binary).
    if "cli_path" in opt_fields:
        import shutil
        cli = os.environ.get("TERRA_CLI_PATH") or shutil.which("claude")
        if cli and os.path.exists(cli):
            kwargs["cli_path"] = cli
    return ClaudeAgentOptions(**kwargs)


def _session_file(sid: str) -> Path | None:
    """The Claude CLI's transcript for a session: ~/.claude/projects/<key>/<sid>.jsonl."""
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl"))
    return Path(hits[0]) if hits else None


def _is_turn(line: str) -> bool:
    """Does this transcript line carry an actual conversation turn?

    The CLI's JSONL also holds non-turn bookkeeping (summaries, file-history snapshots),
    which alone do NOT constitute a resumable conversation."""
    try:
        return json.loads(line).get("type") in ("user", "assistant")
    except Exception:  # noqa: BLE001 — non-JSON lines are never turns
        return False


def _truncate_transcript(sid: str, anchor_uuid: str) -> "tuple[bool, str | None]":
    """Drop the anchor user message + everything after it, so resuming `sid` continues
    from BEFORE that turn.

    Returns ``(ok, resume_sid)``:
      • ``(False, None)`` — the anchor is no longer in the transcript; nothing was changed.
      • ``(True, sid)``   — truncated, turns remain: resume `sid` in place.
      • ``(True, None)``  — the anchor WAS the first turn, so nothing is left to resume.
        The CLI rejects ``--resume`` on a transcript with no turns ("No conversation found
        with session ID: …"), which surfaced as a scary "resume failed" error even though
        rewinding past the first message legitimately means "start over". Reconnect fresh
        instead — that is the correct end state, reached without the error.
    """
    sf = _session_file(sid)
    if not sf:
        return (False, None)
    lines = sf.read_text().splitlines()
    keep: list[str] = []
    for ln in lines:
        try:
            if json.loads(ln).get("uuid") == anchor_uuid:
                break
        except Exception:  # noqa: BLE001 — keep non-JSON/summary lines verbatim
            pass
        keep.append(ln)
    if len(keep) == len(lines):
        return (False, None)  # anchor not found
    sf.write_text("\n".join(keep) + "\n" if keep else "")
    return (True, sid if any(_is_turn(ln) for ln in keep) else None)


MEMORY_SENTINEL = "/memory/.terra-memory-restored"


def _await_memory(timeout: float = 20.0) -> None:
    """Block until the orchestrator finishes restoring /memory, if it said it would.

    Gated on TERRA_MEMORY_RESTORE — set ONLY by the runner that actually performs a restore (k8s in
    memory_mode="synced", where the pod mounts no volume and the snapshot is unpacked after the pod
    is Running). Deliberately NOT gated on harness.memory_mode: the Docker runner keeps a real
    volume mount for "synced" (a local volume attaches in ~0ms, so there's nothing to buy), so it
    never restores and the worker must never wait for a sentinel that isn't coming.

    Fail-OPEN on timeout: an agent that starts with empty memory is recoverable; one wedged forever
    behind a sentinel that never arrives is not. (Restore is KB of notes over an exec pipe — if it
    hasn't finished in 20s it isn't going to.)"""
    if os.environ.get("TERRA_MEMORY_RESTORE") != "1":
        return
    deadline = time.monotonic() + timeout
    while not os.path.exists(MEMORY_SENTINEL):
        if time.monotonic() > deadline:
            emit(P.EV_ERROR, message="memory restore timed out — continuing with empty /memory")
            return
        time.sleep(0.05)


async def main() -> None:
    try:
        h = _load_harness()
    except Exception as exc:  # noqa: BLE001
        emit(P.EV_ERROR, message=f"invalid harness config: {exc}")
        emit(P.EV_STATUS, status="terminated")
        return

    _await_memory()

    # Write the subscription decoy (k8s warden mode) so the CLI emits an OAuth-shaped
    # request, then build the combined real-roots + proxy-CA trust store — both before
    # spawning the CLI so SSL_CERT_FILE/curl/git read a realistic store.
    write_decoy_creds()
    prepare_ca_bundle()

    cmd_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    stop = asyncio.Event()
    state: dict[str, Any] = {"sid": None}     # current SDK session id (learned from messages)
    cost: dict[str, float] = {"baseline": 0.0, "last": 0.0}  # banked cost across rewind reconnects
    client_ref: dict[str, Any] = {"c": None}  # the live client (swapped on a conversation rewind)

    # AskUserQuestion + tool-permission plumbing: the SDK invokes can_use_tool (a concurrent
    # control-request task) for AskUserQuestion AND for any tool that needs approval. We emit
    # an event to the host, then BLOCK that task on a Future until the operator's reply arrives
    # over stdin (resolved inline by the reader). Keyed by a request id in `pending`.
    pending: dict[str, asyncio.Future] = {}
    _ids = {"n": 0}
    always_allow: set[str] = set()  # tools the operator chose "always allow" (this session)

    def _next_id(prefix: str) -> str:
        _ids["n"] += 1
        return f"{prefix}{_ids['n']}"

    # Client-tool bridge: SDK-declared tools whose calls run in the DEVELOPER's process, not
    # here. The in-process MCP server below just emits a client_tool_call event and blocks on
    # `pending` until the SDK posts the result back (CMD_CLIENT_TOOL_RESULT) — same channel as
    # AskUserQuestion. The dev's app context never enters the sandbox.
    client_bridge, bridge_tools = (None, [])
    if h.client_tools:
        try:
            from terracore.tools import build_client_bridge
            client_bridge, bridge_tools = build_client_bridge(h.client_tools, emit=emit, pending=pending, next_id=_next_id)
        except Exception as exc:  # noqa: BLE001 — a bad tool spec must not crash the session
            emit(P.EV_ERROR, message=f"client tools disabled: {exc}")

    def _needs_approval(tool_name: str) -> bool:
        a = h.approval
        if isinstance(a, list):
            return tool_name in a
        if a == "all":
            return True
        if a == "edits":
            return tool_name in EDIT_TOOLS
        return False  # "off" / unknown

    async def pre_tool_use(input_data: dict[str, Any], tool_use_id, ctx):
        """Force gated tools to fall through to can_use_tool.

        allowed_tools would otherwise auto-approve them before the callback runs.
        "ask" is the only decision we ever return: "allow" here would skip the
        operator prompt, and denying outright would bypass the always-allow path.
        """
        name = input_data.get("tool_name") or ""
        if _needs_approval(name) and name not in always_allow:
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "Terrarium approval policy",
            }}
        return {}  # no opinion — normal permission handling applies

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], ctx):
        # 1) AskUserQuestion → ask the operator a structured question.
        if tool_name == ASK_TOOL:
            qid = _next_id("q")
            questions = tool_input.get("questions", [])
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            pending[qid] = fut
            emit(P.EV_QUESTION, question_id=qid, questions=questions)
            try:
                answers = await fut  # resolved by the reader on CMD_ANSWER
            except asyncio.CancelledError:
                return PermissionResultDeny(message="session ended before the question was answered", interrupt=True)
            finally:
                pending.pop(qid, None)
            emit(P.EV_ANSWERED, question_id=qid, answers=answers)  # echo for transcript/replay
            return PermissionResultAllow(updated_input={"questions": questions, "answers": answers})

        # 2) Tool-permission gating (interactive only — this callback isn't installed otherwise).
        if not _needs_approval(tool_name) or tool_name in always_allow:
            return PermissionResultAllow()  # auto-approve (sandbox is the boundary)
        rid = _next_id("p")
        fut = asyncio.get_running_loop().create_future()
        pending[rid] = fut
        emit(P.EV_PERMISSION, request_id=rid, tool_name=tool_name, input=tool_input,
             title=getattr(ctx, "title", None), description=getattr(ctx, "description", None))
        try:
            decision = await fut  # "allow" | "always" | "deny" — resolved by the reader on CMD_DECISION
        except asyncio.CancelledError:
            return PermissionResultDeny(message="session ended before the tool was approved", interrupt=True)
        finally:
            pending.pop(rid, None)
        emit(P.EV_DECIDED, request_id=rid, decision=decision, tool_name=tool_name)
        if decision == "deny":
            return PermissionResultDeny(message="The operator denied this tool use.")
        if decision == "always":
            always_allow.add(tool_name)
        return PermissionResultAllow()

    async def reader() -> None:
        # One persistent reader for the worker's whole life (recreating it would race
        # two readline threads across a reconnect). Interrupts + file rewind are applied
        # inline against the current client; conversation rewind + queries go via the queue.
        while not stop.is_set():
            line = await asyncio.to_thread(sys.stdin.readline)
            if line == "":  # EOF — host closed stdin
                await cmd_q.put(P.shutdown_cmd())
                return
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                emit(P.EV_ERROR, message=f"bad command: {exc}")
                continue
            kind = cmd.get("cmd")
            client = client_ref["c"]
            if kind == P.CMD_ANSWER:
                # Resolve the pending AskUserQuestion inline (the can_use_tool task is
                # blocked awaiting this Future; cmd_q is not being drained while it waits).
                fut = pending.get(cmd.get("question_id", ""))
                if fut and not fut.done():
                    fut.set_result(cmd.get("answers") or {})
            elif kind == P.CMD_DECISION:
                # Resolve a pending tool-permission request (allow / always / deny).
                fut = pending.get(cmd.get("request_id", ""))
                if fut and not fut.done():
                    fut.set_result(cmd.get("decision") or "allow")
            elif kind == P.CMD_CLIENT_TOOL_RESULT:
                # The SDK ran the dev's tool handler; hand its result to the blocked bridge tool.
                fut = pending.get(cmd.get("call_id", ""))
                if fut and not fut.done():
                    fut.set_result({"content": cmd.get("content", ""), "is_error": bool(cmd.get("is_error"))})
            elif kind == P.CMD_RECONFIG:
                # Live config change. Model switches via the SDK's set_model control request
                # (no reconnect); h.model is updated so a later resume keeps it. The next turn
                # re-sends context to the new model uncached (accepted input-cache penalty).
                model = cmd.get("model")
                if model and client:
                    try:
                        await client.set_model(model)
                        h.model = model
                        emit(P.EV_SYSTEM, subtype="config", data={"model": model})
                    except Exception as exc:  # noqa: BLE001
                        emit(P.EV_ERROR, message=f"set_model failed: {exc}")
                pmode = cmd.get("permission_mode")
                if pmode and client:
                    try:
                        await client.set_permission_mode(pmode)
                        h.permission_mode = pmode
                        emit(P.EV_SYSTEM, subtype="config", data={"permission_mode": pmode})
                    except Exception as exc:  # noqa: BLE001
                        emit(P.EV_ERROR, message=f"set_permission_mode failed: {exc}")
            elif kind == P.CMD_INTERRUPT and client:
                try:
                    await client.interrupt()
                except Exception as exc:  # noqa: BLE001
                    emit(P.EV_ERROR, message=f"interrupt failed: {exc}")
            elif kind == P.CMD_REWIND:
                mode = cmd.get("mode", "files")
                mid = cmd.get("message_id", "")
                if mode in ("files", "both") and client:  # restore the workspace inline
                    try:
                        await client.rewind_files(mid)
                        if mode == "files":
                            emit(P.EV_REWOUND, message_id=mid, mode="files")
                    except Exception as exc:  # noqa: BLE001
                        emit(P.EV_ERROR, message=f"rewind failed: {exc}")
                if mode in ("conversation", "both"):  # needs a transcript truncate + resume
                    await cmd_q.put({"cmd": "_rewind_conv", "message_id": mid, "mode": mode})
            else:
                await cmd_q.put(cmd)

    reader_task = asyncio.create_task(reader())
    started = False
    connect_failures = 0   # consecutive connect failures (reset on a good connect)
    rewind: dict[str, Any] = {"to": None, "mode": None}

    while not stop.is_set():
        try:
            options = _build_options(h, resume_id=state["sid"] if started else None, can_use_tool=can_use_tool,
                                     client_bridge=client_bridge, bridge_tools=bridge_tools,
                                     pre_tool_hook=pre_tool_use)
        except Exception as exc:  # noqa: BLE001
            emit(P.EV_ERROR, message=f"invalid harness config: {exc}")
            break

        rewind["to"] = None
        try:
            async with ClaudeSDKClient(options=options) as client:
                connect_failures = 0          # connected OK — clear the failure streak
                client_ref["c"] = client
                if not started:
                    # surface the resolved system prompt so the console can show it at session
                    # start. assistant/custom modes carry the actual persona text; claude_code is
                    # the CLI's built-in preset (its full text is owned by the CLI); minimal → none.
                    _sp = build_system_prompt(h.system_mode, h.custom_prompt)
                    sp_text = _sp if isinstance(_sp, str) else (
                        f"(built-in preset: {_sp.get('preset', h.system_mode)})" if isinstance(_sp, dict) else None)
                    emit(P.EV_READY, model=h.model, system_mode=h.system_mode, system_prompt=sp_text)
                    started = True
                    os.environ.pop("TERRA_RESUME", None)  # one-shot initial resume — consumed
                else:
                    emit(P.EV_STATUS, status="idle")  # reconnected after a conversation rewind

                # Drain the CLI stream CONTINUOUSLY — not just for the active turn. A turn's
                # `result` ends the TURN, but a Workflow/Agent launched `run_in_background`
                # keeps emitting (task_progress/notification) AFTER it; an undrained transport
                # also back-pressures the CLI and PARKS the background task. So one persistent
                # reader runs for the whole connection, and `idle_evt` marks the turn boundary
                # (set on each `result`) so new queries still serialize one turn at a time.
                idle_evt = asyncio.Event()
                idle_evt.set()

                # `noqa: B023` below — this closure captures the loop's `idle_evt`/`client`, which
                # would normally be the late-binding trap. It's safe because the task's lifetime is
                # strictly inside ONE iteration: the `finally` at the end of this block cancels and
                # awaits pump_task before the reconnect loop can rebind either name. Recorded so it
                # isn't "fixed" into a default-argument capture that would break the rewind path.
                async def pump() -> None:
                    try:
                        async for msg in client.receive_messages():  # noqa: B023
                            sid = getattr(msg, "session_id", None)
                            if sid:
                                state["sid"] = sid
                            _handle_message(msg, cost)
                            if isinstance(msg, ResultMessage):
                                emit(P.EV_STATUS, status="idle")  # turn done (background work may continue)
                                idle_evt.set()  # noqa: B023
                                await _emit_context_usage(client)  # noqa: B023
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — a stream error must not crash the worker
                        emit(P.EV_ERROR, message=f"stream failed: {exc}")
                    finally:
                        idle_evt.set()  # noqa: B023 — never leave a serialized query waiting on a dead stream

                pump_task = asyncio.create_task(pump())
                try:
                    while True:
                        cmd = await cmd_q.get()
                        kind = cmd.get("cmd")
                        if kind == P.CMD_SHUTDOWN:
                            stop.set()
                            break
                        if kind == "_rewind_conv":
                            rewind["to"] = cmd.get("message_id")
                            rewind["mode"] = cmd.get("mode")
                            break  # leave the client context, then truncate + reconnect
                        if kind == P.CMD_QUERY:
                            # Serialize: one turn at a time. Wait for the prior turn's `result`
                            # (idle_evt) — background tasks keep streaming through the pump
                            # meanwhile, so this doesn't stall them.
                            await _wait_or_stop(idle_evt, stop)
                            if stop.is_set():
                                break
                            idle_evt.clear()
                            blocks = cmd.get("content")  # list of Anthropic blocks (vision turn), or None
                            text = cmd.get("text", "")
                            emit(P.EV_USER, text=(_blocks_echo(blocks) if isinstance(blocks, list) else text))
                            emit(P.EV_STATUS, status="running")
                            try:
                                if isinstance(blocks, list):
                                    # an image/multimodal user turn: query() takes an async iterable
                                    # of stream-json message dicts (content may be a block list).
                                    async def _msg(c=blocks):
                                        yield {"type": "user", "message": {"role": "user", "content": c},
                                               "parent_tool_use_id": None}
                                    await client.query(_msg())
                                else:
                                    await client.query(text)
                            except Exception as exc:  # noqa: BLE001
                                emit(P.EV_ERROR, message=f"query failed: {exc}")
                                emit(P.EV_STATUS, status="idle")
                                idle_evt.set()
                finally:
                    pump_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pump_task
                client_ref["c"] = None
        except Exception as exc:  # noqa: BLE001 — a connect/resume failure must not crash the worker
            client_ref["c"] = None
            connect_failures += 1
            # A reconnect AFTER start can fail because the resumed/truncated transcript
            # is unusable — e.g. the original turn 401'd (bad credential) and never
            # persisted a conversation, so a later "Edit this message" rewind resumes a
            # session the CLI can't find. Per the documented guarantee, a failed rewind
            # must NOT crash the session: drop the bad resume and reconnect FRESH (the
            # old turn had no real content to keep). Cap retries so a persistent failure
            # (e.g. the new credential is also bad) still terminates instead of looping.
            if started and connect_failures <= 3:
                emit(P.EV_ERROR, message=f"resume failed — continuing in a fresh conversation: {exc}")
                state["sid"] = None
                rewind["to"] = None
                continue
            emit(P.EV_ERROR, message=f"session client error: {exc}")
            break

        # CLI subprocess is now closed — perform a pending conversation rewind, then reconnect.
        # A FAILED rewind is a no-op that keeps the session alive — never terminate on it.
        if rewind["to"]:
            # the resumed client restarts its cost counter — bank what we've spent so far
            cost["baseline"] += cost["last"]
            cost["last"] = 0.0
            try:
                ok, new_sid = _truncate_transcript(state["sid"], rewind["to"]) if state["sid"] else (False, None)
                if ok:
                    # new_sid None = rewound past the first turn: drop the id so the reconnect
                    # below starts a fresh conversation instead of resuming an empty transcript.
                    state["sid"] = new_sid
                    emit(P.EV_REWOUND, message_id=rewind["to"], mode=rewind["mode"])
                else:
                    emit(P.EV_ERROR, message="rewind: that point is no longer in the conversation")
            except Exception as exc:  # noqa: BLE001
                emit(P.EV_ERROR, message=f"conversation rewind failed: {exc}")
            # reconnect either way (truncated if it worked, else the current session)
        else:
            break  # shutdown

    stop.set()
    for fut in list(pending.values()):  # unblock any can_use_tool awaiting an answer
        if not fut.done():
            fut.cancel()
    reader_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reader_task
    emit(P.EV_STATUS, status="terminated")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
