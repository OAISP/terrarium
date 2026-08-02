"use client";

import * as React from "react";
import { Boxes, BarChart3, X, Download, FileText, Loader2 } from "lucide-react";
import { WorkflowCard } from "@/components/WorkflowCard";
import { SubAgentCard } from "@/components/SubAgentCard";
import { DetailSheet } from "@/components/agent-detail";
import { StateDot, type InspectTarget } from "@/components/ui/activity";
import { TokenBar, FieldLabel } from "@/components/ui/stat";
import { CountUp } from "@/components/ui/countup";
import { isWorkflowTask, taskState, workflowCounts, type TaskAgg, type TaskResult } from "@/lib/agentTasks";
import { downloadFile } from "@/lib/api";
import { type Artifact } from "@/lib/artifacts";
import { toast } from "@/components/ui/toast";
import { tint } from "@/lib/tint";
import { fmtNum } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Health, SessionSummary, TokenTotals } from "@/lib/types";

/** A nonce-stamped scroll-to target so repeated clicks on the same task re-trigger the flash. */
export type AgentHighlight = { id: string; n: number };

export type InspectorTab = "activity" | "stats";
type Filter = "all" | "running" | "done" | "failed";

/**
 * The Inspector rail — ONE fixed-width column holding sub-agent activity and run stats as
 * tabs, over a sticky cost/token footer. Two columns, never three: the transcript and this.
 */
export function InspectorRail({
  tab, onTab, onClose, tasks, resultByToolUse, busy, highlight,
  cost, budget, tokens, turns, tools, health, permission, isolation,
  sessionId, artifacts, context,
}: {
  tab: InspectorTab;
  onTab: (t: InspectorTab) => void;
  onClose: () => void;
  tasks: TaskAgg[];
  resultByToolUse: Record<string, TaskResult>;
  busy: boolean;
  highlight: AgentHighlight | null;
  cost: number; budget: number | null; tokens: TokenTotals;
  turns: number; tools: number; health: Health | null;
  permission: string; isolation: "isolated" | "shared";
  sessionId: string; artifacts: Artifact[]; context?: SessionSummary["context"];
}) {
  const [inspect, setInspect] = React.useState<InspectTarget | null>(null);
  // With no sub-agents there's nothing to inspect on Activity — collapse to a Stats-only rail
  // (a plain header, no empty tab to wander into).
  const hasAgents = tasks.length > 0;
  const activeTab: InspectorTab = hasAgents ? tab : "stats";

  return (
    <aside className="flex max-h-[45vh] w-full flex-none flex-col rounded-xl border border-border bg-panel shadow-soft lg:max-h-none lg:w-[340px]">
      <header className="flex items-center gap-2 border-b border-border p-2">
        {hasAgents ? (
          <Segmented<InspectorTab>
            className="flex-1"
            accent
            value={activeTab}
            onChange={onTab}
            options={[
              { value: "activity", label: "Activity", icon: Boxes, count: tasks.length },
              { value: "stats", label: "Stats", icon: BarChart3 },
            ]}
          />
        ) : (
          <div className="flex flex-1 items-center gap-2 px-1.5">
            <BarChart3 className="size-4 flex-none text-accent" />
            <span className="text-sm font-semibold">Stats</span>
          </div>
        )}
        <button type="button" onClick={onClose} aria-label="Close inspector"
          className="grid size-7 flex-none place-items-center rounded-md text-faint outline-none transition-colors hover:bg-surface-2 hover:text-text focus-visible:ring-2 focus-visible:ring-accent">
          <X className="size-4" />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-hidden">
        {activeTab === "activity" ? (
          <ActivityTab tasks={tasks} resultByToolUse={resultByToolUse} busy={busy} highlight={highlight} onInspect={setInspect} />
        ) : (
          <StatsTab tokens={tokens} turns={turns} tools={tools} health={health} permission={permission}
            isolation={isolation} sessionId={sessionId} artifacts={artifacts} context={context} />
        )}
      </div>

      <Footer cost={cost} budget={budget} tokens={tokens} />
      <DetailSheet target={inspect} onClose={() => setInspect(null)} />
    </aside>
  );
}

/** Activity tab — overview-first (rollup + one bar), a filter, then the per-task cards. Built
 *  to survive a 100-agent run: you read the numbers + the bar before any list. */
