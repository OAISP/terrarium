"""The stdio JSON-lines protocol shared by the orchestrator (host) and the
worker (container).

Host → worker: one JSON object per line on the worker's stdin.
Worker → host: one JSON event per line on the worker's stdout (stderr is logs).

Keeping this in the shared lib means both sides agree on the shapes without
duplicating string literals.
"""

from __future__ import annotations

import json
import os
from typing import Any

# ---- commands (host -> worker) ----
CMD_QUERY = "query"
CMD_INTERRUPT = "interrupt"
CMD_SHUTDOWN = "shutdown"
CMD_REWIND = "rewind"
CMD_ANSWER = "answer"
CMD_DECISION = "decision"
CMD_RECONFIG = "reconfig"
CMD_CLIENT_TOOL_RESULT = "client_tool_result"  # result of a client-bridged tool, from the SDK


def query_cmd(content: "str | list") -> dict[str, Any]:
    # a list of Anthropic content blocks (text + image) → "content" for a vision turn; else "text".
    if isinstance(content, list):
        return {"cmd": CMD_QUERY, "content": content}
    return {"cmd": CMD_QUERY, "text": content}


def answer_cmd(question_id: str, answers: dict[str, Any]) -> dict[str, Any]:
    """Answer a pending AskUserQuestion. ``answers`` maps each question's text to the
    chosen option label (or a list of labels for a multi-select question, or free text)."""
    return {"cmd": CMD_ANSWER, "question_id": question_id, "answers": answers}


def decision_cmd(request_id: str, decision: str) -> dict[str, Any]:
    """Operator's verdict on a pending tool-permission request.
    ``decision``: "allow" (once) | "always" (allow + remember for the session) | "deny"."""
    return {"cmd": CMD_DECISION, "request_id": request_id, "decision": decision}


def client_tool_result_cmd(call_id: str, content: "str | list", is_error: bool = False) -> dict[str, Any]:
    """The SDK client's result for a client-bridged tool call (the dev's handler ran in THEIR
    process; only this result crosses back into the sandbox). ``content`` is a string OR a list of
    Anthropic content blocks (so a tool can return an image)."""
    return {"cmd": CMD_CLIENT_TOOL_RESULT, "call_id": call_id, "content": content, "is_error": bool(is_error)}


def reconfig_cmd(model: str | None = None, permission_mode: str | None = None) -> dict[str, Any]:
    """Change a running session's config live. ``model`` switches the model mid-conversation
    (set_model; next turn re-reads context uncached → cache penalty). ``permission_mode``
    switches the CLI permission mode live (set_permission_mode)."""
    return {"cmd": CMD_RECONFIG, "model": model, "permission_mode": permission_mode}


def interrupt_cmd() -> dict[str, Any]:
    return {"cmd": CMD_INTERRUPT}


def shutdown_cmd() -> dict[str, Any]:
    return {"cmd": CMD_SHUTDOWN}


def rewind_cmd(message_id: str, mode: str = "files") -> dict[str, Any]:
    """Rewind to the user turn anchored by `message_id` (an SDK user-message uuid).
    mode: "files" (restore the workspace only) | "conversation" (truncate the
    transcript + resume) | "both"."""
    return {"cmd": CMD_REWIND, "message_id": message_id, "mode": mode}


# ---- events (worker -> host) ----
EV_READY = "ready"  # worker booted, ClaudeSDKClient connected
EV_USER = "user"
EV_THINKING = "thinking"
EV_ASSISTANT_TEXT = "assistant_text"
EV_ASSISTANT_DELTA = "assistant_delta"  # TRANSIENT: incremental assistant text (live-only,
#                                         never persisted; the final assistant_text is canonical)
EV_TOOL_USE = "tool_use"
EV_TOOL_RESULT = "tool_result"
EV_RESULT = "result"
EV_SYSTEM = "system"
EV_STATUS = "status"  # idle / running / etc.
EV_ERROR = "error"
EV_REWIND_POINT = "rewind_point"  # a user turn's SDK uuid — the anchor a rewind targets
EV_REWOUND = "rewound"            # a rewind completed (carries to message_id + mode)
EV_QUESTION = "question"          # agent asked the operator (AskUserQuestion): carries question_id + questions
EV_ANSWERED = "answered"          # the operator's answer was applied: carries question_id + answers
EV_PERMISSION = "permission"      # agent wants to use a gated tool: carries request_id + tool_name + input + title/description
EV_DECIDED = "decided"            # the operator's allow/always/deny verdict was applied: carries request_id + decision + tool_name
EV_CLIENT_TOOL_CALL = "client_tool_call"  # the agent invoked a CLIENT-bridged tool: carries call_id + name + input.
#                                           The SDK runs the dev's handler and replies via CMD_CLIENT_TOOL_RESULT.
EV_CONTEXT_USAGE = "context_usage"  # per-turn context-window telemetry: percentage / total_tokens /
#                                     max_tokens / auto_compact / compact_threshold. Lets a supervisor
#                                     decide when to checkpoint+compact (the CLI auto-compacts by default).
# ORCHESTRATOR-asserted (NOT a worker event — kept out of WORKER_EVENT_TYPES so the sandbox can't
# forge it): the worker's stream ended WITHOUT an intentional stop — the worker died unexpectedly
# (pod OOM/evict/crash). Carries reason + mid_turn; precedes the synthetic session_end.
EV_WORKER_LOST = "worker_lost"


