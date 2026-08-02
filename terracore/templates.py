"""Built-in agent templates — one-click useful agents.

A template is just a name + description + a :class:`Harness` preset; *not* a new
storage system. ``POST /v1/agents`` may pass ``template: <id>`` to seed the
harness, and any explicit request fields override the template's defaults.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .harness import Harness
from .models import HAIKU, OPUS, SONNET


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    description: str
    harness: Harness


_TEMPLATES: dict[str, Template] = {}


def _register(t: Template) -> Template:
    _TEMPLATES[t.id] = t
    return t


_register(Template(
    "research", "Researcher",
    "Web research + note-taking assistant (search, fetch, read/write, bash).",
    Harness(model=SONNET, system_mode="assistant",
            allowed_tools=["WebSearch", "WebFetch", "Read", "Write", "Bash"],
            thinking={"type": "adaptive"}, effort="high"),
))
_register(Template(
    "coder", "Coder",
    "Full Claude Code in a sandbox — high effort, auto-accept edits.",
    Harness(model=OPUS, system_mode="claude_code",
            permission_mode="acceptEdits", effort="xhigh"),
))
_register(Template(
    "github-pr", "GitHub PR agent",
    "Claude Code tuned to open a PR (mount a repo + a GitHub secret per Warden).",
    Harness(model=OPUS, system_mode="claude_code",
            permission_mode="acceptEdits", effort="high"),
))
_register(Template(
    "tldr", "TL;DR",
    "Fast summarizer wired to the bundled tldr skill.",
    Harness(model=HAIKU, system_mode="assistant", skills="all"),
))
_register(Template(
    "bare", "Bare harness",
    "Claude with the default harness stripped: a minimal tool set, no skills or slash "
    "commands (built-ins included), no subagents.",
    Harness(model=SONNET, system_mode="minimal",
            builtin_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"], skills=[]),
))


def list_templates() -> list[dict[str, Any]]:
    return [
        {"id": t.id, "name": t.name, "description": t.description,
         "harness": json.loads(t.harness.to_json())}
        for t in _TEMPLATES.values()
    ]


def get(template_id: str) -> Template | None:
    return _TEMPLATES.get(template_id)
