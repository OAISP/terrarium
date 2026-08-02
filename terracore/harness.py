"""The agent harness configuration — the full set of knobs a developer can set
per agent, mapping onto the Claude Agent SDK's ``ClaudeAgentOptions``.

This is a pure schema (no SDK import) so both the host orchestrator and the
in-container worker can share it. The orchestrator serializes a Harness to the
``TERRA_HARNESS`` env var; the worker deserializes it and builds the SDK options.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass
class Harness:
    # --- core ---
    model: str = "sonnet"                       # alias or full id (sonnet/opus/haiku/...)
    system_mode: str = "assistant"              # minimal | claude_code | custom | assistant
    custom_prompt: str | None = None            # used when system_mode == "custom"

    # --- tools & permissions ---
    allowed_tools: list[str] | None = None      # AUTO-APPROVE list (Claude-SDK-aligned: skips the
    #                                             permission prompt; does NOT change availability)
    # The base set of built-in tools the agent HAS (Claude Agent SDK's `tools` field — a real
    # availability allowlist). None = all defaults; a list = ONLY those (e.g. ["Read","Grep"]);
    # [] = no built-ins; {"type":"preset","preset":"claude_code"} = all defaults. Replaces the old
    # disallowed_tools blacklist with a whitelist (easier + future-proof: presets, no hardcoded list).
    builtin_tools: "list[str] | dict[str, Any] | None" = None
    permission_mode: str = "bypassPermissions"  # default | acceptEdits | plan | bypassPermissions
    mcp_servers: dict[str, Any] | None = None   # name -> server config (e.g. {"type":"url","url":...})
    # Programmatic subagents (the SDK's `agents` field): name -> definition dict
    # ({"description", "prompt", and optionally "tools", "disallowed_tools", "model",
    # "skills", "max_turns", "effort", "permission_mode", ...}; snake_case or the SDK's
    # camelCase both work). These are ADDED to the CLI's built-in agent roster; to remove
    # the built-in subagent system entirely, leave "Task" out of `builtin_tools`.
    agents: dict[str, dict[str, Any]] | None = None
    client_tools: list[dict[str, Any]] | None = None  # SDK-provided tool SCHEMAS [{name,description,input_schema}];
    #                                                   each call is bridged to the dev's process (never runs in-sandbox)

    # --- reasoning / effort ---
    # e.g. {"type":"adaptive","display":"summarized"} or
    #      {"type":"enabled","budget_tokens":20000} or {"type":"disabled"}
    thinking: dict[str, Any] | None = None
    # Claude CLI "thinking level" — low|medium|high|xhigh|max. Guides thinking
    # depth alongside adaptive thinking. (xhigh: Opus 4.7+; Haiku ignores it.)
    effort: str | None = None

    # --- model / reasoning (Claude-SDK-aligned extras) ---
    fallback_model: str | None = None           # model to retry with on overload/refusal
    max_thinking_tokens: int | None = None       # hard cap on thinking tokens per turn
    betas: list[str] | None = None              # API beta flags to opt into (e.g. server-side fallback)

    # --- guardrails ---
    max_turns: int | None = None                # cap agentic turns
    max_budget_usd: float | None = None         # cap spend per session

    # --- interactivity ---
    # Let the agent ask the operator structured questions mid-run (the Claude Code
    # AskUserQuestion tool). Off by default so unattended/scheduled agents never block
    # waiting for a human; turn it on for console-driven sessions that want clarifying
    # questions. When on, the worker routes AskUserQuestion through a permission callback
    # and surfaces it to the UI/SDK; all other tools stay auto-approved (unless
    # `approval` below gates them).
    interactive: bool = False

    # Human-in-the-loop tool approval — which tool uses the operator must approve before
    # they run (the Claude Code permission prompt). Only effective for an `interactive`
    # session (an unattended one has no operator, so it auto-allows and never blocks):
    #   "off"   — auto-approve everything (default; the OS sandbox is the boundary)
    #   "edits" — prompt only for file writes/edits
    #   "all"   — prompt for every tool that needs permission
    #   [...]   — prompt only for the named tools (e.g. ["Bash", "WebFetch"])
    approval: "str | list[str]" = "off"

    # --- extensions ---
    # Skill availability. "all"/[names]/[] map to the SDK's `skills` option — the switch that
    # actually ENABLES the Skill tool. Defaults to "all" so a new agent can run the CLI's built-in
    # skills (deep-research, code-review, verify, …) out of the box. A bare bool is the legacy knob
    # and does NOT enable the Skill tool, so built-ins end up LISTED-but-unrunnable (the model sees
    # them and tries them, but the Skill tool rejects the call).
    #   "all"   — enable every discovered skill (built-in + mounted)          [default]
    #   [names] — enable ONLY the named skills
    #   []      — NO skills at all: hides every skill, including the CLI's built-ins
    #   True    — legacy: mount skills/ + discover, but does NOT enable the Skill tool
    #   False   — legacy: don't mount skills/; built-ins stay listed-but-unrunnable
    skills: "bool | list[str] | str" = "all"
    setting_sources: list[str] | None = None    # ["user","project"] — CLAUDE.md/settings/skills
    env: dict[str, str] | None = None           # extra env for the CLI (effort knobs, flags)

    # --- security ---
    # How /memory is provided. This is the single biggest lever on k8s session startup: mounting
    # the per-agent RWO Longhorn PVC costs ~11s of volume attach (measured: 1.6s pod start without
    # it, 11.4s with), which is ~half of a ~23s launch. Docker mounts a local volume, so the cost
    # there is ~0 and all three modes behave the same.
    #   "volume" — mount the per-agent PVC at /memory.
    #              Durable through anything (pod kill, node loss); pays the attach on every launch.
    #   "synced" — no mount. [default] The orchestrator restores a snapshot into the pod before the agent
    #              runs, and snapshots back out at each turn end and on stop. Fast launch, and the
    #              snapshot lives on the orchestrator's replicated volume — but writes since the
    #              last snapshot are lost if the pod dies abruptly.
    #   "none"   — no mount, no snapshot. /memory is container-local scratch, discarded on stop.
    #              Fastest; correct for stateless/ephemeral agents that never take notes.
    memory_mode: str = "synced"

    # Environments the agent attaches to — named bundles of {secrets, egress profile}.
    # This is the ONLY per-agent egress mechanism: egress is ALWAYS mediated by Warden,
    # and which allow/deny/inspect rules apply comes from the attached environments'
    # profiles (merged: enforce wins, hosts union), else the global policy.
    # None/[] = no operator secrets + the global egress policy. A non-empty list grants only
    # the union of the environments' named secrets and applies their merged egress. Dangling
    # environment/profile references fail closed for egress.
    environments: list[str] | None = None

    # --- escape hatch ---
    extra_options: dict[str, Any] | None = None  # forward-compatible SDK fields; managed keys rejected

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Harness":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    @classmethod
    def from_json(cls, s: str) -> "Harness":
        return cls.from_dict(json.loads(s))


HARNESS_FIELDS = {f.name for f in fields(Harness)}
