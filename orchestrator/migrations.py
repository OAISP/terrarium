"""One-shot data migrations run at orchestrator startup.

These operate on the RAW persisted JSON (not the typed models), so they can read
fields that the current code has since dropped from its dataclasses — e.g. the
now-removed agent-level ``harness.egress_profile`` pin, which ``Harness.from_dict``
would silently discard on load. Each migration is idempotent: safe to run every boot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def migrate_agent_egress_pins(agents_path: Path, environments: Any, profiles: Any) -> int:
    """Convert every agent's legacy direct ``harness.egress_profile`` pin into an
    attached ENVIRONMENT, then drop the pin. Egress is now expressed only through
    environments (a bundle of {secrets, egress profile}); a direct agent pin no longer
    exists. Without this, a deploy that removed the field would silently drop those
    agents' egress to the global policy.

    For each pinned profile we reuse (or create once) an egress-only environment named
    after the profile, so agents sharing a profile share one environment rather than
    spawning duplicates. Idempotent: once migrated the key is gone, so re-runs are no-ops.
    Returns the number of agents migrated.
    """
    if not agents_path.exists():
        return 0
    try:
        data = json.loads(agents_path.read_text() or "{}")
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    # Reuse an existing egress-only environment for a given profile id, else make one.
    # Seed the cache from current environments so a re-run (or a prior manual env) reuses.
    by_profile: dict[str, str] = {}
    try:
        for env in environments.list():
            if env.get("egress_profile") and not env.get("secrets"):
                by_profile.setdefault(env["egress_profile"], env["id"])
    except Exception:  # noqa: BLE001 — a listing hiccup shouldn't block boot
        pass

    def env_for(profile_id: str) -> str | None:
        if profile_id in by_profile:
            return by_profile[profile_id]
        prof = None
        try:
            prof = profiles.get(profile_id)
        except Exception:  # noqa: BLE001
            prof = None
        # A pin to a now-deleted profile still migrates to an env referencing that id
        # (harmless dangling ref, skipped at resolution) so no egress is silently lost.
        label = (prof or {}).get("name") or profile_id
        try:
            created = environments.create(
                name=f"egress: {label}",
                description="Auto-created from a legacy per-agent egress pin.",
                secrets=[], egress_profile=profile_id)
        except Exception:  # noqa: BLE001
            return None
        by_profile[profile_id] = created["id"]
        return created["id"]

    migrated = 0
    changed = False
    for _aid, raw in data.items():
        h = raw.get("harness")
        if not isinstance(h, dict) or not h.get("egress_profile"):
            continue
        pin = h.pop("egress_profile")  # drop the pin regardless of what happens next
        changed = True
        eid = env_for(pin)
        if eid:
            envs = h.get("environments") or []
            if eid not in envs:
                envs.append(eid)
            h["environments"] = envs
            migrated += 1

    if changed:
        tmp = agents_path.with_suffix(".migrating")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(agents_path)
    return migrated


def migrate_pin_memory_mode(agents_path: Path) -> int:
    """Pin every PRE-EXISTING agent to ``memory_mode="volume"``.

    New agents now default to ``"synced"`` (no PVC mount → ~11s faster launch on k8s), but the two
    modes read different stores: ``volume`` memory lives in the agent's RWO PVC, ``synced`` memory in
    an orchestrator-side snapshot. Neither reads the other. So flipping the default alone would make
    every existing agent silently start with an empty ``/memory`` — it would look like they'd all
    forgotten everything, while their real memory sat untouched in a volume nobody mounts.

    Writing the old default explicitly is the fix: behaviour is frozen at what the operator already
    had, and never again depends on which way a dataclass default happens to point. Opting an
    existing agent into ``synced`` stays a deliberate, warned choice in the console.

    Idempotent: once the key exists it is never rewritten. Returns the number of agents pinned.
    """
    if not agents_path.exists():
        return 0
    try:
        data = json.loads(agents_path.read_text() or "{}")
    except Exception:  # noqa: BLE001 — a malformed store shouldn't block boot
        return 0
    if not isinstance(data, dict):
        return 0

    pinned = 0
    for spec in data.values():
        harness = (spec or {}).get("harness")
        if isinstance(harness, dict) and "memory_mode" not in harness:
            harness["memory_mode"] = "volume"
            pinned += 1
    if pinned:
        tmp = agents_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(agents_path)  # atomic: never leave a half-written agent store
    return pinned
