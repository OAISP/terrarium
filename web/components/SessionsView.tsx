"use client";

import { useMemo, useState } from "react";
import { Plus, Search, Trash2, Activity, Bot, AlertTriangle } from "lucide-react";
import type { AgentSpec, SessionSummary } from "@/lib/types";
import { createSession, deleteSession, sendMessage } from "@/lib/api";
import { fmtAge, fmtCost, fmtNum, fmtTime, isLive } from "@/lib/format";
import { useModels } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input, Textarea, Field } from "@/components/ui/input";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { ErrorBox, EmptyState, ListSkeleton, ResourceState } from "@/components/ui/feedback";
import { PageContainer } from "@/components/ui/page";
import { toast } from "@/components/ui/toast";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { StatusPill } from "./StatusPill";

const FILTERS = [
  { id: "all", label: "All", test: () => true },
  { id: "live", label: "Live", test: (s: SessionSummary) => isLive(s.status) },
  { id: "ended", label: "Ended", test: (s: SessionSummary) => !isLive(s.status) },
  // Sessions accumulate forever, so "what ran recently" needs to be one click, not a scroll
  // through every run this agent has ever made.
  { id: "today", label: "Today", test: (s: SessionSummary) => isToday(s.created_ts) },
] as const;

function isToday(ts: string | null | undefined): boolean {
  if (!ts) return false;
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return false;
  return d.toDateString() === new Date().toDateString();
}

/** One metric cell. Carries its own unit when there's no header row to label it. */
function Metric({ value, unit, showUnit, wide }: { value: React.ReactNode; unit: string; showUnit: boolean; wide?: boolean }) {
  return (
    <span className={cn("text-right text-muted", showUnit ? (wide ? "w-24" : "w-20") : wide ? "w-16" : "w-14")}>
      {value}
      {showUnit && <span className="ml-1 text-2xs text-faint">{unit}</span>}
    </span>
  );
}

