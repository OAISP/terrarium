"""Agent registry — durable, versioned agent configs.

Create an agent once (name + a full Harness + memory scope), then reference it
by id on every session. Sessions stay ephemeral; agents are persistent and carry
their own harness and memory scope.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from terracore.harness import HARNESS_FIELDS, Harness

from .store import JsonStore

# Sentinel for AgentStore.update: distinguishes "field not provided" from an explicit
# ``None`` (which means clear it). Without this, a cleared field silently reverts.
_UNSET: Any = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class AgentSpec:
    id: str
    name: str
    harness: Harness = field(default_factory=Harness)
    # Memory volume key. Default = the agent's own id (isolated). Point two
    # agents at the same scope to deliberately share a memory.
    memory_scope: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    version: int = 1

    def memory_volume(self) -> str:
        return f"terrarium-mem-{self.memory_scope or self.id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"memory_volume": self.memory_volume()}


_SPEC_FIELDS = {f.name for f in fields(AgentSpec)}


class AgentStore(JsonStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._agents: dict[str, AgentSpec] = {}
        for aid, raw in (self._read({}) or {}).items():
            d = {k: v for k, v in raw.items() if k in _SPEC_FIELDS}
            if isinstance(d.get("harness"), dict):
                d["harness"] = Harness.from_dict(d["harness"])
            self._agents[aid] = AgentSpec(**d)

    def _save(self) -> None:
        self._write({aid: asdict(a) for aid, a in self._agents.items()})

    def create(self, name: str, harness: Harness | None = None, memory_scope: str = "") -> AgentSpec:
        with self.lock:
            aid = "agt_" + uuid.uuid4().hex[:8]
            spec = AgentSpec(id=aid, name=name, harness=harness or Harness(), memory_scope=memory_scope or "")
            if not spec.memory_scope:
                spec.memory_scope = aid
            self._agents[aid] = spec
            self._save()
            return spec

    def get(self, aid: str) -> AgentSpec | None:
        return self._agents.get(aid)

    def list(self) -> list[AgentSpec]:
        return list(self._agents.values())

    def update(
        self,
        aid: str,
        *,
        name: Any = _UNSET,
        memory_scope: Any = _UNSET,
        harness_updates: dict[str, Any] | None = None,
    ) -> AgentSpec | None:
        with self.lock:
            spec = self._agents.get(aid)
            if not spec:
                return None
            # ``_UNSET`` (not provided) is distinct from an explicit ``None`` (clear) —
            # so the caller can set memory_scope back to isolated (None) and have it stick.
            if name is not _UNSET:
                spec.name = name
            if memory_scope is not _UNSET:
                spec.memory_scope = memory_scope
            # The caller has already narrowed this to explicitly-provided fields
            # (api uses model_dump(exclude_unset=True)), so an explicit ``null`` here
            # means "clear it" (e.g. max_budget_usd → unlimited, allowed_tools → default
            # set). Don't drop None or those clears silently revert on the next load.
            for k, v in (harness_updates or {}).items():
                if k in HARNESS_FIELDS:
                    setattr(spec.harness, k, v)
            spec.version += 1
            spec.updated_at = _now()
            self._save()
            return spec

    def delete(self, aid: str) -> bool:
        with self.lock:
            if aid in self._agents:
                del self._agents[aid]
                self._save()
                return True
            return False
