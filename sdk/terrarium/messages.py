"""Typed messages + content blocks — modelled on the Claude Agent SDK's message types
(``AssistantMessage``, ``TextBlock``, ``ToolUseBlock``, ``ResultMessage`` …) so iterating a
Terrarium turn feels like iterating a Claude Agent SDK response.

Terrarium streams a flat event log; ``parse_message`` lifts each event into the closest
Claude-SDK message. Every message keeps the original event under ``.raw`` as an escape hatch.
Terrarium-only events (human-in-the-loop) become ``QuestionMessage`` / ``PermissionMessage``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


# --- content blocks (mirror claude_agent_sdk) ---
@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any
    is_error: bool = False


ContentBlock = Union[TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock]


# --- messages (mirror claude_agent_sdk) ---
@dataclass
class AssistantMessage:
    content: list[ContentBlock]
    model: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Concatenated text of any TextBlocks (convenience)."""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))


@dataclass
class UserMessage:
    content: list[ContentBlock]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResultMessage:
    subtype: str
    total_cost_usd: float | None
    usage: dict[str, Any]
    duration_ms: int | None
    num_turns: int | None
    is_error: bool
    raw: dict[str, Any] = field(default_factory=dict)


# --- Terrarium extensions (human-in-the-loop) ---
@dataclass
class QuestionMessage:
    """An AskUserQuestion prompt. Answer with ``session.answer(question_id, {...})`` or via a
    ``can_use_tool`` callback returning ``PermissionResultAllow(updated_input={"answers": ...})``."""

    question_id: str
    questions: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PermissionMessage:
    """A gated tool-permission request (when the harness sets ``approval``). Resolve with
    ``session.decide(request_id, "allow"|"always"|"deny")`` or a ``can_use_tool`` callback."""

    request_id: str
    tool_name: str
    input: dict[str, Any]
    title: str | None = None
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


Message = Union[AssistantMessage, UserMessage, SystemMessage, ResultMessage, QuestionMessage, PermissionMessage]


def parse_message(ev: dict[str, Any]) -> Message | None:
    """Convert a raw Terrarium event into a typed message, or ``None`` for events with no
    message equivalent (status, rewind_point, answered, decided, …)."""
    t = ev.get("type")
    # The worker stamps the responding model on assistant events (emit(EV_ASSISTANT_TEXT,
    # …, model=msg.model)); carry it through so AssistantMessage.model matches the real SDK.
    model = ev.get("model")
    if t == "assistant_text":
        return AssistantMessage(content=[TextBlock(str(ev.get("text", "")))], model=model, raw=ev)
    if t == "thinking":
        return AssistantMessage(content=[ThinkingBlock(str(ev.get("text", "")))], model=model, raw=ev)
    if t == "tool_use":
        return AssistantMessage(content=[ToolUseBlock(str(ev.get("id", "")), str(ev.get("name", "")), ev.get("input") or {})], model=model, raw=ev)
    if t == "tool_result":
        return UserMessage(content=[ToolResultBlock(str(ev.get("tool_use_id", "")), ev.get("content"), bool(ev.get("is_error")))], raw=ev)
    if t == "result":
        return ResultMessage(
            subtype=str(ev.get("subtype", "")), total_cost_usd=ev.get("total_cost_usd"),
            usage=ev.get("usage") or {}, duration_ms=ev.get("duration_ms"),
            num_turns=ev.get("num_turns"), is_error=bool(ev.get("is_error")), raw=ev)
    if t in ("system", "ready", "session_start", "error", "session_end", "worker_lost"):
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {k: v for k, v in ev.items() if k not in ("type", "seq", "ts")}
        return SystemMessage(subtype=str(ev.get("subtype", t)), data=data, raw=ev)
    if t == "question":
        return QuestionMessage(str(ev.get("question_id", "")), ev.get("questions") or [], raw=ev)
    if t == "permission":
        return PermissionMessage(str(ev.get("request_id", "")), str(ev.get("tool_name", "")),
                                 ev.get("input") or {}, ev.get("title"), ev.get("description"), raw=ev)
    return None