export function SessionsView({ sessions, agents, loading, error, total, hasMore, loadingMore, onLoadMore,
  newSessionAgentId, clearNewSessionAgentId, onChanged, onOpen }: {
  sessions: SessionSummary[]; agents: AgentSpec[]; loading: boolean; error: string | null;
  // `total` is the whole fleet; `sessions` is what has been paged in so far.
  total: number; hasMore: boolean; loadingMore: boolean; onLoadMore: () => void;
  newSessionAgentId: string | null; clearNewSessionAgentId: () => void; onChanged: () => void; onOpen: (id: string) => void;
}) {
  const [showNew, setShowNew] = useState(false);
  const [deleting, setDeleting] = useState<SessionSummary | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<string>("all");
  const showModal = showNew || newSessionAgentId !== null;
  const closeNew = () => { setShowNew(false); clearNewSessionAgentId(); };

  const counts = useMemo(() => Object.fromEntries(FILTERS.map((f) => [f.id, sessions.filter(f.test).length])), [sessions]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const f = FILTERS.find((x) => x.id === filter)!;
    return sessions.filter((s) => f.test(s) && (!q || `${s.title ?? ""} ${s.id} ${s.model ?? ""} ${s.agent_id ?? ""}`.toLowerCase().includes(q)));
  }, [sessions, query, filter]);

  // Below the scan threshold a header row is pure overhead, so the values carry their own units and
  // the row self-describes; once the list is long enough to scan in columns, the header earns itself
  // back and the units retire.
  const showHeader = filtered.length >= 8;

  return (
    <PageContainer>
      <div className="flex flex-wrap items-center gap-2">
        {/* A filter group, not tabs: this used role="tablist"/role="tab" with no tabpanel, no
            aria-controls, no arrow-key handling and no roving tabindex — so AT announced "tab 1 of
            3" and the user's arrow keys did nothing. aria-pressed toggles describe what it is.
            Filters that match nothing are also hidden ("Ended 0" was a control for nothing). */}
        <div role="group" aria-label="Filter sessions" className="flex items-center gap-1 rounded-lg border border-border bg-surface-2 p-1">
          {FILTERS.filter((f) => f.id === "all" || counts[f.id] > 0).map((f) => (
            <button key={f.id} type="button" aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}
              className={cn("flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium outline-none transition-colors", filter === f.id ? "bg-surface text-text shadow-soft" : "text-muted hover:text-text")}>
              {f.label}<span className="font-mono tabular-nums text-faint">{counts[f.id]}</span>
            </button>
          ))}
        </div>
        <div className="relative min-w-[180px] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-faint" />
          {/* Filtering and search run over the rows LOADED so far, not the fleet — say which
              that is, so "no matches" can't be misread as "it never happened". */}
          <Input value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder={hasMore ? `Search ${sessions.length} loaded of ${total}…` : "Search sessions…"}
            aria-label="Search sessions" className="pl-8" />
        </div>
        <Button onClick={() => setShowNew(true)}><Plus /> New session</Button>
      </div>

      <ResourceState
        loading={loading && sessions.length === 0}
        error={error}
        isEmpty={filtered.length === 0}
        skeleton={<ListSkeleton rows={5} />}
        empty={sessions.length === 0 ? (
          <EmptyState
            icon={Activity}
            title="No sessions yet"
            description="Launch a session from a saved agent, or spin one up with an inline config."
            action={<Button onClick={() => setShowNew(true)}><Plus /> New session</Button>}
          />
        ) : (
          <EmptyState icon={Search} title="No matching sessions" description="No sessions match your current filter or search." />
        )}
      >
        <div className="overflow-hidden rounded-lg border border-border bg-surface shadow-soft">
            {/* Chrome scales with data, not with the component. At N=1 this header's only job was to
                label four numbers — a fill, a divider and 4 labels to present 4 digits — so below
                the scan threshold the rows carry their own units instead and the header retires. */}
            {showHeader && (
            <div className="grid grid-cols-[1fr_auto] gap-4 border-b border-border bg-surface-2 px-4 py-2.5 text-2xs font-semibold uppercase tracking-wide text-faint">
              <div>Session</div>
              <div className="flex gap-4 pr-12 sm:gap-7">
                <span className="hidden w-14 text-right sm:inline">Turns</span>
                <span className="hidden w-14 text-right sm:inline">Tools</span>
                <span className="hidden w-16 text-right sm:inline">Tokens</span>
                <span className="w-16 text-right">Cost</span>
              </div>
            </div>
            )}
            {filtered.map((s) => (
              <div key={s.id} className="group grid grid-cols-[1fr_auto] items-center gap-4 border-b border-border px-4 py-3 transition-colors last:border-0 hover:bg-surface-2">
                <button onClick={() => onOpen(s.id)} className="flex min-w-0 items-center gap-3 text-left">
                  <StatusPill status={s.status} />
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{s.title || s.id}</div>
                    <div className="flex items-center gap-2 text-xs text-muted">
                      {/* A budget hard-stop and a dead sandbox both ended as an unremarkable
                          grey "terminated" row. These are the two outcomes an operator scans a
                          fleet for, so they get a label here rather than only inside the run. */}
                      {s.terminal && (
                        <span className="inline-flex flex-none items-center gap-1 rounded-md px-1.5 py-0.5 text-2xs font-medium"
                          style={{ color: "var(--c-error)", background: "color-mix(in oklch, var(--c-error) 12%, transparent)" }}
                          title={s.terminal === "budget"
                            ? "Hard-stopped by a budget backstop"
                            : "The sandbox stopped unexpectedly (out of memory, evicted, or crashed)"}>
                          <AlertTriangle className="size-3" />
                          {s.terminal === "budget" ? "budget" : "lost"}
                        </span>
                      )}
                      {/* Age leads: on a list ordered newest-first it's the field that says where
                          you are. Exact timestamp stays on hover rather than costing a column. */}
                      <span className="tabular-nums text-faint" title={fmtTime(s.created_ts) || undefined}>{fmtAge(s.created_ts)}</span>
                      <span className="text-faint">·</span>
                      <span className="font-mono">{s.model ?? "—"}</span>
                      {s.system_mode && <><span className="text-faint">·</span><span>{s.system_mode}</span></>}
                    </div>
                  </div>
                </button>
                <div className="flex items-center gap-4 sm:gap-7">
                  {/* Cost stays OUTSIDE the responsive block. On a narrow viewport the supporting
                      counts go first and the spend number never does — it is the one figure an
                      operator checking a long run from their phone came for. */}
                  <div className="hidden gap-7 font-mono text-sm tabular-nums sm:flex">
                    <Metric value={s.user_turns} unit="turns" showUnit={!showHeader} />
                    <Metric value={s.tool_calls} unit="tools" showUnit={!showHeader} />
                    <Metric value={fmtNum(s.tokens.total)} unit="tok" showUnit={!showHeader} wide />
                  </div>
                  <span className="w-16 text-right font-mono text-sm font-semibold tabular-nums text-text">{fmtCost(s.total_cost_usd)}</span>
                  <Button variant="ghost" size="icon-sm" className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100" onClick={() => setDeleting(s)} aria-label="Delete"><Trash2 /></Button>
                </div>
              </div>
            ))}
          {hasMore && (
            <div className="flex items-center justify-center gap-3 border-t border-border px-4 py-3">
              <span className="text-xs text-faint tabular-nums">{sessions.length} of {total}</span>
              <Button variant="outline" size="sm" onClick={onLoadMore} disabled={loadingMore}>
                {loadingMore ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}
          </div>
      </ResourceState>

      {/* `?new=` (present but empty) means "open the launch dialog with no agent pre-picked" — it
          must become null here, since ?? only falls through on null/undefined and an empty string
          would otherwise select a nonexistent agent. */}
      {showModal && <NewSessionDialog agents={agents} presetAgentId={newSessionAgentId || null} onClose={closeNew} onCreated={(id) => { closeNew(); onChanged(); onOpen(id); }} />}

      <ConfirmDialog
        open={deleting != null}
        onOpenChange={(o) => { if (!o) setDeleting(null); }}
        title="Delete session?"
        description={<>Deletes <b>{deleting?.title || deleting?.id}</b> and terminates its sandbox.</>}
        confirmLabel="Delete"
        destructive
        onConfirm={async () => { if (deleting) { await deleteSession(deleting.id); onChanged(); } }}
      />
    </PageContainer>
  );
}