function ActivityTab({ tasks, resultByToolUse, busy, highlight, onInspect }: {
  tasks: TaskAgg[];
  resultByToolUse: Record<string, TaskResult>;
  busy: boolean;
  highlight: AgentHighlight | null;
  onInspect: (t: InspectTarget) => void;
}) {
  // Roll up the whole run ONCE: coarse task states + a single agents-done/total track (workflow
  // agents counted via workflowCounts, lone subagents counted as one agent each).
  const ov = React.useMemo(() => {
    let running = 0, done = 0, error = 0, aDone = 0, aTotal = 0;
    for (const t of tasks) {
      const st = taskState(t, resultByToolUse[t.toolUseId]);
      if (st === "running") running++; else if (st === "done") done++; else if (st === "error") error++;
      if (isWorkflowTask(t)) { const c = workflowCounts(t); aDone += c.done; aTotal += c.total; }
      else { aTotal += 1; if (st === "done") aDone += 1; }
    }
    return { running, done, error, aDone, aTotal };
  }, [tasks, resultByToolUse]);

  // Default to "what's live" under load (busy + a long list); else show everything.
  const [filter, setFilter] = React.useState<Filter>(() => (busy && tasks.length > 12 ? "running" : "all"));

  const filtered = React.useMemo(() => {
    if (filter === "all") return tasks;
    const want = filter === "failed" ? "error" : filter;
    return tasks.filter((t) => taskState(t, resultByToolUse[t.toolUseId]) === want);
  }, [tasks, filter, resultByToolUse]);

  const cardRefs = React.useRef<Record<string, HTMLDivElement | null>>({});
  const [flashId, setFlashId] = React.useState<string | null>(null);

  // Scroll the highlighted card into view + flash it. If the filter hides it, switch to All
  // first (filter beats scroll) — the effect re-runs once `filtered` updates and then scrolls.
  React.useEffect(() => {
    if (!highlight) return;
    const inView = filtered.some((t) => t.toolUseId === highlight.id);
    if (!inView) {
      // Deliberate two-pass: widen the filter now, and the effect re-runs on the updated
      // `filtered` to do the scroll (filter must win before scrolling to a hidden card).
      // eslint-disable-next-line react-hooks/set-state-in-effect
      if (tasks.some((t) => t.toolUseId === highlight.id)) setFilter("all");
      return;
    }
    const el = cardRefs.current[highlight.id];
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setFlashId(highlight.id);
    const tmr = setTimeout(() => setFlashId((cur) => (cur === highlight.id ? null : cur)), 1400);
    return () => clearTimeout(tmr);
  }, [highlight, filtered, tasks]);

  const pct = ov.aTotal > 0 ? Math.round((ov.aDone / ov.aTotal) * 100) : 0;

  return (
    <div className="flex h-full flex-col">
      {/* Sticky overview: a few numbers + one bar — what a 100-agent run should READ AS. */}
      <div className="border-b border-border px-3.5 py-3">
        <div className="flex items-center gap-3 text-xs">
          <OvCount state="running" n={ov.running} label="running" />
          <OvCount state="done" n={ov.done} label="done" />
          {ov.error > 0 && <OvCount state="error" n={ov.error} label="failed" />}
          <span className="flex-1" />
          <span className="font-mono text-2xs tabular-nums text-faint">{ov.aDone}/{ov.aTotal} agents</span>
        </div>
        <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
          <div className="h-full rounded-full transition-[width]" style={{ width: `${pct}%`, background: "var(--accent)" }} />
        </div>
        <Segmented<Filter>
          className="mt-2.5"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: "All" },
            { value: "running", label: "Running" },
            { value: "done", label: "Done" },
            { value: "failed", label: "Failed" },
          ]}
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        {tasks.length === 0 ? (
          <p className="px-1 py-6 text-center text-xs text-faint">No sub-agents yet.</p>
        ) : filtered.length === 0 ? (
          <p className="px-1 py-6 text-center text-xs text-faint">Nothing {filter === "failed" ? "failed" : filter}.</p>
        ) : (
          filtered.map((task) => {
            const result = resultByToolUse[task.toolUseId];
            const flash = flashId === task.toolUseId;
            const running = taskState(task, result) === "running";
            return (
              <div key={task.toolUseId} ref={(el) => { cardRefs.current[task.toolUseId] = el; }}
                className="scroll-mt-2 rounded-xl transition-shadow"
                style={flash ? { boxShadow: "0 0 0 2px var(--accent)" } : undefined}>
                {isWorkflowTask(task) ? (
                  <WorkflowCard wf={task} output={result?.content} isError={result?.isError} detached={!busy} expanded={running} onInspect={onInspect} />
                ) : (
                  <SubAgentCard task={task} output={result?.content}
                    isError={result?.isError || task.isError}
                    live={!result && !task.done} detached={!busy} />
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

/** Stats tab — token composition, turn/tool counts, artifacts, sandbox. Session cost and the
 *  token total live in the always-on footer, so they are not repeated here. */
/** Files the agent wrote, as saves. Derived from the transcript (see lib/artifacts), so it
 *  still works after the session ends — but the BYTES come from the live workspace, which a
 *  finished session no longer has. That is why a failed download reports the orchestrator's
 *  reason rather than silently doing nothing. */
function ArtifactList({ sessionId, artifacts }: { sessionId: string; artifacts: Artifact[] }) {
  const [busy, setBusy] = React.useState<string | null>(null);

  async function save(name: string) {
    setBusy(name);
    try { await downloadFile(sessionId, name); }
    catch (e) { toast.error(`Couldn't download ${name}: ${e instanceof Error ? e.message : String(e)}`); }
    finally { setBusy(null); }
  }

  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <FieldLabel>Artifacts</FieldLabel>
        <span className="font-mono text-xs text-faint">{artifacts.length}</span>
      </div>
      <ul className="flex flex-col gap-1">
        {artifacts.map((a) => (
          <li key={a.name}>
            <button type="button" onClick={() => save(a.name)} disabled={busy != null}
              aria-label={`Download ${a.name}`}
              className="group flex w-full items-center gap-2 rounded-lg border border-border bg-surface-2 px-2.5 py-1.5 text-left text-xs outline-none transition-colors hover:border-accent hover:bg-surface-3 focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50">
              <FileText className="size-3.5 flex-none text-faint" />
              <span className="min-w-0 flex-1 truncate font-mono text-text">{a.name}</span>
              {busy === a.name
                ? <Loader2 className="size-3.5 flex-none animate-spin text-accent" />
                : <Download className="size-3.5 flex-none text-faint transition-colors group-hover:text-accent" />}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Context-window usage for the live session.
 *
 *  The orchestrator has always folded this into every session summary — the field is even
 *  commented "for supervisors" — and the console dropped it. It answers the question the
 *  transcript can't: is this run about to auto-compact, which is when a long agent silently
 *  loses the earlier half of its own reasoning. The compact threshold is marked, because
 *  "83%" only means something relative to where compaction actually fires. */
function ContextGauge({ ctx }: { ctx: NonNullable<SessionSummary["context"]> }) {
  const pct = Math.max(0, Math.min(100, Number(ctx.percentage) || 0));
  const threshold = ctx.max_tokens > 0 ? (ctx.compact_threshold / ctx.max_tokens) * 100 : 0;
  // Amber approaching the threshold, error past it — the point at which history starts
  // being dropped is a state change, not a gradient.
  const tone = pct >= threshold && threshold > 0 ? "var(--c-error)"
    : pct >= threshold * 0.8 ? "var(--c-result)" : "var(--accent)";
  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <FieldLabel>Context window</FieldLabel>
        <span className="font-mono text-xs font-semibold" style={{ color: tone }}>{pct}%</span>
      </div>
      <div className="relative h-[9px] overflow-hidden rounded-full bg-surface-2">
        <div className="h-full rounded-full transition-[width]" style={{ width: `${pct}%`, background: tone }} />
        {threshold > 0 && threshold < 100 && (
          <span className="absolute top-0 h-full w-px bg-border-2"
            style={{ left: `${threshold}%` }} aria-hidden />
        )}
      </div>
      <div className="mt-1.5 flex items-center justify-between text-2xs text-faint">
        <span className="font-mono tabular-nums">{fmtNum(ctx.total_tokens)} / {fmtNum(ctx.max_tokens)}</span>
        <span>{ctx.auto_compact ? `auto-compacts at ${Math.round(threshold)}%` : "auto-compact off"}</span>
      </div>
    </section>
  );
}

function StatsTab({ tokens, turns, tools, health, permission, isolation, sessionId, artifacts, context }: {
  tokens: TokenTotals; turns: number; tools: number;
  health: Health | null; permission: string; isolation: "isolated" | "shared";
  sessionId: string; artifacts: Artifact[]; context?: SessionSummary["context"];
}) {
  const total = tokens.input + tokens.output + tokens.cacheRead + tokens.cacheCreate + tokens.subagent;
  return (
    <div className="flex h-full flex-col gap-[18px] overflow-y-auto p-5">
      <section>
        <div className="mb-2 flex items-center justify-between">
          <FieldLabel>Tokens</FieldLabel>
          <span className="font-mono text-xs font-semibold">{fmtNum(total)}</span>
        </div>
        <TokenBar tokens={tokens} format={fmtNum} barClassName="h-[9px]" />
      </section>

      {context && <ContextGauge ctx={context} />}

      <section className="grid grid-cols-2 gap-2.5">
        <Tile value={turns} label="user turns" />
        <Tile value={tools} label="tool calls" />
      </section>

      {artifacts.length > 0 && <ArtifactList sessionId={sessionId} artifacts={artifacts} />}

      <section className="mt-auto rounded-xl border p-3.5" style={{ background: "color-mix(in oklch, var(--accent) 8%, var(--surface-2))", borderColor: "color-mix(in oklch, var(--accent) 22%, var(--border))" }}>
        <FieldLabel>Sandbox</FieldLabel>
        <div className="mt-2 flex flex-col gap-1.5 text-xs">
          <Row k="Runner" v={<span className="font-mono uppercase text-accent">{health?.runner ?? "—"}</span>} />
          <Row k="Permission" v={<span className="font-mono text-text">{permission}</span>} />
          <Row k="Isolation" v={<span className="text-agent">{isolation === "isolated" ? "Isolated volume" : "Shared volume"}</span>} />
        </div>
      </section>
    </div>
  );
}

/** Sticky bottom metrics — ambient spend never disappears, whichever tab is active. */
function Footer({ cost, budget, tokens }: { cost: number; budget: number | null; tokens: TokenTotals }) {
  const hasBudget = budget != null && budget > 0;
  const total = tokens.input + tokens.output + tokens.cacheRead + tokens.cacheCreate + tokens.subagent;
  const pct = hasBudget ? Math.min(1, cost / (budget as number)) : 0;
  const gauge = pct > 0.85 ? "var(--c-error)" : pct > 0.6 ? "var(--c-tool)" : "var(--accent)";
  return (
    <div className="border-t border-border px-3.5 py-2.5">
      <div className="flex items-baseline gap-1.5">
        <span className="font-mono text-sm font-bold tracking-tight">$<CountUp to={cost} decimals={cost < 1 ? 4 : 2} /></span>
        {hasBudget ? <span className="text-2xs text-faint">/ ${(budget as number).toFixed(2)}</span> : <span className="text-2xs text-faint">uncapped</span>}
        <span className="flex-1" />
        <span className="font-mono text-2xs tabular-nums text-muted">{fmtNum(total)} tok</span>
      </div>
      {hasBudget && (
        <div className="mt-1.5 h-[5px] overflow-hidden rounded-md bg-surface-3">
          <div className="h-full rounded-md transition-[width,background] duration-500" style={{ width: `${pct * 100}%`, background: gauge }} />
        </div>
      )}
    </div>
  );
}

/** A segmented control. `accent` paints the active segment with the one accent (tabs); otherwise
 *  the active segment is a quiet raised surface (filters) — one accent at a time. */
function Segmented<T extends string>({ options, value, onChange, accent, className }: {
  options: { value: T; label: string; icon?: React.ElementType; count?: number }[];
  value: T;
  onChange: (v: T) => void;
  accent?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex gap-0.5 rounded-lg border border-border bg-surface-2 p-0.5", className)} role="tablist">
      {options.map((o) => {
        const active = o.value === value;
        const Icon = o.icon;
        return (
          <button key={o.value} type="button" role="tab" aria-selected={active} onClick={() => onChange(o.value)}
            className={cn(
              "flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent",
              active ? (accent ? "text-accent" : "bg-surface text-text shadow-soft") : "text-muted hover:text-text",
            )}
            style={active && accent ? { background: tint("var(--accent)", 14) } : undefined}>
            {Icon && <Icon className="size-3.5 flex-none" />}
            {o.label}
            {o.count != null && (
              <span className={cn("rounded px-1 text-2xs tabular-nums", active ? "bg-surface-3" : "bg-surface-3 text-muted")}>{o.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

const OvCount = ({ state, n, label }: { state: "running" | "done" | "error"; n: number; label: string }) => (
  <span className="inline-flex items-center gap-1.5">
    <StateDot state={state} size={13} className="flex-none" />
    <span className="font-mono font-semibold tabular-nums">{n}</span>
    <span className="text-faint">{label}</span>
  </span>
);
const Tile = ({ value, label }: { value: number; label: string }) => (
  <div className="rounded-lg border border-border bg-surface-2 p-3">
    <div className="font-mono text-xl font-bold"><CountUp to={value} /></div>
    <div className="mt-0.5 text-xs text-faint">{label}</div>
  </div>
);
const Row = ({ k, v }: { k: string; v: React.ReactNode }) => (
  <div className="flex justify-between"><span className="text-muted">{k}</span>{v}</div>
);
