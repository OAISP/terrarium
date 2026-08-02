"""The tool + skill catalog — one source of truth for what an agent can be given.

Shipped over ``GET /v1/tools`` so the console renders what this orchestrator actually
supports, exactly like the model catalog in :mod:`terracore.models`. The console and the
worker read the same source; a second list in another language has nothing to keep it
honest when a CLI upgrade changes the tool set.

Deliberately import-light (no ``claude_agent_sdk``): the orchestrator serves this and must
not pull the agent SDK in to do it.

Neither list is authoritative over the CLI. The tool set varies by CLI version and the
skill set by what is installed, so both the availability picker and the skill picker keep
a free-text escape hatch — this catalog decides what is *offered*, never what is allowed.
"""

from __future__ import annotations

from typing import Any

# Built-in tools, grouped the way an operator reasons about them rather than the way the
# CLI enumerates them: what it can touch, what it can reach, what it can delegate to.
TOOL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Files & shell", ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "NotebookEdit")),
    ("Web", ("WebSearch", "WebFetch")),
    ("Subagents & skills", ("Task", "Workflow", "Skill", "ToolSearch")),
    ("Task tracking", ("TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate")),
    ("Automation & scheduling", ("ScheduleWakeup", "CronCreate", "CronDelete", "CronList",
                                 "Monitor", "RemoteTrigger", "PushNotification", "SendMessage")),
    ("Worktree & misc", ("EnterWorktree", "ExitWorktree", "DesignSync")),
)

ALL_TOOLS: tuple[str, ...] = tuple(t for _label, tools in TOOL_GROUPS for t in tools)

# The auto-approve set a harness gets when `allowed_tools` is unset. NOT an availability
# list — it decides which tools skip the permission prompt (see Harness.allowed_tools).
DEFAULT_BUILTINS: tuple[str, ...] = (
    "Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebSearch", "WebFetch",
)

# Named starting points for the availability allowlist.
TOOL_PRESETS: dict[str, tuple[str, ...]] = {
    "All": ALL_TOOLS,
    "Coding": ("Read", "Write", "Edit", "Glob", "Grep", "Bash"),
    "Read-only": ("Read", "Glob", "Grep", "WebSearch", "WebFetch"),
    "None": (),
}

# Claude Code's built-in skills, for the picker. Project/plugin skills and any built-in a
# newer CLI adds are reached through the form's free-text field.
KNOWN_SKILLS: tuple[str, ...] = (
    "deep-research", "code-review", "security-review", "simplify", "verify", "debug",
    "run", "dataviz", "claude-api", "schedule", "loop", "update-config",
)


def catalog() -> dict[str, Any]:
    """Wire form for ``GET /v1/tools``."""
    return {
        "groups": [{"label": label, "tools": list(tools)} for label, tools in TOOL_GROUPS],
        "presets": {name: list(tools) for name, tools in TOOL_PRESETS.items()},
        "defaults": list(DEFAULT_BUILTINS),
        "skills": list(KNOWN_SKILLS),
    }
