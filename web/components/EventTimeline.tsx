"use client";

import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";
import { User, Leaf, Sparkles, Wrench, AlertTriangle, ChevronDown, ChevronRight, History, Loader2, Boxes, Bot } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { CopyButton } from "@/components/ui/misc";
import { Markdown } from "@/components/ui/markdown";
import { StateDot, type InspectTarget } from "@/components/ui/activity";
import { Chip } from "@/components/ui/chip";
import { DetailSheet } from "@/components/agent-detail";
import { STATE_LABEL, tint, tintBorder } from "@/lib/tint";
import { QuestionCard } from "@/components/QuestionCard";
import { PermissionCard } from "@/components/PermissionCard";
import { WorkflowCard } from "@/components/WorkflowCard";
import { SubAgentCard } from "@/components/SubAgentCard";
import { isWorkflowTask, str, taskState, workflowCounts, type TaskAgg, type TaskResult } from "@/lib/agentTasks";
import type { Decision } from "@/lib/api";
import type { LogEvent } from "@/lib/types";
import type { RewindMode } from "@/lib/api";
import { fmtClock, fmtCost, fmtDuration, fmtNum, toolOneLiner } from "@/lib/format";


export function EventTimeline({ events, tasks, cardIds, resultByToolUse, agentName, busy, streaming, onRewind, onAnswer, onDecide, onOpenAgents }: {
  events: LogEvent[];
  // Sub-agent / workflow aggregation hoisted to SessionView's single memo (shared with the
  // Inspector rail) and passed in — so it isn't recomputed per SSE event on both surfaces.
  tasks: Record<string, TaskAgg>; cardIds: Set<string>; resultByToolUse: Record<string, TaskResult>;
  agentName: string; busy: boolean; streaming?: string;
  onRewind?: (messageId: string, mode: RewindMode, editText?: string) => void;
  onAnswer?: (questionId: string, answers: Record<string, string | string[]>) => Promise<void>;
  onDecide?: (requestId: string, decision: Decision) => Promise<void>;
  // When provided, sub-agent/workflow cards move to the Agents side panel and the transcript
  // shows a compact reference instead; clicking it opens the panel on that task.
  onOpenAgents?: (toolUseId: string) => void;
}) {
  // Derive anchors / hidden-set / visible list ONCE per events change. This was
  // recomputed on every render (i.e. every SSE event before batching) and the hidden-set
  // pass is O(n²), so without memoization a live run did O(n²) work per event.
  const { anchorBySeq, rows, answeredById, decidedById } = React.useMemo(() => {
    // An "answered" event locks the matching question card; "decided" locks a permission.
    const answeredById: Record<string, Record<string, string | string[]>> = {};
    const decidedById: Record<string, Decision> = {};
    for (const ev of events) {
      if (ev.type === "answered" && typeof ev.question_id === "string") {
        answeredById[ev.question_id] = (ev.answers as Record<string, string | string[]>) ?? {};
      } else if (ev.type === "decided" && typeof ev.request_id === "string") {
        decidedById[ev.request_id] = ev.decision as Decision;
      }
    }
    // Anchor each user turn (by seq) with the uuid carried by the following rewind_point
    // event, so we can offer "rewind to here". The markers themselves stay hidden.
    const anchorBySeq: Record<number, string> = {};
    let lastUserSeq: number | null = null;
    for (const ev of events) {
      if (ev.type === "user") lastUserSeq = ev.seq;
      else if (ev.type === "rewind_point" && lastUserSeq != null && typeof ev.message_id === "string") anchorBySeq[lastUserSeq] = ev.message_id;
    }
    // A conversation/both rewind truncates the VIEW: hide the target turn + everything
    // through its rewound marker (the on-disk event log stays append-only).
    const hidden = new Set<number>();
    for (const r of events) {
      if (r.type !== "rewound" || (r.mode !== "conversation" && r.mode !== "both")) continue;
      let targetSeq: number | null = null;
      for (const u of events) {
        if (u.seq >= r.seq) break;
        if (u.type === "user" && anchorBySeq[u.seq] === r.message_id) targetSeq = u.seq;
      }
      if (targetSeq != null) for (const e of events) {
        if (e.seq >= targetSeq && e.seq <= r.seq) hidden.add(e.seq);
      }
    }
    const visible = events.filter((ev) => ev.type !== "rewind_point" && !hidden.has(ev.seq)
      && ev.type !== "status"                                          // transient run-state (idle/running) — shown live by the StatusPill, not part of the transcript; a rewind reconnect emits an extra idle, so these otherwise pile up
      && ev.type !== "answered" && ev.type !== "decided"               // metadata: lock the question/permission card, not their own rows
      && !(ev.type === "thinking" && !String(ev.text ?? "").trim())   // drop empty (omitted) reasoning steps
      && !(ev.type === "system" && ev.subtype === "thinking_tokens")   // drop transient thinking-token progress
      && !(ev.type === "system" && typeof ev.subtype === "string" && ev.subtype.startsWith("task_")) // task_started/progress/notification/updated/completed → folded into the Agent/Workflow card
      && !(ev.type === "tool_result" && typeof ev.tool_use_id === "string" && cardIds.has(ev.tool_use_id))); // the card renders the subagent's output

    // Collapse consecutive tool steps into ONE expandable group so a burst of steps doesn't
    // flood the transcript; Agent/Workflow calls render as their own card (not a tool step).
    type Row = { kind: "tools"; seq: number; steps: LogEvent[] } | { kind: "card"; ev: LogEvent; task: TaskAgg } | { kind: "ev"; ev: LogEvent };
    const rows: Row[] = [];
    for (const ev of visible) {
      if (ev.type === "tool_use" && typeof ev.id === "string" && cardIds.has(ev.id)) {
        rows.push({ kind: "card", ev, task: tasks[ev.id] });
      } else if (ev.type === "tool_use" || ev.type === "tool_result") {
        const last = rows[rows.length - 1];
        if (last && last.kind === "tools") last.steps.push(ev);
        else rows.push({ kind: "tools", seq: ev.seq, steps: [ev] });
      } else {
        rows.push({ kind: "ev", ev });
      }
    }
    return { anchorBySeq, rows, answeredById, decidedById };
  }, [events, tasks, cardIds]);
  const [inspect, setInspect] = React.useState<InspectTarget | null>(null);

  // Finding the one failure in a 300+ row transcript was manual scroll-and-expand: a failed tool
  // call showed only as a grey "N errors" inside a collapsed group, with no jump, anchor, or search.
  // Errors are now addressable — each failing row carries an id and this strip walks them.
  const errorSeqs = React.useMemo(
    () => events.filter((e) => e.type === "error" || e.type === "worker_lost" || e.type === "budget_exceeded"
      || (e.type === "tool_result" && e.is_error === true))
      .map((e) => e.seq).filter((s): s is number => typeof s === "number"),
    [events],
  );
  const [errIdx, setErrIdx] = React.useState(0);
  const jumpToError = () => {
    if (!errorSeqs.length) return;
    const i = errIdx % errorSeqs.length;
    setErrIdx(i + 1);
    document.getElementById(`ev-${errorSeqs[i]}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  return (
    <div className="mx-auto max-w-[760px]">
      {errorSeqs.length > 0 && (
        <div className="sticky top-0 z-20 -mx-2 mb-2 flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-xs backdrop-blur"
          style={{ borderColor: "color-mix(in oklch, var(--c-error) 35%, var(--border))", background: "color-mix(in oklch, var(--c-error) 10%, var(--panel))" }}>
          <AlertTriangle className="size-3.5 flex-none" style={{ color: "var(--c-error)" }} />
          <span style={{ color: "var(--c-error)" }} className="font-medium tabular-nums">
            {errorSeqs.length} error{errorSeqs.length > 1 ? "s" : ""} in this run
          </span>
          <button type="button" onClick={jumpToError}
            className="ml-auto rounded-md border border-border bg-surface-2 px-2 py-0.5 text-2xs font-medium text-text transition-colors hover:border-accent hover:text-accent">
            Jump{errorSeqs.length > 1 ? ` (${(errIdx % errorSeqs.length) + 1}/${errorSeqs.length})` : ""}
          </button>
        </div>
      )}
      {rows.map((row, i) => {
        const key = row.kind === "tools" ? `tg${row.seq}` : (row.ev.seq ?? `i${i}`);
        // While text is actively streaming below, the live caret belongs to the streaming
        // bubble — don't also flag the last persisted row as the head.
        const head = busy && i === rows.length - 1 && !streaming;
        // Speaker turns render as claude.ai-style messages (avatar + name + body), no rail.
        if (row.kind === "ev" && (row.ev.type === "user" || row.ev.type === "assistant_text")) {
          return (
            <Message key={key} ev={row.ev} agentName={agentName} head={head && row.ev.type === "assistant_text"}
              anchor={row.ev.type === "user" ? anchorBySeq[row.ev.seq] : undefined} onRewind={onRewind} />
          );
        }
        if (row.kind === "ev" && row.ev.type === "rewound") return <RewoundDivider key={key} ev={row.ev} />;
        // Everything else is agent *process* — indented under the content column, quiet.
        const node =
          row.kind === "tools" ? (
            <ToolGroup steps={row.steps} live={head} />
          ) : row.kind === "card" ? (
            onOpenAgents ? (
              <AgentRef task={row.task} result={resultByToolUse[String(row.ev.id)]} onOpen={() => onOpenAgents(String(row.ev.id))} />
            ) : row.task.taskType === "local_workflow" || row.task.agents.length > 0 || row.task.phases.length > 0 ? (
              <WorkflowCard wf={row.task} output={resultByToolUse[String(row.ev.id)]?.content}
                isError={resultByToolUse[String(row.ev.id)]?.isError} detached={!busy} onInspect={setInspect} />
            ) : (
              <SubAgentCard task={row.task} output={resultByToolUse[String(row.ev.id)]?.content}
                isError={resultByToolUse[String(row.ev.id)]?.isError || row.task.isError}
                live={!resultByToolUse[String(row.ev.id)] && !row.task.done} detached={!busy} />
            )
          ) : row.ev.type === "question" && onAnswer ? (
            <QuestionCard questionId={String(row.ev.question_id ?? "")} questions={(row.ev.questions as never) ?? []}
              answered={answeredById[String(row.ev.question_id ?? "")]} onAnswer={onAnswer} />
          ) : row.ev.type === "permission" && onDecide ? (
            <PermissionCard requestId={String(row.ev.request_id ?? "")} toolName={String(row.ev.tool_name ?? "tool")}
              input={row.ev.input as Record<string, unknown> | undefined} title={row.ev.title as string | undefined}
              description={row.ev.description as string | undefined} decided={decidedById[String(row.ev.request_id ?? "")]}
              onDecide={onDecide} />
          ) : (
            <ProcessRow ev={row.ev} head={head} />
          );
        // Anchor every process row by seq so the error strip (and a future deep link) can address
        // it. A tool group anchors on the first failing step it holds, since that's what you jumped
        // for — the group itself already auto-opens when it contains an error.
        const anchorSeq = row.kind === "tools"
          ? row.steps.find((s) => s.type === "tool_result" && s.is_error === true)?.seq
          : row.ev.seq;
        return <div key={key} id={typeof anchorSeq === "number" ? `ev-${anchorSeq}` : undefined} className="ml-[42px] scroll-mt-16">{node}</div>;
      })}
      {/* Live token stream: the in-flight assistant text, shown with the caret until the
          canonical assistant_text lands (at which point `streaming` clears → no flicker). */}
      {busy && streaming ? (
        <Message ev={{ type: "assistant_text", text: streaming } as unknown as LogEvent} agentName={agentName} head />
      ) : null}
      <DetailSheet target={inspect} onClose={() => setInspect(null)} />
    </div>
  );
}

/** A run-ending failure the operator has to notice: loud, always expanded, never collapsed
 *  behind a disclosure. Reserved for the two terminal states that are not "the agent
 *  finished" — a budget hard-stop and a dead sandbox. */
function TerminalCard({ tone, title, children }: { tone: string; title: string; children: React.ReactNode }) {
  return (
    <div className="mt-1.5 flex items-start gap-2.5 rounded-[11px] border p-3 text-[13.5px] leading-relaxed"
      style={{ background: tint(tone, 10), borderColor: tintBorder(tone, 34), color: "var(--muted)" }}>
      <AlertTriangle className="mt-0.5 size-4 flex-none" style={{ color: tone }} aria-hidden />
      <div>
        <div className="font-medium" style={{ color: tone }}>{title}</div>
        <div className="mt-0.5">{children}</div>
      </div>
    </div>
  );
}

/** Extract a one-line summary from a tool_use's input (the command, file, url…),
 *  falling back to the tool name when its input has nothing one-liner-worthy. */
function toolSummary(ev: LogEvent): string {
  return toolOneLiner(ev.input as Record<string, unknown> | undefined) || str(ev.name);
}

/**
 * A run of consecutive tool steps, folded into one collapsible row. Collapsed by default
 * (a compact "N steps" summary); while live, the header shows the current step so you can
 * follow along without the steps floating in one by one. Expand for the full detail.
 */
function ToolGroup({ steps, live }: { steps: LogEvent[]; live: boolean }) {
  const uses = steps.filter((s) => s.type === "tool_use");
  const names = [...new Set(uses.map((s) => str(s.name)).filter(Boolean))];
  const errors = steps.filter((s) => s.type === "tool_result" && s.is_error === true).length;
  // A failure must never hide. The inner tool_result sets defaultOpen on is_error — but inside a
  // COLLAPSED group its CollapsibleContent never mounts, so that never fires, leaving the failure as
  // a grey "N errors" somewhere in a 300+ row scroll. Start open when this group holds an error, and
  // open it if one arrives mid-stream — but only once, so closing it stays closed.
  const [open, setOpen] = React.useState(errors > 0);
  const opened = React.useRef(errors > 0);
  React.useEffect(() => {
    if (errors > 0 && !opened.current) { opened.current = true; setOpen(true); }
  }, [errors]);
  const lastUse = [...steps].reverse().find((s) => s.type === "tool_use");
  const count = uses.length || steps.length;
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="my-1.5 overflow-hidden rounded-lg border border-border bg-surface">
      <CollapsibleTrigger className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-xs outline-none transition-colors hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-accent">
        {live ? <Loader2 className="size-3.5 flex-none animate-spin" style={{ color: "var(--accent)" }} /> : <Wrench className="size-3.5 flex-none text-faint" />}
        <span className="flex-none font-medium text-muted">{live ? "Working" : `${count} ${count === 1 ? "step" : "steps"}`}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-faint">{live && lastUse ? toolSummary(lastUse) : lastUse ? `${names.join(" · ")} · ${toolSummary(lastUse)}` : names.join(" · ")}</span>
        {errors > 0 && <span className="flex-none text-2xs text-error">{errors} error{errors > 1 ? "s" : ""}</span>}
        <ChevronDown size={13} className="flex-none text-faint transition-transform" style={{ transform: open ? "rotate(180deg)" : "" }} />
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-1 border-t border-border px-3 py-2">
        {steps.map((s, i) => <EventBody key={s.seq ?? i} ev={s} isHead={false} />)}
      </CollapsibleContent>
    </Collapsible>
  );
}

function RewoundDivider({ ev }: { ev: LogEvent }) {
  const what = str(ev.mode) === "conversation" ? "conversation" : str(ev.mode) === "both" ? "conversation + workspace" : "workspace";
  return (
    <div className="my-2 flex items-center gap-3 pl-[42px] text-2xs text-muted">
      <span className="h-px flex-1" style={{ background: tintBorder("var(--c-think)", 40) }} />
      <span className="inline-flex items-center gap-1.5"><History size={12} /> rewound {what} to an earlier turn</span>
      <span className="h-px flex-1" style={{ background: tintBorder("var(--c-think)", 40) }} />
    </div>
  );
}

/** A compact one-liner standing in for a sub-agent/workflow card that now lives in the Agents
 *  side panel. Quiet (var(--c-agent) tint), on the indented process column. Clicking opens the
 *  panel and highlights this task's card. */
function AgentRef({ task, result, onOpen }: { task: TaskAgg; result?: TaskResult; onOpen: () => void }) {
  const wf = isWorkflowTask(task);
  const Icon = wf ? Boxes : Bot;
  const state = taskState(task, result);
  let chip: string;
  if (wf) {
    const c = workflowCounts(task);
    chip = `${c.donePhases}/${c.totalPhases} phases · ${c.done}/${c.total}`;
  } else {
    chip = task.tokens ? `${fmtNum(task.tokens)} tok` : STATE_LABEL[state];
  }
  const name = wf ? task.name || "Workflow" : "Subagent";
  return (
    <button type="button" onClick={onOpen}
      className="my-1.5 flex w-full items-center gap-2.5 rounded-lg border px-3 py-2 text-left text-xs outline-none transition-colors hover:bg-[color-mix(in_oklch,var(--c-agent)_8%,transparent)] focus-visible:ring-2 focus-visible:ring-accent"
      style={{ borderColor: tintBorder("var(--c-agent)", 26), background: tint("var(--c-agent)", 5) }}>
      <StateDot state={state} size={13} className="flex-none" />
      <Icon className="size-3.5 flex-none" style={{ color: "var(--c-agent)" }} />
      <span className="min-w-0 truncate font-medium" title={name}>{name}</span>
      {task.subagentType && !wf && <Chip mono className="flex-none text-faint">{task.subagentType}</Chip>}
      <span className="flex-1" />
      <span className="flex-none font-mono text-2xs tabular-nums text-faint">{chip}</span>
      <ChevronRight size={13} className="flex-none text-faint" />
    </button>
  );
}

/** The speaker badge for a turn — a small rounded avatar, with a soft pulse when it's the
 *  live streaming head. Replaces the per-row icon-node + vertical rail. */
function RoleAvatar({ role, head }: { role: "user" | "agent"; head?: boolean }) {
  const m = role === "user" ? { Icon: User, c: "var(--c-user)" } : { Icon: Leaf, c: "var(--c-agent)" };
  return (
    <span className="relative flex-none">
      <span className="relative z-10 grid size-7 place-items-center rounded-lg border"
        style={{ background: tint(m.c, 13), borderColor: tintBorder(m.c), color: m.c }}>
        <m.Icon size={15} strokeWidth={1.7} />
      </span>
      {head && <span className="absolute inset-0 z-0 rounded-lg motion-safe:animate-[terra-ring_1.7s_ease-in-out_infinite]" style={{ background: m.c }} />}
    </span>
  );
}

/** A claude.ai-style conversation turn: avatar + speaker name + body, with hover-revealed
 *  actions (copy, rewind). Generous top margin gives the thread turn-to-turn rhythm. */
function Message({ ev, agentName, head, anchor, onRewind }: {
  ev: LogEvent; agentName: string; head?: boolean;
  anchor?: string; onRewind?: (messageId: string, mode: RewindMode, editText?: string) => void;
}) {
  const reduce = useReducedMotion();
  const isUser = ev.type === "user";
  const text = str(ev.text);
  const color = isUser ? "var(--c-user)" : "var(--c-agent)";
  return (
    <motion.div className="group/msg mt-6 flex gap-3.5 first:mt-1"
      initial={reduce ? false : { opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.34, ease: [0.2, 0.7, 0.3, 1] }}>
      <RoleAvatar role={isUser ? "user" : "agent"} head={head} />
      <div className="min-w-0 flex-1">
        <div className="flex h-7 items-center gap-2">
          <span className="text-sm font-semibold" style={{ color }}>{isUser ? "You" : agentName}</span>
          <span className="flex-1" />
          <span className="flex items-center gap-1.5">
            <span className="flex items-center gap-1.5 opacity-0 transition-opacity group-hover/msg:opacity-100 focus-within:opacity-100">
              {anchor && onRewind && <RewindMenu anchor={anchor} userText={isUser ? text : undefined} onRewind={onRewind} />}
              {text && <CopyButton value={text} label="Copy message" />}
            </span>
            {ev.ts && <span className="font-mono text-2xs tabular-nums text-faint">{fmtClock(ev.ts)}</span>}
          </span>
        </div>
        {isUser ? (
          <div className="whitespace-pre-wrap rounded-xl border px-3.5 py-2.5 text-sm" style={{ background: tint("var(--c-user)", 11), borderColor: tintBorder("var(--c-user)", 24) }}>{text}</div>
        ) : (
          <Markdown className={head ? "caret-blink" : undefined}>{text}</Markdown>
        )}
      </div>
    </motion.div>
  );
}

function RewindMenu({ anchor, userText, onRewind }: { anchor: string; userText?: string; onRewind: (messageId: string, mode: RewindMode, editText?: string) => void }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button type="button" aria-label="Rewind or edit from here"
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-2xs text-faint transition-colors hover:text-text data-[state=open]:text-text">
          <History size={12} /> rewind
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onClick={() => onRewind(anchor, "conversation", userText)}>Edit this message</DropdownMenuItem>
        <DropdownMenuItem onClick={() => onRewind(anchor, "both", userText)}>Rewind + restore workspace</DropdownMenuItem>
        <DropdownMenuItem onClick={() => onRewind(anchor, "files")}>Restore workspace only</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** A non-speaker agent process event (thinking / tool / result / error / system), rendered
 *  as a quiet self-contained block indented under the agent's content column. */
function ProcessRow({ ev, head }: { ev: LogEvent; head: boolean }) {
  const reduce = useReducedMotion();
  return (
    <motion.div initial={reduce ? false : { opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: [0.2, 0.7, 0.3, 1] }}>
      <EventBody ev={ev} isHead={head} />
    </motion.div>
  );
}

function EventBody({ ev, isHead }: { ev: LogEvent; isHead: boolean }) {
  switch (ev.type) {
    case "user":
      return <div className="mt-1.5 whitespace-pre-wrap rounded-xl border p-3.5 text-sm" style={{ background: tint("var(--c-user)", 11), borderColor: tintBorder("var(--c-user)", 24) }}>{str(ev.text)}</div>;
    case "assistant_text":
      return (
        <Markdown className={isHead ? "caret-blink" : undefined}>{str(ev.text)}</Markdown>
      );
    case "thinking":
      // Default-open while it's the live head (you're watching it reason); the small Sparkles
      // glyph + "Thinking…"/"Reasoning" label distinguishes it from collapsed tool steps.
      return (
        <Collapsible defaultOpen={isHead}>
          <CollapsibleTrigger className="mt-1.5 flex items-center gap-1.5 text-xs italic text-faint hover:text-muted">
            <Sparkles size={12} className="flex-none" /> {isHead ? "Thinking…" : "Reasoning"}
          </CollapsibleTrigger>
          <CollapsibleContent className={`mt-1.5 whitespace-pre-wrap rounded-r-[10px] border-l-2 p-3 text-[13.5px] italic leading-relaxed text-muted${isHead ? " caret-blink" : ""}`} style={{ borderColor: tintBorder("var(--c-think)", 44), background: "color-mix(in oklch, var(--c-think) 7%, transparent)" }}>{str(ev.text)}</CollapsibleContent>
        </Collapsible>
      );
    case "tool_use": {
      const inputJson = JSON.stringify(ev.input, null, 2);
      return (
        <Collapsible className="group/c mt-1.5 overflow-hidden rounded-[11px] border" style={{ borderColor: tintBorder("var(--c-tool)", 24) }}>
          <div className="flex items-center gap-2 px-3 py-1.5 font-mono text-xs" style={{ background: tint("var(--c-tool)", 9) }}>
            <CollapsibleTrigger className="flex flex-1 items-center gap-2 py-0.5 text-left">
              <span className="font-semibold" style={{ color: "var(--c-tool)" }}>{str(ev.name)}</span>
              <ChevronDown size={13} className="ml-auto text-faint transition-transform group-data-[state=closed]/c:-rotate-90" />
            </CollapsibleTrigger>
            <CopyButton value={inputJson} label="Copy tool input" className="opacity-0 transition-opacity group-hover/c:opacity-100 focus-within:opacity-100" />
          </div>
          <CollapsibleContent>
            <pre className="overflow-x-auto border-t border-border bg-surface-2 p-3 font-mono text-xs leading-relaxed text-muted">{inputJson}</pre>
          </CollapsibleContent>
        </Collapsible>
      );
    }
    case "tool_result": {
      const content = str(ev.content);
      const err = ev.is_error === true;
      return (
        <Collapsible defaultOpen={err} className="group/c mt-1.5 overflow-hidden rounded-[11px] border" style={{ borderColor: err ? tintBorder("var(--c-error)", 30) : "var(--border)" }}>
          <div className="flex items-center gap-2 bg-surface-2 px-3 py-1.5 font-mono text-xs text-faint">
            <CollapsibleTrigger className="flex flex-1 items-center gap-2 py-0.5 text-left">
              <span style={{ color: err ? "var(--c-error)" : "var(--c-result)" }}>↳ {err ? "error" : "result"} · {content.length.toLocaleString()} chars</span>
              <ChevronDown size={13} className="ml-auto transition-transform group-data-[state=closed]/c:-rotate-90" />
            </CollapsibleTrigger>
            <CopyButton value={content} label="Copy result" className="opacity-0 transition-opacity group-hover/c:opacity-100 focus-within:opacity-100" />
          </div>
          <CollapsibleContent>
            <pre className="max-h-[260px] overflow-auto border-t border-border p-3 font-mono text-xs leading-relaxed text-muted">{content}</pre>
          </CollapsibleContent>
        </Collapsible>
      );
    }
    case "result": {
      // The 6-stat box duplicated the LiveScorecard and was the loudest per-turn noise. Fold it
      // into a quiet, divider-bearing turn marker (the rule doubles as the turn boundary); the
      // full chips stay one click away. Collapsed by default.
      const u = (ev.usage as Record<string, unknown>) ?? {};
      const tokens = (Number(u.input_tokens) || 0) + (Number(u.output_tokens) || 0);
      // Why the turn ended (agent SDK >= 0.2.126). "completed" is the boring case and stays
      // hidden; anything else — an interrupt, a max_turns stop — is the one thing you want to
      // see on the turn marker without expanding it.
      const why = typeof ev.terminal_reason === "string" && ev.terminal_reason !== "completed"
        ? ev.terminal_reason : null;
      return (
        <Collapsible defaultOpen={false} className="group/turn my-2">
          <CollapsibleTrigger className="flex w-full items-center gap-3 text-2xs text-faint outline-none focus-visible:ring-2 focus-visible:ring-accent">
            <span className="h-px flex-1 bg-border" />
            <span className="flex-none font-mono tabular-nums">turn · {fmtCost(Number(ev.total_cost_usd) || 0)} · {fmtNum(tokens)} tok · {fmtDuration(Number(ev.duration_ms) || 0)}</span>
            {why && (
              <span className="flex-none rounded px-1.5 py-0.5 font-medium not-italic"
                style={{ background: tint("var(--c-result)", 14), color: "var(--c-result)" }}>{why.replace(/_/g, " ")}</span>
            )}
            <ChevronDown size={12} className="flex-none transition-transform group-data-[state=open]/turn:rotate-180" />
            <span className="h-px flex-1 bg-border" />
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-1.5 flex flex-wrap gap-x-4 gap-y-2 rounded-[11px] border p-3 font-mono text-xs" style={{ background: tint("var(--c-result)", 8), borderColor: tintBorder("var(--c-result)", 22) }}>
            <Stat k="cost so far" v={fmtCost(Number(ev.total_cost_usd) || 0)} hi />
            <Stat k="in" v={fmtNum(Number(u.input_tokens) || 0)} />
            <Stat k="out" v={fmtNum(Number(u.output_tokens) || 0)} />
            <Stat k="cache r" v={fmtNum(Number(u.cache_read_input_tokens) || 0)} />
            <Stat k="cache w" v={fmtNum(Number(u.cache_creation_input_tokens) || 0)} />
            <Stat k="dur" v={fmtDuration(Number(ev.duration_ms) || 0)} />
          </CollapsibleContent>
        </Collapsible>
      );
    }
    case "error":
      return <div className="mt-1.5 rounded-[11px] border p-3 text-[13.5px]" style={{ background: tint("var(--c-error)", 10), borderColor: tintBorder("var(--c-error)", 30), color: "color-mix(in oklch, var(--c-error) 58%, var(--text))" }}>{str(ev.message)}</div>;
    // The two ways a run ends that are NOT the agent finishing. Both used to fall through to
    // the generic unknown-event row — a grey collapsed `worker_lost ›` at the foot of the
    // transcript, indistinguishable from debug noise. For unattended, budgeted agents these
    // are the whole reason you came to look: the run was killed, or the sandbox died under it.
    case "budget_exceeded": {
      const why = str(ev.reason);
      const detail = why === "cost" ? "Cumulative spend passed the hard cap."
        : why === "turns" ? "The turn count passed the runaway backstop."
        : why === "runtime" ? "A single turn ran past the wall-clock limit without finishing."
        : "A budget backstop fired.";
      return (
        <TerminalCard tone="var(--c-error)" title="Stopped — budget backstop">
          {detail}{" "}
          <span className="font-mono tabular-nums">
            spent {fmtCost(Number(ev.cost_usd) || 0)}
            {ev.cap_usd != null && <> of {fmtCost(Number(ev.cap_usd))} cap</>}
            {ev.hard_cap_usd != null && <> · hard stop {fmtCost(Number(ev.hard_cap_usd))}</>}
            {ev.result_count != null && <> · {String(ev.result_count)} turns</>}
          </span>
        </TerminalCard>
      );
    }
    case "worker_lost":
      return (
        <TerminalCard tone="var(--c-error)" title="Sandbox stopped unexpectedly">
          The agent process ended without finishing{ev.mid_turn === true ? " — mid-turn, so the last request never completed" : ""}.
          Usually the sandbox was killed (out of memory, evicted, or crashed). The transcript
          above is complete up to this point; start a new session to continue.
        </TerminalCard>
      );
    case "ready":
    case "session_start":
      return (
        <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
          <span>model <b className="font-mono text-text">{str(ev.model)}</b></span>
          <span>persona <b className="text-text">{str(ev.system_mode) || "—"}</b></span>
          {ev.title ? <span>title <b className="text-text">{str(ev.title)}</b></span> : null}
        </div>
      );
    case "system": {
      const sub = str(ev.subtype);
      if (sub === "init" || ev.data != null)
        return (
          <Collapsible className="mt-1.5">
            <CollapsibleTrigger className="text-xs text-faint hover:text-muted">{sub === "init" ? "harness composition" : sub}</CollapsibleTrigger>
            <CollapsibleContent><pre className="mt-1.5 overflow-x-auto rounded-[10px] border border-border bg-surface-2 p-3 font-mono text-xs text-muted">{JSON.stringify(ev.data, null, 2)}</pre></CollapsibleContent>
          </Collapsible>
        );
      return null;
    }
    case "status":         // transient run-state — surfaced live by the StatusPill, never in the transcript
      return null;
    case "session_end":
      return null;
    case "context_usage": {
      // Was falling through to `default` and dumping a raw JSON blob into the middle of the
      // conversation (between the agent's answer and the composer). It's a measurement — render
      // the measurement.
      const pct = Number(ev.percentage ?? 0);
      const tone = pct >= 90 ? "var(--c-error)" : pct >= 70 ? "var(--c-result)" : "var(--muted)";
      return (
        <div className="mt-1.5 flex items-center gap-2 text-2xs text-faint">
          <span className="h-1 w-24 overflow-hidden rounded-full bg-surface-2" aria-hidden>
            <span className="block h-full rounded-full" style={{ width: `${Math.min(100, Math.max(0, pct))}%`, background: tone }} />
          </span>
          <span className="tabular-nums" style={{ color: pct >= 70 ? tone : undefined }}>{pct}% of context</span>
          {ev.total_tokens != null && <span className="tabular-nums">· {Number(ev.total_tokens).toLocaleString()} tok</span>}
        </div>
      );
    }
    default:
      // An unrecognised event is a one-line note you can expand — not a wall of JSON pasted into
      // the thread. Unknown types are normal (the worker ships new ones ahead of the console).
      return (
        <Collapsible className="mt-1.5">
          <CollapsibleTrigger className="font-mono text-2xs text-faint hover:text-muted">{str(ev.type) || "event"} ›</CollapsibleTrigger>
          <CollapsibleContent><pre className="mt-1.5 overflow-x-auto rounded-[10px] border border-border bg-surface-2 p-3 font-mono text-xs text-muted">{JSON.stringify(ev, null, 2)}</pre></CollapsibleContent>
        </Collapsible>
      );
  }
}

const Stat = ({ k, v, hi }: { k: string; v: string; hi?: boolean }) => (
  <span className="inline-flex items-center gap-1.5">
    <span className="text-faint">{k}</span>
    <span className={hi ? "font-semibold text-accent" : "font-semibold text-text"}>{v}</span>
  </span>
);
