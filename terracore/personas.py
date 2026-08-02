"""System-prompt modes — the lever that turns the Claude Code *runtime* into a
general assistant instead of a coding-only tool.

    minimal     -> None  (SDK default: tool-calling only; NOT the Claude Code prompt)
    claude_code -> the full Claude Code harness, with an appended hint
    custom      -> only the provided string (replaces the harness entirely)
    assistant   -> a general personal-assistant persona on top of the runtime
"""

from __future__ import annotations

from typing import Any

# Concealment: describe CAPABILITIES, not the boundary. Saying "sandboxed" (or
# naming the product) volunteers to the agent that it is contained — a free
# fingerprint that primes an adversarial/injected agent to probe the isolation.
# The agent should read as a normal assistant on a normal workstation.
ASSISTANT_PROMPT = (
    "You are a capable personal assistant. You have a working directory, a "
    "persistent memory directory, web search, and the ability to write and run "
    "code. Use them to actually complete tasks — research, summarize, compute, "
    "organize, automate — not just describe how. Keep your working directory tidy "
    "and record durable notes to /memory when something is worth remembering."
)

CUSTOM_PROMPT = "You are a concise assistant. "

MODES = ("minimal", "claude_code", "custom", "assistant")


def build_system_prompt(mode: str, custom_text: str | None = None) -> Any:
    if mode == "minimal":
        return None
    if mode == "claude_code":
        return {"type": "preset", "preset": "claude_code"}
    if mode == "assistant":
        return ASSISTANT_PROMPT
    # custom
    return custom_text or CUSTOM_PROMPT
