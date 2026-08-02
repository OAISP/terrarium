"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Send, Square, Trash2, ArrowDown, AlertTriangle, Loader2, Paperclip, ChevronDown, Check, Cpu, WifiOff, Leaf, MessageSquarePlus, Boxes, BarChart3, ScrollText, Download, ShieldCheck } from "lucide-react";
import { CollapsibleCard } from "@/components/ui/collapsible-card";
import type { AgentSpec, Health, LogEvent, SessionSummary, TokenTotals } from "@/lib/types";
import { interruptSession, sendMessage, rewindSession, uploadFile, answerSession, decideSession, reconfigureSession, verifySessionEgress, type EgressVerification, type RewindMode } from "@/lib/api";
import { fmtAge, fmtTime } from "@/lib/format";
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { useEventStream } from "@/lib/useEventStream";
import { useModels } from "@/lib/queries";
import { modelLabel } from "@/lib/harness";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/input";
import { Tooltip } from "@/components/ui/misc";
import { ErrorBox } from "@/components/ui/feedback";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { StatusPill } from "./StatusPill";
import { BudgetGauge } from "./BudgetGauge";
import { EventTimeline } from "./EventTimeline";
import { ConnectionPill } from "./Hud";
import { InspectorRail, type AgentHighlight, type InspectorTab } from "./InspectorRail";
import { artifactsFrom } from "@/lib/artifacts";
import { aggregateAgentTasks } from "@/lib/agentTasks";

type Derived = { status: string; model: string | null; systemMode: string | null; systemPrompt: string | null; title: string | null; userTurns: number; toolCalls: number; cost: number; tokens: TokenTotals };

function derive(events: LogEvent[], base: SessionSummary | null, historyTruncated = false): Derived {
  const d: Derived = { status: base?.status ?? "unknown", model: base?.model ?? null, systemMode: base?.system_mode ?? null, systemPrompt: null, title: base?.title ?? null, userTurns: 0, toolCalls: 0, cost: 0, tokens: { input: 0, output: 0, cacheRead: 0, cacheCreate: 0, subagent: 0, total: 0 } };
  let banked = 0, seg = 0;  // cost: bank completed CLI segments (a rewind reconnect restarts the counter)
  // Sub-agent / workflow tokens land in task_notification.usage.total_tokens (one per task;
  // for a Workflow it's the aggregate of its agents). Keyed by task_id so re-notifications
  // don't double-count. The main `usage` is per-turn MAIN-agent only.
  const subByTask: Record<string, number> = {};
  for (const ev of events) {
    switch (ev.type) {
      case "session_start": case "ready": if (ev.model) d.model = String(ev.model); if (ev.system_mode) d.systemMode = String(ev.system_mode); if (ev.system_prompt) d.systemPrompt = String(ev.system_prompt); if (ev.title) d.title = String(ev.title); break;
      case "system": {
        const sd = (ev.data as Record<string, unknown>) ?? {};
        if (ev.subtype === "config" && (sd as { model?: string }).model) d.model = String((sd as { model: string }).model); // live model switch
        if (ev.subtype === "task_notification") {
          const tt = Number((sd.usage as Record<string, unknown>)?.total_tokens) || 0;
          if (tt && sd.task_id != null) subByTask[String(sd.task_id)] = tt;
        }
        break;
      }
      case "status": if (ev.status) d.status = String(ev.status); break;
      case "session_end": d.status = "ended"; break;
      case "user": d.userTurns += 1; break;
      case "tool_use": d.toolCalls += 1; break;
      case "result": {
        // total_cost_usd is cumulative within a CLI segment, but a rewind reconnect
        // restarts the counter — bank the prior segment when it drops, then accumulate.
        // usage is per-turn, so tokens are summed to give total processed.
        if (ev.total_cost_usd != null) {
          const c = Number(ev.total_cost_usd) || 0;
          if (c + 1e-9 < seg) banked += seg;
          seg = c;
        }
        const u = (ev.usage as Record<string, unknown>) ?? {};
        d.tokens.input += Number(u.input_tokens) || 0; d.tokens.output += Number(u.output_tokens) || 0;
        d.tokens.cacheRead += Number(u.cache_read_input_tokens) || 0; d.tokens.cacheCreate += Number(u.cache_creation_input_tokens) || 0;
        break;
      }
    }
  }
  d.cost = banked + seg;  // banked completed segments + the current one
  d.tokens.subagent = Object.values(subByTask).reduce((a, b) => a + b, 0);
  d.tokens.total = d.tokens.input + d.tokens.output + d.tokens.cacheRead + d.tokens.cacheCreate + d.tokens.subagent;
  // A bounded transcript is intentionally incomplete. Never replace fleet totals
  // with totals folded from only its tail.
  if (base && (events.length === 0 || historyTruncated)) {
    d.cost = Math.max(d.cost, base.total_cost_usd);
    d.tokens = base.tokens;
    d.userTurns = Math.max(d.userTurns, base.user_turns);
    d.toolCalls = Math.max(d.toolCalls, base.tool_calls);
  }
  return d;
}

