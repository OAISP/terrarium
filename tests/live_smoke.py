#!/usr/bin/env python3
"""Live smoke via the SDK: configurable harness + skills + the SDK itself.

Requires a running orchestrator (`make run`). Creates an agent with a custom
harness (haiku, adaptive thinking, restricted tools, skills on), runs one turn,
and reports what it observed. Prints the session id so the caller can inspect
the persisted log for the loaded skill.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from terrarium import TerrariumClient


async def main() -> None:
    async with TerrariumClient("http://127.0.0.1:8900") as c:
        print("HEALTH", await c.health())

        agent = await c.agents.create(
            name="HarnessDemo",
            model="haiku",
            system_mode="assistant",
            thinking={"type": "adaptive"},
            allowed_tools=["Read", "Write", "Bash"],
            skills=True,
        )
        h = agent["harness"]
        print(f"AGENT {agent['id']} · model={h['model']} thinking={h['thinking']} skills={h['skills']}")
        print(f"ALLOWED_TOOLS {h['allowed_tools']}")
        print(f"MEMORY_VOLUME {agent['memory_volume']}")

        async with c.session(agent_id=agent["id"]) as s:
            print(f"SID {s.id}")
            reply = await s.run(
                "Use the Bash tool to compute 144/12, then explain what division is in one short sentence."
            )
            used_bash = any(
                e["type"] == "tool_use" and "Bash" in str(e.get("name", ""))
                for e in reply["events"]
            )
            has_tldr = "TLDR" in reply["text"]
            print(f"COST {reply['cost_usd']}")
            print(f"USED_BASH {used_bash}")
            print(f"SKILL_APPLIED(TLDR) {has_tldr}")
            print(f"REPLY {reply['text'][:300]!r}")


if __name__ == "__main__":
    asyncio.run(main())