# ---- protocol version + worker-event trust boundary ----
#
# The sandbox is untrusted by design — it runs arbitrary agent code, so its
# stdout is HOSTILE input. The orchestrator must never act on a worker-emitted
# event without sanitizing it first (forged control-plane events, runaway
# payload sizes, manipulated cost numbers). ``validate_worker_event`` is that
# single boundary; ``EventStore.record`` / the pump call it before trusting.
PROTOCOL_VERSION = 1

# Event types the worker is permitted to assert.
WORKER_EVENT_TYPES = frozenset({
    EV_READY, EV_USER, EV_THINKING, EV_ASSISTANT_TEXT, EV_ASSISTANT_DELTA, EV_TOOL_USE,
    EV_TOOL_RESULT, EV_RESULT, EV_SYSTEM, EV_STATUS, EV_ERROR,
    EV_REWIND_POINT, EV_REWOUND, EV_QUESTION, EV_ANSWERED, EV_PERMISSION, EV_DECIDED,
    EV_CLIENT_TOOL_CALL, EV_CONTEXT_USAGE,
})

# Transient events: broadcast LIVE to subscribers but never persisted or replayed (the
# durable log keeps only the canonical final event). Keeps token-streaming off the disk
# log + reconnect replay. A reconnect re-derives the final state from the persisted events.
TRANSIENT_EVENT_TYPES = frozenset({EV_ASSISTANT_DELTA})

# Types the ORCHESTRATOR stamps itself — a worker that emits one is forging
# control-plane state (ending sessions, faking a budget kill), so we reject it.
ORCH_ONLY_TYPES = frozenset({"session_start", "session_end", "budget_exceeded"})

# Hard cap on a single worker event's serialized size. One multi-MB stdout line
# would otherwise be read fully into RAM and persisted verbatim (DoS).
MAX_EVENT_BYTES = int(os.environ.get("TERRA_MAX_EVENT_BYTES", str(1 << 20)))  # 1 MiB


def _coerce_num(v: Any) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    return n if n == n and n not in (float("inf"), float("-inf")) else 0.0  # reject NaN/inf


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…[clipped]"


def _bounded(ev: dict[str, Any]) -> dict[str, Any]:
    """Return ``ev`` unchanged if it serializes within the size cap, else a small
    placeholder that preserves the type + a preview but drops the bulky payload."""
    try:
        blob = json.dumps(ev, ensure_ascii=False, default=str)
    except Exception:
        return {"type": EV_ERROR, "subtype": "unserializable_event"}
    if len(blob.encode("utf-8")) <= MAX_EVENT_BYTES:
        return ev
    t = ev.get("type")
    return {
        "type": t if t in WORKER_EVENT_TYPES else EV_SYSTEM,
        "subtype": "oversized_event",
        "bytes": len(blob.encode("utf-8")),
        "preview": _clip(blob, 4000),
    }


def validate_worker_event(ev: Any) -> dict[str, Any]:
    """Sanitize a worker-emitted event before the orchestrator trusts/persists it.

    Never raises. Guarantees the returned dict has a ``type`` drawn only from
    ``WORKER_EVENT_TYPES`` (anything else — orchestrator-only or unknown — is
    quarantined into a ``system`` event so it can't drive control-plane logic),
    coerces the cost/usage numbers the budget backstop reads, and bounds size.
    """
    if not isinstance(ev, dict):
        return {"type": EV_ERROR, "subtype": "malformed_event", "detail": _clip(str(ev), 2000)}
    t = ev.get("type")
    if t not in WORKER_EVENT_TYPES:
        # Quarantine: keep the raw claim for the audit trail, strip its agency. A
        # worker emitting an ORCHESTRATOR-only type (session_start/end, budget kill)
        # is forging control-plane state, not just sending junk — flag it distinctly
        # so the audit separates a deliberate forgery from a benign unknown type.
        return _bounded({
            "type": EV_SYSTEM,
            "subtype": "forged_control_event" if t in ORCH_ONLY_TYPES else "quarantined_event",
            "claimed_type": _clip(str(t), 200),
            "data": {k: v for k, v in ev.items() if k != "type"},
        })
    out = dict(ev)
    if t == EV_RESULT:
        if out.get("total_cost_usd") is not None:
            out["total_cost_usd"] = max(0.0, _coerce_num(out.get("total_cost_usd")))  # clamp ≥ 0
        if "usage" in out and not isinstance(out["usage"], dict):
            out["usage"] = {}
    return _bounded(out)