export function SessionView({ sessionId, summary, agent, health, onBack, onDeleted }: {
  sessionId: string; summary: SessionSummary | null; agent?: AgentSpec | null; health: Health | null; onBack: () => void; onDeleted: () => void;
}) {
  const { events, conn, streaming, retry, historyTruncated, retryNow } = useEventStream(sessionId);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [uploads, setUploads] = useState<string[]>([]);
  const [uploading, setUploading] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const [pendingEcho, setPendingEcho] = useState<string | null>(null);  // optimistic user message
  const [agentsOpen, setAgentsOpen] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("activity");
  const [agentHighlight, setAgentHighlight] = useState<AgentHighlight | null>(null);
  const [auditResult, setAuditResult] = useState<EgressVerification | null>(null);
  const [verifyingAudit, setVerifyingAudit] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const atBottomRef = useRef(true);
  const userCountRef = useRef(0);

  const d = useMemo(
    () => derive(events, summary, historyTruncated),
    [events, summary, historyTruncated],
  );
  // Append the optimistic echo until the real `user` event round-trips via SSE, so the
  // message appears instantly on send instead of after the server confirms it.
  const shownEvents = useMemo(() => {
    if (!pendingEcho) return events;
    const seq = (events[events.length - 1]?.seq ?? 0) + 1;
    return [...events, { type: "user", text: pendingEcho, seq, ts: new Date().toISOString() } as LogEvent];
  }, [events, pendingEcho]);
  // Sub-agent / workflow tasks for the Agents side panel — same aggregation the transcript uses.
  const agents = useMemo(() => aggregateAgentTasks(shownEvents), [shownEvents]);
  const artifacts = useMemo(() => artifactsFrom(events), [events]);
  const hasAgents = agents.taskList.length > 0;
  // Persist rail open/closed + active tab per session (rail defaults closed; opens on the header
  // pill or an inline ref — both land on the Activity tab).
  const agentsKey = `terra:agents-panel:${sessionId}`;
  const tabKey = `terra:inspector-tab:${sessionId}`;
  // localStorage does not exist during SSR/render, so restoring persisted UI state can only
  // happen after mount.
  useEffect(() => {
    try {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setAgentsOpen(localStorage.getItem(agentsKey) === "1");
      const t = localStorage.getItem(tabKey);
      if (t === "activity" || t === "stats") setInspectorTab(t);
    } catch { /* SSR / blocked */ }
  }, [agentsKey, tabKey]);
  function changeTab(t: InspectorTab) {
    setInspectorTab(t);
    try { localStorage.setItem(tabKey, t); } catch { /* ignore */ }
  }
  function toggleAgents(open: boolean) {
    setAgentsOpen(open);
    // Opening lands on Activity when there are agents to inspect, else on Stats (an empty
    // Activity tab shouldn't be auto-selected).
    if (open) changeTab(hasAgents ? "activity" : "stats");
    try { localStorage.setItem(agentsKey, open ? "1" : "0"); } catch { /* ignore */ }
  }
  function openAgentsAt(toolUseId: string) {
    toggleAgents(true);
    setAgentHighlight({ id: toolUseId, n: Date.now() });
  }
  // Clear the echo once a new `user` event arrives (ours, reconciled from the stream).
  useEffect(() => {
    const c = events.reduce((n, e) => n + (e.type === "user" ? 1 : 0), 0);
    if (pendingEcho && c > userCountRef.current) setPendingEcho(null);
    userCountRef.current = c;
  }, [events, pendingEcho]);
  // "working" = the agent is actively processing a turn (idle ≠ working — idle waits for input)
  const working = d.status === "running" || d.status === "starting";
  const ended = d.status === "ended" || d.status === "terminated" || d.status === "error";
  const maxBudget = agent?.harness.max_budget_usd ?? null;
  const overBudget = maxBudget != null && maxBudget > 0 && d.cost / maxBudget > 0.6;
  const isolation: "isolated" | "shared" = agent?.memory_scope && agent.memory_scope !== agent.id ? "shared" : "isolated";

  useEffect(() => { const el = scrollRef.current; if (el && atBottomRef.current) el.scrollTop = el.scrollHeight; }, [shownEvents.length]);
  function onScroll() { const el = scrollRef.current; if (!el) return; const b = el.scrollHeight - el.scrollTop - el.clientHeight < 80; atBottomRef.current = b; setAtBottom(b); }
  function toBottom() { const el = scrollRef.current; if (el) { el.scrollTop = el.scrollHeight; atBottomRef.current = true; setAtBottom(true); } }

  async function handleSend(e?: React.FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || sending || ended) return;
    setSending(true); setActionError(null);
    setInput(""); setPendingEcho(text); atBottomRef.current = true;  // optimistic: show it now
    try { await sendMessage(sessionId, text); }
    catch (err) { setActionError(err instanceof Error ? err.message : String(err)); setPendingEcho(null); setInput(text); }  // rollback
    finally { setSending(false); }
  }
  async function handleInterrupt() { setActionError(null); try { await interruptSession(sessionId); } catch (err) { setActionError(err instanceof Error ? err.message : String(err)); } }
  async function handleVerifyAudit() {
    setVerifyingAudit(true); setActionError(null);
    try { setAuditResult(await verifySessionEgress(sessionId)); }
    catch (err) { setAuditResult(null); setActionError(err instanceof Error ? err.message : String(err)); }
    finally { setVerifyingAudit(false); }
  }
  // Export comes directly from the durable log; the transcript intentionally keeps
  // only a bounded recent window for long-running sessions.
  function handleExport() {
    const a = document.createElement("a");
    a.href = `/api/sessions/${encodeURIComponent(sessionId)}/events/export`;
    a.download = `terrarium-${sessionId}.jsonl`;
    document.body.appendChild(a); a.click(); a.remove();
  }
  async function switchModel(m: string) { setActionError(null); try { await reconfigureSession(sessionId, m); } catch (err) { setActionError(err instanceof Error ? err.message : String(err)); } }
  async function handleRewind(messageId: string, mode: RewindMode, editText?: string) {
    setActionError(null);
    try {
      await rewindSession(sessionId, messageId, mode);
      if (editText !== undefined) setInput(editText);  // edit flow: drop the original text back into the composer
    } catch (err) { setActionError(err instanceof Error ? err.message : String(err)); }
  }
  async function handleUpload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true); setActionError(null);
    try {
      const names: string[] = [];
      for (const f of Array.from(files)) names.push((await uploadFile(sessionId, f)).name);
      setUploads((u) => [...u, ...names]);
      setInput((v) => (v ? v + " " : "") + names.map((n) => `@${n}`).join(" "));  // mention them for the agent
    } catch (err) { setActionError(err instanceof Error ? err.message : String(err)); }
    finally { setUploading(false); }
  }

  return (
    <div className="flex h-full flex-col gap-3.5">
      {/* detail HUD */}
      <header className="flex flex-wrap items-center gap-2 sm:gap-3 rounded-xl border border-border bg-panel px-3 py-2.5 shadow-soft sm:px-4">
        <Button variant="ghost" size="icon-sm" onClick={onBack} aria-label="Back to sessions"><ArrowLeft /></Button>
        <div className="min-w-40 flex-1">
          <div className="flex items-center gap-2">
            {/* This view replaces the Hud, so it owns the page's h1. */}
            <h1 className="truncate text-sm font-semibold">{d.title || sessionId}</h1>
            {summary?.memory_isolated && (
              <Tooltip label="Another session of this agent is already live, so this one runs on its own memory instead of the agent's.">
                <span className="flex-none rounded-md border border-border bg-surface-2 px-1.5 py-0.5 text-2xs text-muted">isolated memory</span>
              </Tooltip>
            )}
          </div>
          {/* Only when it adds something: untitled sessions (the common case — nobody fills Title)
              already show the id as the heading, so don't repeat it verbatim underneath itself.
              The age is worth a line either way — "how long has this been going" is the first
              question about a run you've just opened. */}
          <div className="flex items-center gap-1.5 text-2xs text-faint">
            {d.title && <span className="truncate font-mono">{sessionId}</span>}
            {d.title && summary?.created_ts && <span>·</span>}
            {summary?.created_ts && (
              <span className="flex-none tabular-nums" title={fmtTime(summary.created_ts) || undefined}>
                started {fmtAge(summary.created_ts)}
              </span>
            )}
          </div>
        </div>
        {/* The Inspector toggle is always available: "Agents · N" → Activity when sub-agents
            exist, else a compact "Stats" control → the Stats tab. Default closed either way. */}
        <Tooltip label={agentsOpen ? "Hide the inspector" : hasAgents ? "Show sub-agents & workflows" : "Show session stats"}>
          <button type="button" onClick={() => toggleAgents(!agentsOpen)} aria-pressed={agentsOpen}
            className="inline-flex flex-none items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent"
            style={agentsOpen
              ? { borderColor: "color-mix(in oklch, var(--c-agent) 30%, var(--border))", background: "color-mix(in oklch, var(--c-agent) 12%, var(--surface))", color: "var(--c-agent)" }
              : { borderColor: "var(--border)", color: "var(--muted)" }}>
            {hasAgents ? (
              <>
                <Boxes className="size-3.5" /> Agents
                <span className="rounded bg-surface-2 px-1 py-0.5 text-2xs tabular-nums text-muted">{agents.taskList.length}</span>
              </>
            ) : (
              <><BarChart3 className="size-3.5" /> Stats</>
            )}
          </button>
        </Tooltip>
        <ModelSwitcher model={d.model} disabled={ended} onSwitch={switchModel} />
        <StatusPill status={d.status} />
        {/* Cost lives in the rail footer when it's open — show the gauge only as a fallback. */}
        {maxBudget != null && !agentsOpen && <Tooltip label={`$${d.cost.toFixed(2)} of $${maxBudget} budget`}><div><BudgetGauge cost={d.cost} budget={maxBudget} /></div></Tooltip>}
        <ConnectionPill conn={conn} retry={retry} onRetry={retryNow} />
        <Tooltip label={auditResult ? auditResult.reason : "Verify the retained egress audit from genesis"}>
          <Button variant="ghost" size="sm" onClick={handleVerifyAudit} disabled={verifyingAudit}
            aria-label="Verify egress audit integrity"
            className={auditResult ? (auditResult.ok ? "text-agent" : "text-error") : undefined}>
            {verifyingAudit ? <Loader2 className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />}
            <span className="hidden xl:inline">{auditResult ? (auditResult.ok ? "Audit verified" : "Audit failed") : "Verify audit"}</span>
          </Button>
        </Tooltip>
        <Tooltip label="Export the raw session as JSONL (every event)">
          <Button variant="ghost" size="icon-sm" onClick={handleExport} disabled={events.length === 0} aria-label="Export raw session"><Download /></Button>
        </Tooltip>
        {working ? (
          <Button variant="danger" size="sm" onClick={handleInterrupt}><Square className="size-3.5" /> Interrupt</Button>
        ) : (
          <Tooltip label="Delete session"><Button variant="ghost" size="icon-sm" onClick={() => setConfirmDel(true)} aria-label="Delete"><Trash2 /></Button></Tooltip>
        )}
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-3.5 lg:flex-row">
        {/* transcript + composer */}
        <div className="flex min-w-0 flex-1 flex-col rounded-xl border border-border bg-surface shadow-soft">
          {overBudget && (() => {
            const usedPct = Math.min(100, Math.round((d.cost / (maxBudget as number)) * 100));
            const tone = usedPct >= 85 ? "var(--c-error)" : "var(--c-result)";
            return (
              <div className="flex items-center gap-2 border-b px-4 py-2 text-xs" style={{ background: `color-mix(in oklch, ${tone} 10%, transparent)`, borderColor: `color-mix(in oklch, ${tone} 24%, var(--border))`, color: tone }}>
                <AlertTriangle className="size-3.5 flex-none" />
                <span>{usedPct >= 100 ? "Budget reached" : `${usedPct}% of budget used`}. Spent ${d.cost.toFixed(2)} of a ${maxBudget} cap.</span>
              </div>
            );
          })()}
          <div className="relative min-h-0 flex-1">
            {/* The transcript is NOT a live region — a polite live region over a token-
                streaming container floods screen readers. Status is announced separately below. */}
            <div ref={scrollRef} onScroll={onScroll} aria-label="Conversation transcript" aria-busy={working} className="absolute inset-0 overflow-y-auto px-5 py-5">
              {/* The resolved system prompt for this session — open by default on a fresh session
                  (nothing else to read yet), collapsible once the conversation is underway. */}
              {d.systemPrompt && (
                <CollapsibleCard
                  tone="var(--muted)"
                  defaultOpen={d.userTurns === 0}
                  contentClassName="px-3 py-2"
                  header={<>
                    <ScrollText className="size-3.5 flex-none text-faint" />
                    <span className="font-medium">System prompt</span>
                    {d.systemMode && <span className="rounded bg-surface-2 px-1.5 py-0.5 text-2xs text-muted">{d.systemMode}</span>}
                  </>}
                >
                  <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap break-words font-mono text-2xs leading-relaxed text-muted">{d.systemPrompt}</pre>
                </CollapsibleCard>
              )}
              {shownEvents.length === 0 ? (() => {
                // Branch the empty state on the real situation so it never reads "loading" when
                // it isn't: lost stream (WifiOff), ended (static leaf), connecting (spinner),
                // or idle-and-ready (invite-to-type, no spin).
                const es = conn === "error"
                  ? { Icon: WifiOff, spin: false, head: "Reconnecting to the stream…", sub: "Lost the connection. Retrying automatically." }
                  : ended
                  ? { Icon: Leaf, spin: false, head: "Session ended", sub: "It can't take new turns. Start a new session to continue." }
                  : conn === "connecting"
                  ? { Icon: Loader2, spin: true, head: "Connecting…", sub: "Opening the event stream." }
                  : { Icon: MessageSquarePlus, spin: false, head: "Waiting for events", sub: "Send a message below to start the agent." };
                return (
                  <div className="flex h-full flex-col items-center justify-center gap-3 text-center text-sm text-muted">
                    <span className="relative grid size-11 place-items-center rounded-2xl border border-border bg-surface-2">
                      <es.Icon className={`size-5 ${es.spin ? "animate-spin text-accent opacity-80" : "text-faint"}`} />
                    </span>
                    <div>
                      <div className="font-medium text-text">{es.head}</div>
                      <div className="mt-0.5 text-xs text-faint">{es.sub}</div>
                    </div>
                  </div>
                );
              })() : <>
                {historyTruncated && (
                  <div className="mx-3 mb-2 rounded-lg border border-border bg-surface-2 px-3 py-2 text-xs text-muted">
                    Showing the latest activity. Export the raw session to inspect the complete history.
                  </div>
                )}
                <EventTimeline events={shownEvents} tasks={agents.tasks} cardIds={agents.cardIds} resultByToolUse={agents.resultByToolUse} agentName={agent?.name ?? "Agent"} busy={working} streaming={streaming} onRewind={handleRewind} onAnswer={(qid, answers) => answerSession(sessionId, qid, answers)} onDecide={(rid, decision) => decideSession(sessionId, rid, decision)} onOpenAgents={openAgentsAt} />
              </>}
            </div>
            {/* Status-only announcer for assistive tech (replaces the flooding live region). */}
            <div className="sr-only" role="status" aria-live="polite">
              {ended ? "Session ended." : working ? "Agent is working." : "Agent is ready for input."}
            </div>
            {!atBottom && (
              <button onClick={toBottom} aria-label="Jump to latest event"
                className="absolute bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border bg-panel px-3 py-1.5 text-xs font-medium shadow-pop transition-colors hover:border-border-2 hover:bg-surface-2 motion-safe:animate-[terra-in_0.2s_var(--ease)]">
                <ArrowDown className="size-3.5" /> Jump to latest
              </button>
            )}
          </div>

          {actionError && <ErrorBox className="mx-4 mb-2">{actionError}</ErrorBox>}

          <form onSubmit={handleSend} className="border-t border-border p-3"
            onDragOver={(e) => { if (!ended) e.preventDefault(); }}
            onDrop={(e) => { if (!ended) { e.preventDefault(); handleUpload(e.dataTransfer.files); } }}>
            {uploads.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-1.5">
                {uploads.map((n, i) => (
                  <span key={`${n}-${i}`} className="inline-flex items-center gap-1 rounded-md border border-border bg-surface-2 px-2 py-0.5 text-2xs text-muted">
                    <Paperclip className="size-3" /> {n}
                  </span>
                ))}
              </div>
            )}
            <div className="relative">
              <Textarea value={input} onChange={(e) => setInput(e.target.value)} disabled={ended} autoFocus
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
                placeholder={ended ? "Session has ended." : "Message the agent…  (⏎ send · ⇧⏎ newline · drop files to upload)"} rows={2} className="resize-none rounded-xl pl-11 pr-28" />
              <input ref={fileRef} type="file" multiple className="hidden"
                onChange={(e) => { handleUpload(e.target.files); e.target.value = ""; }} />
              <div className="absolute bottom-2 left-2">
                <Tooltip label="Upload files to the workspace">
                  <Button type="button" variant="ghost" size="icon-sm" disabled={ended || uploading}
                    onClick={() => fileRef.current?.click()} aria-label="Upload files">
                    {uploading ? <Loader2 className="size-4 animate-spin" /> : <Paperclip className="size-4" />}
                  </Button>
                </Tooltip>
              </div>
              <div className="absolute bottom-2 right-2">
                {working ? (
                  <Button type="button" variant="danger" size="sm" onClick={handleInterrupt}><Square className="size-3.5" /> Interrupt</Button>
                ) : (
                  <Button type="submit" size="sm" disabled={sending || ended || !input.trim()}>{sending ? <Loader2 className="size-3.5 animate-spin" /> : <Send className="size-3.5" />} Send</Button>
                )}
              </div>
            </div>
          </form>
        </div>

        {agentsOpen && (
          <InspectorRail tab={inspectorTab} onTab={changeTab} onClose={() => toggleAgents(false)}
            tasks={agents.taskList} resultByToolUse={agents.resultByToolUse} busy={working} highlight={agentHighlight}
            cost={d.cost} budget={maxBudget} tokens={d.tokens} turns={d.userTurns} tools={d.toolCalls}
            health={health} permission={agent?.harness.permission_mode ?? "default"} isolation={isolation}
            sessionId={sessionId} artifacts={artifacts} context={summary?.context} />
        )}
      </div>

      <ConfirmDialog
        open={confirmDel}
        onOpenChange={setConfirmDel}
        title="Delete this session?"
        description="Its sandbox is destroyed and it can't be resumed."
        confirmLabel="Delete"
        destructive
        onConfirm={onDeleted}
      />
    </div>
  );
}

