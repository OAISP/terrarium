// Single source of truth for sub-agent (`Agent`/`Task` tool) + multi-agent (`Workflow` tool)
// aggregation. Both the inline references in the transcript (EventTimeline) and the dedicated
// Agents side panel (SessionView) consume this — so the parsing lives in exactly one place and
// the two surfaces can't drift apart.

import type { LogEvent } from "@/lib/types";
import type { Workflow, WfAgent } from "@/components/WorkflowCard";
import { activityState, type ActivityState } from "@/lib/tint";

// Keyed by tool_use_id — a STANDALONE Agent/Workflow has a top-level tool_use; a workflow's
// internal children do not, so keying this way means children never double-render (they live in
// the workflow's own tree). Each event reports only what changed → merge.
export type TaskAgg = Workflow & {
  toolUseId: string;
  taskType?: string;
  subagentType?: string;
  model?: string;
  tokens?: number;
  toolCalls?: number;
  lastTool?: string;
  isError?: boolean;
  durationMs?: number;
};

export type TaskResult = { content: string; isError: boolean };

/** Coerce any event field to a display string (raw string passthrough; objects → pretty JSON). */
export const str = (v: unknown) => (v == null ? "" : typeof v === "string" ? v : JSON.stringify(v, null, 2));

/** A task renders as a WorkflowCard (vs a SubAgentCard) when it carries phase/agent structure. */
export function isWorkflowTask(task: TaskAgg): boolean {
  return task.taskType === "local_workflow" || task.agents.length > 0 || task.phases.length > 0;
}

/**
 * Aggregate every `task_*`/`tool_use`/`tool_result` event into the per-task summaries that drive
 * the Agent/Workflow cards. Returns the raw `tasks` map, the card-worthy tasks in occurrence
 * order, the set of tool_use ids that became cards, and each task's final tool_result.
 */
export function aggregateAgentTasks(events: LogEvent[]): {
  tasks: Record<string, TaskAgg>;
  taskList: TaskAgg[];
  cardIds: Set<string>;
  resultByToolUse: Record<string, TaskResult>;
} {
  const tasks: Record<string, TaskAgg> = {};
  const tidToTuid: Record<string, string> = {}; // task_id → tool_use_id (from task_started)
  const resultByToolUse: Record<string, TaskResult> = {};
  const toolUseName: Record<string, string> = {};
  const order: string[] = []; // tool_use occurrence order

  for (const ev of events) {
    if (ev.type === "tool_use" && typeof ev.id === "string") {
      toolUseName[ev.id] = String(ev.name ?? "");
      order.push(ev.id);
    } else if (ev.type === "tool_result" && typeof ev.tool_use_id === "string") {
      resultByToolUse[ev.tool_use_id] = { content: str(ev.content), isError: ev.is_error === true };
    } else if (ev.type === "system" && typeof ev.subtype === "string" && ev.subtype.startsWith("task_")) {
      const d = (ev.data as Record<string, unknown>) ?? {};
      const tid = typeof d.task_id === "string" ? d.task_id : "";
      // task_started carries both ids; task_updated carries only task_id → resolve via the map.
      if (ev.subtype === "task_started" && tid && typeof d.tool_use_id === "string") tidToTuid[tid] = d.tool_use_id;
      const tuid = typeof d.tool_use_id === "string" ? d.tool_use_id : tid ? tidToTuid[tid] : "";
      if (!tuid) continue;
      const task = (tasks[tuid] ??= { toolUseId: tuid, taskId: String(d.task_id ?? tuid), phases: [], agents: [] });
      if (typeof d.task_type === "string") task.taskType = d.task_type;
      if (typeof d.workflow_name === "string") task.name = d.workflow_name;
      if (typeof d.description === "string") task.description = d.description;
      if (typeof d.subagent_type === "string") task.subagentType = d.subagent_type;
      if (typeof d.model === "string") task.model = d.model;
      // Completion: there is no task_completed in practice — done arrives via task_notification
      // (status) or task_updated (patch.status). Treat all three as the done signal.
      if (ev.subtype === "task_completed") task.done = true;
      if (ev.subtype === "task_notification") {
        const st = String(d.status ?? "");
        if (st === "completed" || st === "failed" || st === "error") task.done = true;
        if (st === "failed" || st === "error") task.isError = true;
      }
      if (ev.subtype === "task_updated") {
        const st = String((d.patch as Record<string, unknown> | undefined)?.status ?? "");
        if (st === "completed") task.done = true;
        if (st === "failed" || st === "error") { task.done = true; task.isError = true; }
      }
      const usage = (d.usage as Record<string, number>) ?? {};
      if (typeof usage.total_tokens === "number") task.tokens = usage.total_tokens;
      if (typeof usage.tool_uses === "number") task.toolCalls = usage.tool_uses;
      if (typeof usage.duration_ms === "number") task.durationMs = usage.duration_ms;
      if (typeof d.last_tool_name === "string") task.lastTool = d.last_tool_name;
      const byLabel = new Map(task.agents.map((a) => [a.label, a]));
      for (const item of (d.workflow_progress as Record<string, unknown>[]) ?? []) {
        if (item.type === "workflow_phase" && typeof item.index === "number") {
          if (!task.phases.some((p) => p.index === item.index)) task.phases.push({ index: item.index as number, title: String(item.title ?? "") });
        } else if (item.type === "workflow_agent" && typeof item.label === "string") {
          const prev = byLabel.get(item.label) ?? { label: item.label };
          const u = (item.usage as Record<string, number>) ?? {};
          byLabel.set(item.label, {
            ...prev, label: item.label, index: (item.index as number) ?? prev.index,
            phaseTitle: (item.phaseTitle as string) ?? prev.phaseTitle, model: (item.model as string) ?? prev.model,
            state: (item.state as string) ?? prev.state, tokens: (item.tokens as number) ?? u.total_tokens ?? prev.tokens ?? 0,
            toolCalls: (item.toolCalls as number) ?? prev.toolCalls ?? 0,
            lastToolName: (item.lastToolName as string) ?? prev.lastToolName, lastToolSummary: (item.lastToolSummary as string) ?? prev.lastToolSummary,
            // The CLI emits promptPreview/resultPreview/durationMs (not description/output/usage.duration_ms).
            description: (item.promptPreview as string) ?? (item.description as string) ?? prev.description,
            durationMs: (item.durationMs as number) ?? (u.duration_ms as number) ?? prev.durationMs,
            output: (item.resultPreview as string) ?? (item.output as string) ?? prev.output,
            toolHistory: (item.toolHistory as WfAgent["toolHistory"]) ?? prev.toolHistory,
          });
        }
      }
      task.agents = [...byLabel.values()].sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
      task.phases.sort((a, b) => a.index - b.index);
    }
  }
  // A tool_use becomes a card iff it's an Agent/Workflow/Task call WITH aggregated task data.
  const cardIds = new Set(Object.keys(tasks).filter((id) => ["Agent", "Workflow", "Task"].includes(toolUseName[id] ?? "")));
  const taskList = order.filter((id) => cardIds.has(id) && tasks[id]).map((id) => tasks[id]);
  return { tasks, taskList, cardIds, resultByToolUse };
}

