import type { Harness, ModelInfo } from "./types";

// The model list is NOT here. It comes from the orchestrator (`useModels()` →
// terracore/models.py), because a hardcoded copy is what left the live model switcher
// offering three aliases to a session already running claude-opus-5.

/** Display name for a model id, from the catalog when known. Falls back to trimming the
 *  vendor prefix + date suffix so a model the orchestrator doesn't list (a pinned id from
 *  an older agent, say) still renders as something readable rather than blank. */
export function modelLabel(model: string | null | undefined, catalog: ModelInfo[] = []): string {
  if (!model) return "—";
  return catalog.find((m) => m.id === model)?.label
    ?? model.replace(/^claude-/, "").replace(/-\d{6,}$/, "");
}

export function thinkingLabel(t: Harness["thinking"]): string {
  if (!t) return "no thinking";
  if (t.type === "adaptive") return "adaptive thinking";
  if (t.type === "disabled") return "thinking off";
  return `${Math.round(t.budget_tokens / 1000)}k thinking`;
}

export function toolsLabel(h: Harness): string {
  if (h.allowed_tools === null) return "all tools";
  if (h.allowed_tools.length === 0) return "no tools";
  return `${h.allowed_tools.length} tool${h.allowed_tools.length === 1 ? "" : "s"}`;
}

export function personaLabel(mode: string | null | undefined): string {
  switch (mode) {
    case "claude_code": return "Claude Code";
    case "assistant": return "Assistant";
    case "minimal": return "Minimal";
    case "custom": return "Custom prompt";
    default: return mode ?? "—";
  }
}