/** Switch the running session's model live (set_model — the conversation continues; the
 *  next turn re-reads context uncached). Shows the current model + a quick picker.
 *
 *  Offers the orchestrator's FULL catalog, and matches the current model by exact id. A
 *  short alias-only list cannot express a session pinned to a concrete generation: every
 *  "switch" from it would silently move the session off the model it was configured with,
 *  and nothing would ever read as already-selected. */
function ModelSwitcher({ model, disabled, onSwitch }: { model: string | null; disabled?: boolean; onSwitch: (m: string) => void }) {
  const catalog = useModels().data?.models ?? [];
  // Always include what's actually running, even if the catalog doesn't list it — otherwise
  // the control claims the session is on something it isn't.
  const options = model && !catalog.some((m) => m.id === model)
    ? [{ id: model, label: modelLabel(model), alias: false }, ...catalog]
    : catalog;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled}>
        <Button variant="outline" size="sm" className="gap-1.5 font-mono text-xs" disabled={disabled} title="Switch model without restarting">
          <Cpu className="size-3.5" /> <span className="hidden max-w-[120px] truncate sm:inline">{modelLabel(model, catalog)}</span> <ChevronDown className="size-3" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-[70vh] overflow-y-auto">
        {options.map((m) => (
          <DropdownMenuItem key={m.id} onSelect={() => { if (m.id !== model) onSwitch(m.id); }} className="gap-2">
            {m.id === model ? <Check className="size-3.5 text-accent" /> : <span className="size-3.5" />}
            <span className="flex-1">{m.label}</span>
            {m.alias && <span className="text-2xs text-faint">alias</span>}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