/** Coarse overall state for a task's compact reference dot (error → done → running). */
export function taskState(task: TaskAgg, result?: TaskResult): ActivityState {
  if (task.isError || result?.isError) return "error";
  if (task.done || result != null) return "done";
  return "running";
}

/** A phase's batch of subagents + its roll-up state — the unit both the WorkflowCard's phase
 *  stepper and the compact workflow chip are built from. */
export type PhaseGroup = {
  key: string;
  index: number;
  title: string;
  agents: WfAgent[];
  done: number;
  running: number;
  total: number;
  state: ActivityState;
};

/**
 * Group a workflow's subagents into per-phase batches: seed from declared phases (so empty
 * phases still show), bucket agents by phaseTitle, and fall back to ONE synthetic "Agents"
 * group for any agent without a phase (or when no phases were declared) — reproducing the old
 * flat list. Single source of truth for WorkflowCard (rendering) and workflowCounts (the chip).
 */
export function buildPhaseGroups(phases: Workflow["phases"], agents: WfAgent[]): PhaseGroup[] {
  const byTitle = new Map<string, PhaseGroup>();
  const mk = (key: string, index: number, title: string): PhaseGroup =>
    ({ key, index, title, agents: [], done: 0, running: 0, total: 0, state: "queued" });
  for (const p of phases) byTitle.set(p.title, mk(p.title, p.index, p.title));
  let synthetic: PhaseGroup | null = null;
  const syn = () => (synthetic ??= mk("__agents__", phases.length, "Agents"));
  for (const a of agents) {
    let g: PhaseGroup;
    if (!a.phaseTitle || phases.length === 0) g = syn();
    else g = byTitle.get(a.phaseTitle) ?? (byTitle.set(a.phaseTitle, mk(a.phaseTitle, byTitle.size, a.phaseTitle)), byTitle.get(a.phaseTitle)!);
    g.agents.push(a);
  }
  const groups = [...byTitle.values()];
  if (synthetic) groups.push(synthetic);
  for (const g of groups) {
    g.agents.sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
    g.total = g.agents.length;
    g.done = g.agents.filter((a) => activityState(a.state) === "done").length;
    g.running = g.agents.filter((a) => activityState(a.state) === "running").length;
    g.state = g.agents.some((a) => activityState(a.state) === "error") ? "error"
      : g.total > 0 && g.done === g.total ? "done"
      : g.running > 0 || g.done > 0 ? "running"
      : "queued";
  }
  return groups.sort((a, b) => a.index - b.index);
}

/** Phase/agent roll-up for the compact workflow chip — derived from the same phase grouping
 *  the WorkflowCard renders, so the two surfaces can't drift. */
export function workflowCounts(task: TaskAgg): { done: number; total: number; donePhases: number; totalPhases: number } {
  const agents = task.agents;
  const groups = buildPhaseGroups(task.phases, agents);
  return {
    done: agents.filter((a) => activityState(a.state) === "done").length,
    total: agents.length,
    donePhases: groups.filter((g) => g.state === "done").length,
    totalPhases: groups.length,
  };
}
