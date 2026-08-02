"use client";

import * as React from "react";
import { Boxes, ChevronDown, ChevronRight } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { StateDot, TokenMeter, ActivityCard, type InspectTarget } from "@/components/ui/activity";
import { Chip } from "@/components/ui/chip";
import { FieldLabel } from "@/components/ui/stat";
import { Markdown } from "@/components/ui/markdown";
import { activityState, tintBorder, type ActivityState } from "@/lib/tint";
import { buildPhaseGroups, type PhaseGroup } from "@/lib/agentTasks";
import { fmtNum } from "@/lib/format";

export type WfAgent = {
  label: string;
  index?: number;
  phaseTitle?: string;
  model?: string;
  state?: string;
  tokens?: number;
  toolCalls?: number;
  lastToolName?: string;
  lastToolSummary?: string;
  description?: string;
  durationMs?: number;
  output?: string;
  toolHistory?: { tool: string; summary?: string; last?: boolean }[];
};
export type Workflow = {
  taskId: string;
  name?: string;
  description?: string;
  phases: { index: number; title: string }[];
  agents: WfAgent[];
  done?: boolean;
};

/** Overall progress: a thin accent-filled track over completed/total agents + a one-line
 *  roll-up of the work done (sum of tokens / tool calls across every subagent). */
function OverallBar({ done, total, tokens, toolCalls }: { done: number; total: number; tokens: number; toolCalls: number }) {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  return (
    <div className="space-y-1.5">
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
        <div className="h-full rounded-full transition-[width]" style={{ width: `${pct}%`, background: "var(--accent)" }} />
      </div>
      <div className="flex items-center gap-2 font-mono text-2xs tabular-nums text-faint">
        <span>{done}/{total} agents done</span>
        {tokens > 0 && <span>· {fmtNum(tokens)} tok</span>}
        {toolCalls > 0 && <span>· {toolCalls} {toolCalls === 1 ? "tool" : "tools"}</span>}
      </div>
    </div>
  );
}

/** One phase's batch of subagents, collapsible. Open by default only while the phase is
 *  running, so a long multi-phase run stays scannable (done/queued phases collapse). */
function PhaseSection({ group, wfName, onInspect }: { group: PhaseGroup; wfName?: string; onInspect?: (t: InspectTarget) => void }) {
  const [open, setOpen] = React.useState(group.state === "running");
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="overflow-hidden rounded-lg border"
      style={{ borderColor: tintBorder("var(--accent)", 16) }}>
      <CollapsibleTrigger className="flex w-full items-center gap-2.5 px-2.5 py-2 text-left text-xs outline-none transition-colors hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-accent">
        <StateDot state={group.state} size={13} className="flex-none" />
        <span className="min-w-0 flex-1 truncate font-medium" title={group.title}>{group.title}</span>
        <Chip className="flex-none">{group.done}/{group.total}</Chip>
        <ChevronDown size={13} className="flex-none text-faint transition-transform" style={{ transform: open ? "rotate(180deg)" : "" }} />
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-1 border-t px-2 py-2" style={{ borderColor: tintBorder("var(--accent)", 16) }}>
        {group.agents.length > 0
          ? group.agents.map((a) => <AgentRow key={a.label} a={a} wfName={wfName} onInspect={onInspect} />)
          : <p className="px-1 py-1 text-2xs text-faint">No agents reported for this phase yet.</p>}
      </CollapsibleContent>
    </Collapsible>
  );
}

/** A single subagent line in the workflow tree — clickable to inspect in the detail drawer. */
function AgentRow({ a, wfName, onInspect }: { a: WfAgent; wfName?: string; onInspect?: (t: InspectTarget) => void }) {
  const st = activityState(a.state);
  const activity = a.lastToolSummary || a.lastToolName;
  const inspect = onInspect
    ? () => onInspect({
        title: a.label, subtitle: wfName, badge: a.model?.replace("claude-", ""), state: st,
        tokens: a.tokens, tools: a.toolCalls, activity,
        phase: a.phaseTitle, task: a.description, model: a.model, durationMs: a.durationMs, output: a.output,
        error: st === "error" ? (a.lastToolSummary ?? a.state) : undefined,
        timeline: a.toolHistory ?? (activity ? [{ tool: a.lastToolName ?? "tool", summary: a.lastToolSummary, last: true }] : undefined),
      })
    : undefined;
  return (
    <button type="button" onClick={inspect} disabled={!inspect}
      className="flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left text-xs outline-none transition-colors enabled:hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default"
      style={{ background: "color-mix(in oklch, var(--surface-2) 60%, transparent)" }}>
      <StateDot state={st} size={14} className="flex-none" />
      <span className="w-40 flex-none truncate font-medium" title={a.label}>{a.label}</span>
      {a.model && <span className="hidden flex-none font-mono text-2xs text-faint sm:inline">{a.model.replace("claude-", "")}</span>}
      <span className="min-w-0 flex-1 truncate text-faint" title={activity}>{activity ?? a.state ?? ""}</span>
      <TokenMeter tokens={a.tokens} tools={a.toolCalls} className="flex-none" />
      {inspect && <ChevronRight size={13} className="flex-none text-faint" />}
    </button>
  );
}

