"""Environments — named bundles of {secrets, egress profile} an agent attaches to.

An environment groups operator injection secrets (by name) and, optionally, an egress
profile. An agent attaches to zero or more environments via ``harness.environments``; its
sessions then receive ONLY those environments' secrets (least privilege) and an egress
policy merged from their profiles.

An agent with no environments receives no operator secrets and uses the global egress
policy. Attaching environments grants only the union of their named secrets.

Storage mirrors :class:`EgressProfileStore`: one JSON file on the PVC, a lock, atomic
rename. References are by NAME (secrets) / id (egress profile); the API prevents deleting
referenced environments/profiles and runtime resolution fails closed if storage is edited
out of band.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from terracore.events import now_iso

from .store import JsonStore


def _new_id() -> str:
    return "env_" + os.urandom(4).hex()


# Sentinel: distinguishes "field omitted from a PATCH" from an explicit null (detach).
_UNSET = object()


def _clean_names(names: Any) -> list[str]:
    """De-dup + strip a list of secret names, preserving first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for n in names or []:
        n = str(n).strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


class EnvironmentStore(JsonStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._envs: dict[str, dict[str, Any]] = {
            e["id"]: e for e in (self._read({}) or {}).get("environments", []) if e.get("id")
        }

    def _save(self) -> None:
        self._write({"environments": list(self._envs.values())})

    def create(self, *, name: str, description: str = "", secrets: Any = None,
               egress_profile: str | None = None) -> dict[str, Any]:
        with self.lock:
            eid = _new_id()
            env = {
                "id": eid, "name": (name or eid).strip(), "description": (description or "").strip(),
                "secrets": _clean_names(secrets), "egress_profile": (egress_profile or None),
                "created_at": now_iso(), "updated_at": now_iso(),
            }
            self._envs[eid] = env
            self._save()
            return env

    def get(self, eid: str | None) -> dict[str, Any] | None:
        if not eid:
            return None
        return self._envs.get(eid)

    def list(self) -> list[dict[str, Any]]:
        return list(self._envs.values())

    def update(self, eid: str, *, name: str | None = None, description: str | None = None,
               secrets: Any = None, egress_profile: Any = _UNSET) -> dict[str, Any] | None:
        with self.lock:
            env = self._envs.get(eid)
            if not env:
                return None
            if name is not None:
                env["name"] = name.strip()
            if description is not None:
                env["description"] = description.strip()
            if secrets is not None:
                env["secrets"] = _clean_names(secrets)
            # egress_profile uses a sentinel so an explicit null (detach) is distinct from
            # "not in this PATCH" — mirrors the agent-update identity handling.
            if egress_profile is not _UNSET:
                env["egress_profile"] = egress_profile or None
            env["updated_at"] = now_iso()
            self._save()
            return env

    def delete(self, eid: str) -> bool:
        with self.lock:
            if eid not in self._envs:
                return False
            del self._envs[eid]
            self._save()
            return True
