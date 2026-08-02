"""The model catalog — one source of truth for every model Terrarium offers.

This existed in four places that drifted independently: the console's agent form (concrete
ids), ``web/lib/harness.ts`` (three bare aliases, used by the new-session picker AND the live
model switcher), this module's neighbour ``templates.py`` (concrete ids, a generation behind),
and ``Config.default_model``. The visible symptom: a session running ``claude-opus-5`` opened
its switcher and was offered only ``sonnet | opus | haiku``, so switching silently downgraded
it — and adding Opus 5 to the picker didn't add it anywhere else.

So the catalog lives here, ships over ``GET /v1/models``, and the console renders whatever the
orchestrator says it supports. Adding a model is one entry in this file.

Aliases (``sonnet``/``opus``/``haiku``) are resolved by the Claude CLI itself, not by us — they
track the current generation, which is exactly what a long-lived agent config usually wants.
Concrete ids pin a generation, which is what a reproducible one wants. Both are offered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    # An alias resolves to "whatever is current" at launch; a concrete id pins a generation.
    alias: bool = False
    # Surfaced in the console so an operator can see the trade-off without leaving the form.
    note: str = ""


# Newest first — the console renders this order, so it is the recommendation order.
MODELS: tuple[Model, ...] = (
    Model("claude-opus-5", "Opus 5", note="Most capable."),
    Model("claude-sonnet-5", "Sonnet 5", note="Balanced capability and cost."),
    Model("claude-opus-4-8", "Opus 4.8"),
    Model("claude-opus-4-7", "Opus 4.7"),
    Model("claude-sonnet-4-6", "Sonnet 4.6"),
    Model("claude-haiku-4-5", "Haiku 4.5", note="Fastest and cheapest."),
    Model("claude-fable-5", "Fable 5", note="Premium tier."),
    # Aliases last: they're the right default for a durable agent config, but a reader
    # scanning for a specific model wants the concrete ids first.
    Model("opus", "Opus (latest)", alias=True, note="Tracks the current Opus generation."),
    Model("sonnet", "Sonnet (latest)", alias=True, note="Tracks the current Sonnet generation."),
    Model("haiku", "Haiku (latest)", alias=True, note="Tracks the current Haiku generation."),
)

# What a new agent/session gets when nothing is specified. An alias on purpose: an agent
# created today should not be pinned to a generation that ages out. Config.default_model
# (TERRA_MODEL) can override it per deployment.
DEFAULT_MODEL = "sonnet"

# Concrete ids for the templates below and anywhere else that wants to pin a generation.
OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"

_BY_ID = {m.id: m for m in MODELS}


def known(model_id: str) -> bool:
    return model_id in _BY_ID


def label(model_id: str) -> str:
    """Human label for a model id, falling back to the id itself — an operator may pin a
    model we don't list (a new release, or a deployment-specific alias), and that must
    display rather than render blank."""
    m = _BY_ID.get(model_id)
    return m.label if m else model_id


def catalog() -> list[dict[str, Any]]:
    """Wire form for ``GET /v1/models``."""
    return [{"id": m.id, "label": m.label, "alias": m.alias, "note": m.note} for m in MODELS]