/**
 * A multi-agent Workflow run (the Claude Code Workflow tool). Collapsed by default to a
 * one-line summary (N/total agents + overall state) so it doesn't dominate the thread;
 * expand for the phase stepper, the per-subagent tree, and the workflow's final output.
 */
export function WorkflowCard({ wf, output, isError, detached, expanded, onInspect }: { wf: Workflow; output?: string; isError?: boolean; detached?: boolean; expanded?: boolean; onInspect?: (t: InspectTarget) => void }) {
  const [open, setOpen] = React.useState(!!expanded);
  const agents = wf.agents;
  const total = agents.length;
  const done = agents.filter((a) => activityState(a.state) === "done").length;
  const running = agents.filter((a) => activityState(a.state) === "running").length;

  // Group subagents into per-phase batches (shared with the compact workflow chip in
  // agentTasks → one source of truth for the phase roll-up).
  const phaseGroups = React.useMemo(() => buildPhaseGroups(wf.phases, agents), [wf.phases, agents]);
  const donePhases = phaseGroups.filter((g) => g.state === "done").length;
  const sumTokens = agents.reduce((s, a) => s + (a.tokens ?? 0), 0);
  const sumTools = agents.reduce((s, a) => s + (a.toolCalls ?? 0), 0);

  // A background launch leaves `output` = the launch ack (Task ID), NOT the result — so it
  // must NOT count as "done". Completion is the terminal task signal (wf.done) or a real
  // (non-launch) output or all agents finishing.
  const launched = output?.startsWith("Workflow launched in background") ?? false;
  const isDone = !!wf.done || (output != null && !launched) || (total > 0 && done === total);
  // Detached = the session went idle but this background run never reported any agent
  // progress (only the launch ack) and never signalled done — it's stranded, not running.
  // Scoped to total===0 so a genuinely-progressing background run still shows "running".
  const stalled = !!detached && !isDone && !isError && total === 0;
  const overall: ActivityState = isError ? "error" : isDone ? "done" : stalled ? "detached" : running > 0 ? "running" : "queued";
  const tone = isError ? "var(--c-error)" : stalled ? "var(--muted)" : "var(--accent)";

  return (
    <ActivityCard tone={tone} className="p-0">
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger className="flex w-full items-center gap-2.5 px-4 py-3 text-left outline-none transition-colors hover:bg-[color-mix(in_oklch,var(--accent)_7%,transparent)] focus-visible:ring-2 focus-visible:ring-accent">
          <Boxes className="size-4 flex-none" style={{ color: "var(--accent)" }} />
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="flex items-center gap-2">
              <span className="text-sm font-semibold">{wf.name || "Workflow"}</span>
              <Chip>{donePhases}/{phaseGroups.length} phases · {done}/{total} agents</Chip>
            </span>
            {wf.description && <span className="truncate text-xs text-muted">{wf.description}</span>}
          </span>
          <StateDot state={overall} size={15} className="flex-none" />
          <ChevronDown size={14} className="flex-none text-faint transition-transform" style={{ transform: open ? "rotate(180deg)" : "" }} />
        </CollapsibleTrigger>
        <CollapsibleContent className="border-t px-4 py-3" style={{ borderColor: "color-mix(in oklch, var(--accent) 18%, var(--border))" }}>
          <div className="mb-3"><OverallBar done={done} total={total} tokens={sumTokens} toolCalls={sumTools} /></div>
          <div className="space-y-1.5">
            {phaseGroups.map((g) => <PhaseSection key={g.key} group={g} wfName={wf.name} onInspect={onInspect} />)}
          </div>
          {output != null && (
            // A background workflow's tool_result is just the launch ack (Task ID, transcript
            // dir) — the real synthesis arrives later in the thread. Label honestly, and flag
            // when the run detached (session idle, no further progress) so it doesn't read as live.
            <div className="mt-3 border-t pt-3" style={{ borderColor: "color-mix(in oklch, var(--accent) 12%, var(--border))" }}>
              <FieldLabel className="mb-1.5">
                {launched ? (stalled ? "Launched in background, then detached (session idle)" : "Launched (running in background)") : "Result"}
              </FieldLabel>
              <Markdown>{output}</Markdown>
            </div>
          )}
        </CollapsibleContent>
      </Collapsible>
    </ActivityCard>
  );
}
