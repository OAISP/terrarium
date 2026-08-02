"""Options + permission types — modelled on the Claude Agent SDK (``ClaudeAgentOptions``,
``can_use_tool``, ``PermissionResultAllow/Deny``) so the two SDKs feel the same, with extra
fields for Terrarium's own features (personas, approval gating, egress profiles, memory).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Awaitable, Callable, Union


@dataclass
class AgentDefinition:
    """A programmatic subagent — field-for-field the Claude Agent SDK's ``AgentDefinition``
    (same names, incl. its camelCase), so ported code drops in unchanged. Plain dicts (with
    snake_case or camelCase keys) are accepted anywhere this is."""

    description: str
    prompt: str
    tools: list[str] | None = None
    disallowedTools: list[str] | None = None  # noqa: N815 — Claude-SDK name
    model: str | None = None
    skills: list[str] | None = None
    memory: str | None = None                 # "user" | "project" | "local"
    mcpServers: "list[str | dict[str, Any]] | None" = None  # noqa: N815
    initialPrompt: str | None = None          # noqa: N815
    maxTurns: int | None = None               # noqa: N815
    background: bool | None = None
    effort: "str | int | None" = None
    permissionMode: str | None = None         # noqa: N815


def _agent_spec(a: "AgentDefinition | dict[str, Any]") -> dict[str, Any]:
    if is_dataclass(a) and not isinstance(a, type):
        return {k: v for k, v in asdict(a).items() if v is not None}
    return dict(a)


@dataclass
class ToolPermissionContext:
    """Context passed to a ``can_use_tool`` callback (mirrors the Claude SDK's). For a
    permission request it carries the request id + the agent's title/description; for an
    AskUserQuestion it wraps the question event."""

    request_id: str = ""
    title: str | None = None
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PermissionResultAllow:
    """Allow a tool call. ``always`` maps to Terrarium's "always allow (this session)".
    For AskUserQuestion, put the answers under ``updated_input={"answers": {...}}`` —
    exactly as the Claude Agent SDK expects."""

    updated_input: dict[str, Any] | None = None
    always: bool = False


@dataclass
class PermissionResultDeny:
    """Deny a tool call (optionally with a message the agent sees)."""

    message: str = ""


@dataclass
class ClientTool:
    """A custom tool whose handler runs in YOUR process (with your application context) —
    bridged to the sandboxed agent. The agent's tool input crosses out, your handler runs
    here, and only the result you return crosses back in; your code/state/secrets never
    enter the sandbox. Create via :func:`tool`."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: "Callable[[dict[str, Any]], Any]"  # (input) -> str | {"content","is_error"} | awaitable

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def tool(name: str, description: str, input_schema: dict[str, Any] | None = None):
    """Decorator turning an (async or sync) ``handler(input) -> result`` into a :class:`ClientTool`.
    Pass the resulting tools to ``TerrariumOptions(tools=[...])``. Mirrors the Claude Agent SDK's
    ``@tool`` — except the handler executes client-side, never in the sandbox.

        @tool("get_user", "Look up a user by id", {"id": {"type": "string"}})
        async def get_user(args):
            return await db.users.find(args["id"])   # your app context, your process
    """
    def deco(fn):
        return ClientTool(name=name, description=description, input_schema=input_schema or {}, handler=fn)
    return deco


PermissionResult = Union[PermissionResultAllow, PermissionResultDeny]
# A permission callback (like the Claude Agent SDK's). May be sync OR async — the client
# awaits the result if it's awaitable.
CanUseTool = Callable[[str, dict[str, Any], ToolPermissionContext], Union[PermissionResult, Awaitable[PermissionResult]]]

# Harness keys the orchestrator understands (kept in sync with terrarium/harness.py).
_HARNESS_FIELDS = (
    "model", "system_mode", "custom_prompt", "permission_mode", "allowed_tools", "builtin_tools",
    "thinking", "effort", "fallback_model", "max_thinking_tokens", "betas",
    "max_turns", "max_budget_usd", "mcp_servers", "agents", "skills",
    "interactive", "approval", "setting_sources", "env", "extra_options",
    "environments", "memory_mode",
)


@dataclass
class TerrariumOptions:
    """Configuration for a Terrarium agent/session — a superset of the Claude Agent SDK's
    ``ClaudeAgentOptions``.

    The first block mirrors the Claude SDK field-for-field (so existing knowledge carries
    over); the second block adds Terrarium-only capabilities. ``None`` means "leave to the
    orchestrator default" — only set fields are sent.
    """

    # --- Claude Agent SDK-aligned ---
    model: str | None = None                      # "sonnet" | "opus" | "haiku" | full id
    system_prompt: "str | dict[str, Any] | None" = None  # custom prompt (→ custom persona), or the
    #     Claude-SDK preset dict {"type":"preset","preset":"claude_code"} (→ claude_code persona)
    allowed_tools: list[str] | None = None        # AUTO-APPROVE list (Claude-SDK: skips the prompt;
    #                                                does NOT change which tools the agent has)
    builtin_tools: "list[str] | dict[str, Any] | None" = None  # AVAILABILITY allowlist — the base set
    #     of built-in tools the agent may use. None=all defaults; ["Read","Grep"]=only those; []=none;
    #     {"type":"preset","preset":"claude_code"}=all defaults. This is the real "restrict tools" knob.
    permission_mode: str | None = None            # default | acceptEdits | plan | bypassPermissions
    max_turns: int | None = None
    max_budget_usd: float | None = None
    mcp_servers: dict[str, Any] | None = None
    agents: "dict[str, AgentDefinition | dict[str, Any]] | None" = None  # programmatic subagents
    #     (SDK `agents`): name -> AgentDefinition, or a plain dict ({"description", "prompt",
    #     and optionally "tools", "disallowed_tools", "model", "skills", "max_turns", ...})
    thinking: dict[str, Any] | None = None         # e.g. {"type": "adaptive"} / {"type": "enabled", "budget_tokens": N}
    effort: str | None = None                      # low | medium | high | xhigh | max
    fallback_model: str | None = None              # model to retry with on overload/refusal
    max_thinking_tokens: int | None = None         # hard cap on thinking tokens per turn
    betas: list[str] | None = None                 # API beta flags to opt into
    setting_sources: list[str] | None = None       # ["user", "project", "local"]
    env: dict[str, str] | None = None
    skills: "bool | list[str] | str | None" = None  # True=mount+discover; "all"; [names]=only these;
    #     []=NO skills at all (hides the CLI's built-in skills too — the "bare harness" setting)
    memory_mode: str | None = None                  # "volume" (default, durable mount) | "synced"
    #     (snapshot in/out — much faster k8s launch, loses writes since the last turn if the pod
    #     dies abruptly) | "none" (container-local scratch, discarded on stop)
    can_use_tool: CanUseTool | None = None          # (tool, input, ctx) -> Allow | Deny
    tools: list[ClientTool] | None = None           # custom tools that run in YOUR process (see `tool`)

    # --- Terrarium extensions ---
    system_mode: str | None = None                  # minimal | claude_code | custom | assistant
    custom_prompt: str | None = None                # alias for system_prompt (explicit persona text)
    approval: "str | list[str] | None" = None       # off | edits | all | [tool names] — human-in-the-loop gating
    interactive: bool | None = None                 # allow AskUserQuestion / approval prompts to block for an operator
    environments: list[str] | None = None           # attach to {secrets, egress} bundles — the sole per-agent
    #     egress + secret-scoping mechanism. None/[] = no operator secrets + global egress; a list =
    #     ONLY those environments' secrets, and egress merged from their profiles (enforce wins; hosts union).
    memory_scope: str | None = None                 # share memory with another agent id
    extra_options: dict[str, Any] | None = None     # new SDK fields only; Terrarium-managed keys are rejected

    # --- session attach / routing (not part of the harness) ---
    agent_id: str | None = None                     # attach the session to an existing agent
    title: str | None = None

    def to_harness(self) -> dict[str, Any]:
        """Serialize the harness-relevant fields for the create-agent/create-session API."""
        h: dict[str, Any] = {}
        # `system_prompt` is the Claude-SDK name for a bespoke prompt → Terrarium custom
        # persona. Its preset dict form maps onto the matching system_mode.
        sp = self.system_prompt
        preset_mode: str | None = None
        if isinstance(sp, dict):
            if sp.get("type") == "preset" and sp.get("preset") == "claude_code" and "append" not in sp:
                sp = None
                preset_mode = "claude_code"
            else:  # unknown preset / "append" — no Terrarium equivalent; refuse loudly
                raise ValueError(
                    f"unsupported system_prompt {sp!r}: only "
                    '{"type": "preset", "preset": "claude_code"} (without "append") maps to a '
                    "persona; for appended guidance use system_prompt=<full text> instead"
                )
        # A claude_code preset can't be combined with a custom prompt or an explicit
        # system_mode — either would silently clobber the preset. Refuse loudly (matching the
        # unknown-preset/unknown-kwarg behavior) rather than picking one.
        if preset_mode and (self.custom_prompt is not None or self.system_mode is not None):
            raise ValueError(
                'conflicting persona: system_prompt={"preset": "claude_code"} cannot be '
                "combined with custom_prompt or an explicit system_mode — set only one"
            )
        custom = self.custom_prompt or sp
        if custom is not None:
            h["custom_prompt"] = custom
            h["system_mode"] = self.system_mode or "custom"
        elif self.system_mode is not None:
            h["system_mode"] = self.system_mode
        elif preset_mode is not None:
            h["system_mode"] = preset_mode
        for k in _HARNESS_FIELDS:
            if k in ("custom_prompt", "system_mode"):
                continue
            v = getattr(self, k, None)
            if v is not None:
                h[k] = v
        if self.agents:  # AgentDefinition dataclasses → plain dicts for the wire
            h["agents"] = {name: _agent_spec(a) for name, a in self.agents.items()}
        if self.tools:  # send only the SCHEMAS; the handlers stay client-side
            h["client_tools"] = [t.schema() for t in self.tools]
        return h
