"""Egress profiles — named, reusable bundles of allow/deny/inspect rules.

The global egress policy (``EgressPolicyStore``) is the default. A *profile* is a named
rule bundle (e.g. "github+pypi", "anthropic-only") that reaches an agent through an
ENVIRONMENT the agent attaches to — there is no per-agent egress pin (the old
``harness.egress_profile`` was removed; see migrations.migrate_agent_egress_pins).
Sessions of an attached agent get the merged profile rules instead of the global ones,
and the global KILL switch always overrides (panic button).

Same hygiene as the global policy: each destination is a domain, IP literal, or CIDR
(wildcards dropped, IP/CIDR canonicalized) — a private CIDR here grants that agent
internal-network reach.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from terracore.events import now_iso

from .egress import VALID_MODES, clean_host_overrides, clean_rules, record_rules
from .store import JsonStore


def _new_id() -> str:
    return "egp_" + os.urandom(4).hex()


def _allow(*dests: str) -> list[dict[str, Any]]:
    """Allow rules on the default ports — the shape almost every preset wants."""
    return clean_rules([{"action": "allow", "dest": d} for d in dests])


# Built-in presets — curated allow-lists for common workflows, instantiable into a real
# profile via the API/SDK (`preset=<key>`). Exact hosts only (wildcards are dropped by the
# host hygiene). Anthropic is always reachable regardless (mandatory, hardcoded in Warden),
# so it never needs listing. `enforce` blocks anything not listed; `monitor` blocks nothing
# but audits every connection.
#
# Authored as rules[] — the canonical model the store, the editor and Warden all read — so
# instantiating one is a copy, not a translation.
EGRESS_PRESETS: dict[str, dict[str, Any]] = {
    "anthropic-only": {
        "name": "Anthropic only",
        "description": "Maximum isolation — only the model API is reachable; every other host is blocked.",
        "mode": "enforce", "rules": [],
    },
    "developer": {
        "name": "Developer",
        "description": "Git hosting + the major language package registries (Python, npm, Rust, Go).",
        "mode": "enforce",
        "rules": _allow(
            # git hosting
            "github.com", "api.github.com", "codeload.github.com",
            "raw.githubusercontent.com", "objects.githubusercontent.com", "ghcr.io",
            "gitlab.com",
            # Python
            "pypi.org", "files.pythonhosted.org",
            # JavaScript
            "registry.npmjs.org", "registry.yarnpkg.com",
            # Rust
            "crates.io", "static.crates.io", "index.crates.io",
            # Go
            "proxy.golang.org", "sum.golang.org", "pkg.go.dev",
        ),
    },
    "python": {
        "name": "Python",
        "description": "pip/PyPI plus GitHub (for VCS installs). Nothing else.",
        "mode": "enforce",
        "rules": _allow("pypi.org", "files.pythonhosted.org",
                        "github.com", "codeload.github.com", "raw.githubusercontent.com"),
    },
    "node": {
        "name": "Node.js",
        "description": "npm / Yarn registries only.",
        "mode": "enforce",
        "rules": _allow("registry.npmjs.org", "registry.yarnpkg.com"),
    },
    "data-science": {
        "name": "Data science / ML",
        "description": "PyPI + Hugging Face models & datasets.",
        "mode": "enforce",
        "rules": _allow("pypi.org", "files.pythonhosted.org",
                        "huggingface.co", "cdn-lfs.huggingface.co", "datasets-server.huggingface.co"),
    },
    "web-audit": {
        "name": "Open web (audit-only)",
        "description": "Blocks nothing but logs every connection — for trusted exploratory research where you want a full audit trail without an allow-list.",
        "mode": "monitor", "rules": [],
    },
}


def list_presets() -> list[dict[str, Any]]:
    """The built-in presets as a list (each carries its ``key``), for the API/SDK."""
    return [{"key": k, **v} for k, v in EGRESS_PRESETS.items()]


class EgressProfileStore(JsonStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._profiles: dict[str, dict[str, Any]] = {}
        for p in (self._read({}) or {}).get("profiles", []):
            if not p.get("id"):
                continue
            # Migrate a pre-rules profile forward once, then drop the legacy list fields.
            p["rules"] = record_rules(p)
            p["hosts"] = clean_host_overrides(p.get("hosts"))
            for k in ("allow", "deny", "inspect"):
                p.pop(k, None)
            self._profiles[p["id"]] = p

    def _save(self) -> None:
        self._write({"profiles": list(self._profiles.values())})

    @staticmethod
    def _normalize(mode: Any, rules: Any, hosts: Any) -> dict[str, Any]:
        m = mode if mode in VALID_MODES else "enforce"
        return {"mode": m, "rules": clean_rules(rules), "hosts": clean_host_overrides(hosts)}

    def create(self, *, name: str, mode: str = "enforce", rules: Any = None, hosts: Any = None) -> dict[str, Any]:
        with self.lock:
            pid = _new_id()
            prof = {"id": pid, "name": (name or pid).strip(),
                    **self._normalize(mode, rules, hosts),
                    "created_at": now_iso(), "updated_at": now_iso()}
            self._profiles[pid] = prof
            self._save()
            return prof

    def create_preset(self, preset: str, *, name: str | None = None) -> dict[str, Any]:
        """Instantiate a built-in preset (see ``EGRESS_PRESETS``) into a real profile.
        ``name`` overrides the preset's default display name."""
        p = EGRESS_PRESETS.get(preset)
        if not p:
            raise KeyError(f"unknown egress preset: {preset!r} (have: {', '.join(EGRESS_PRESETS)})")
        return self.create(name=name or p["name"], mode=p["mode"], rules=p["rules"], hosts=p.get("hosts"))

    def get(self, pid: str | None) -> dict[str, Any] | None:
        if not pid:
            return None
        return self._profiles.get(pid)

    def list(self) -> list[dict[str, Any]]:
        return list(self._profiles.values())

    def update(self, pid: str, *, name: str | None = None, mode: str | None = None,
               rules: Any = None, hosts: Any = None) -> dict[str, Any] | None:
        with self.lock:
            prof = self._profiles.get(pid)
            if not prof:
                return None
            if name is not None:
                prof["name"] = name.strip()
            prof.update(self._normalize(
                mode if mode is not None else prof["mode"],
                rules if rules is not None else prof["rules"],
                hosts if hosts is not None else prof.get("hosts"),
            ))
            prof["updated_at"] = now_iso()
            self._save()
            return prof

    def delete(self, pid: str) -> bool:
        with self.lock:
            if pid in self._profiles:
                del self._profiles[pid]
                self._save()
                return True
            return False