function NewSessionDialog({ agents, presetAgentId, onClose, onCreated }: { agents: AgentSpec[]; presetAgentId?: string | null; onClose: () => void; onCreated: (id: string) => void }) {
  const [mode, setMode] = useState<"agent" | "inline">(agents.length || presetAgentId ? "agent" : "inline");
  const [agentId, setAgentId] = useState(presetAgentId ?? agents[0]?.id ?? "");
  const [prompt, setPrompt] = useState("");
  // The orchestrator owns both the list and the default (no hardcoded "sonnet" here, which
  // would silently disagree with TERRA_MODEL).
  const catalog = useModels().data;
  const models = catalog?.models ?? [];
  const [model, setModel] = useState("");
  const selectedModel = model || catalog?.default || "";
  const [systemMode, setSystemMode] = useState("claude_code");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setSubmitting(true); setError(null);
    const task = prompt.trim();
    // Derived, not asked for. The Title field was a decision demanded BEFORE you were allowed to
    // state the actual job — and nobody filled it, so live sessions are named things like
    // "20260716-154310-267". The job itself is the best title there is.
    const title = task ? task.split(/\s+/).slice(0, 9).join(" ").slice(0, 60) : undefined;
    try {
      const body = mode === "agent" ? { agent_id: agentId, title } : { model: selectedModel, system_mode: systemMode, title };
      const res = await createSession(body);
      // Send the opening prompt straight away: the worker's reader queues commands and the run loop
      // drains them once the CLI client is up, so this is safe before "ready". If it does fail we
      // still open the session rather than losing the run — the composer is right there.
      if (task) {
        try { await sendMessage(res.id, task); }
        catch { toast.error("Session started, but the opening message didn't send. Try again from the composer."); }
      }
      onCreated(res.id);
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); setSubmitting(false); }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>New session</DialogTitle><DialogDescription>Launch a sandbox from a saved agent or an inline config.</DialogDescription></DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <Tabs value={mode} onValueChange={(v) => setMode(v as "agent" | "inline")}>
            <TabsList className="w-full"><TabsTrigger value="agent" className="flex-1"><Bot className="size-3.5" /> From agent</TabsTrigger><TabsTrigger value="inline" className="flex-1">Inline config</TabsTrigger></TabsList>
            <TabsContent value="agent" className="mt-4 outline-none">
              {agents.length === 0 ? <div className="rounded-lg border border-border p-3 text-sm text-muted">No agents registered · use inline config.</div> :
                <Field label="Agent"><Select value={agentId} onValueChange={setAgentId}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{agents.map((a) => <SelectItem key={a.id} value={a.id}>{a.name} ({a.harness.model})</SelectItem>)}</SelectContent></Select></Field>}
            </TabsContent>
            <TabsContent value="inline" className="mt-4 grid grid-cols-2 gap-4 outline-none">
              <Field label="Model"><Select value={selectedModel} onValueChange={setModel}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{models.map((m) => <SelectItem key={m.id} value={m.id}>{m.label}</SelectItem>)}</SelectContent></Select></Field>
              <Field label="System mode"><Select value={systemMode} onValueChange={setSystemMode}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["minimal", "claude_code", "assistant"].map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}</SelectContent></Select></Field>
            </TabsContent>
          </Tabs>
          {/* The job, as the primary field. The old flow made you pick a mode and a title, submit,
              land on an empty "Waiting for events" transcript, and only THEN say what you wanted —
              five steps before stating the actual task. Now: pick an agent, type the job, launch. */}
          <Field label="What should it do?" hint="Sent as the opening message. Leave blank to open an idle session.">
            <Textarea autoFocus rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)}
              placeholder="e.g. Audit the egress rules and tell me what's reachable"
              onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") (e.currentTarget.form as HTMLFormElement)?.requestSubmit(); }} />
          </Field>
          {error && <ErrorBox>{error}</ErrorBox>}
          <div className="flex items-center justify-end gap-3">
            <span className="text-2xs text-faint">⌘↵ to launch</span>
            <Button type="submit" disabled={submitting || (mode === "agent" && !agentId)}>{submitting ? "Launching…" : "Launch"}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
