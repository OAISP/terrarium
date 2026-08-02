// Shared tint formula + activity-status vocabulary for the agent-activity cards
// (ToolGroup / SubAgentCard / WorkflowCard / Question / Permission). One source of truth so
// the cards stop hand-rolling slightly-different color-mix strings and state→color maps.

/** A tone tinted toward the surface (card background). */
export const tint = (c: string, pct = 6) => `color-mix(in oklch, ${c} ${pct}%, var(--surface))`;
/** A tone tinted toward the border (card border). */
export const tintBorder = (c: string, pct = 30) => `color-mix(in oklch, ${c} ${pct}%, var(--border))`;

// "detached" = a background run (Workflow/Agent launched run_in_background) the session
// is no longer actively tracking — the turn went idle with no terminal signal. Never
// produced by activityState(); set explicitly by the cards.
export type ActivityState = "queued" | "running" | "done" | "error" | "detached";

const DONE = new Set(["done", "complete", "completed", "success"]);
const RUNNING = new Set(["progress", "start", "running", "retry", "active"]);

/** Normalize the many raw state strings (from workflow_progress, live flags, error flags)
 *  into the four-state vocabulary every card shares. */
export function activityState(raw?: string, opts?: { live?: boolean; isError?: boolean }): ActivityState {
  if (opts?.isError || raw === "error" || raw === "failed") return "error";
  if (raw && DONE.has(raw)) return "done";
  if (raw && RUNNING.has(raw)) return "running";
  if (opts?.live) return "running";
  if (raw) return "queued";
  return opts?.live === false ? "done" : "queued";
}

export const STATE_LABEL: Record<ActivityState, string> = {
  queued: "queued",
  running: "running",
  done: "done",
  error: "failed",
  detached: "detached",
};

/** The tone (CSS color) for each state — used by StateDot and card accents. */
export const STATE_TONE: Record<ActivityState, string> = {
  queued: "var(--muted)",
  running: "var(--accent)",
  done: "var(--c-agent)",
  error: "var(--c-error)",
  detached: "var(--muted)",
};
