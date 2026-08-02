"use client";

import * as React from "react";
import { CircleHelp, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CollapsibleCard } from "@/components/ui/collapsible-card";
import { Chip } from "@/components/ui/chip";
import { cn } from "@/lib/utils";

type Option = { label: string; description?: string };
type Question = { question: string; header?: string; multiSelect?: boolean; options: Option[] };

const OTHER = "__other__";

/**
 * Renders an agent AskUserQuestion (EV_QUESTION) as an interactive card: single- or
 * multi-select options with descriptions, plus a free-text "Other". Once answered it
 * locks and shows the chosen selection (driven by the EV_ANSWERED event, so replay /
 * reconnect stays consistent).
 */
export function QuestionCard({
  questionId,
  questions,
  answered,
  onAnswer,
}: {
  questionId: string;
  questions: Question[];
  answered?: Record<string, string | string[]>; // set → locked
  onAnswer: (questionId: string, answers: Record<string, string | string[]>) => Promise<void>;
}) {
  const locked = answered != null;
  const [sel, setSel] = React.useState<Record<number, Set<string>>>({});
  const [other, setOther] = React.useState<Record<number, string>>({});
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);

  function toggle(qi: number, label: string, multi: boolean) {
    setSel((prev) => {
      const cur = new Set(prev[qi] ?? []);
      if (multi) { if (cur.has(label)) cur.delete(label); else cur.add(label); }
      else { const had = cur.has(label); cur.clear(); if (!had) cur.add(label); }
      return { ...prev, [qi]: cur };
    });
  }

  function buildAnswers(): Record<string, string | string[]> | null {
    const out: Record<string, string | string[]> = {};
    for (let qi = 0; qi < questions.length; qi++) {
      const q = questions[qi];
      const chosen = Array.from(sel[qi] ?? []);
      const vals = chosen.flatMap((c) => (c === OTHER ? (other[qi]?.trim() ? [other[qi].trim()] : []) : [c]));
      if (vals.length === 0) return null; // each question needs an answer
      out[q.question] = q.multiSelect ? vals : vals[0];
    }
    return out;
  }

  async function submit() {
    const answers = buildAnswers();
    if (!answers) { setErr("Pick an option for each question."); return; }
    setBusy(true); setErr(null);
    try { await onAnswer(questionId, answers); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); setBusy(false); }
  }

  // Once answered, the prompt folds to a compact summary of the choices (expandable) so
  // resolved questions don't keep taking full-card space in the transcript.
  if (locked) {
    const pairs = questions.map((q) => {
      const v = answered![q.question];
      const vals = Array.isArray(v) ? v : v != null ? [String(v)] : [];
      return { label: q.header || q.question, vals };
    });
    const inline = pairs.map((p) => p.vals.join(", ")).filter(Boolean).join(" · ");
    return (
      <CollapsibleCard tone="var(--c-agent)" contentClassName="space-y-2 px-4 py-3"
        header={<>
          <Check className="size-3.5 flex-none text-agent" />
          <span className="flex-none font-medium text-agent">Answered</span>
          <span className="min-w-0 flex-1 truncate text-faint">{inline}</span>
        </>}>
        {pairs.map((p, i) => (
          <div key={i} className="text-sm">
            <span className="text-xs text-faint">{p.label}: </span>
            <span className="font-medium text-text">{p.vals.join(", ") || "—"}</span>
          </div>
        ))}
      </CollapsibleCard>
    );
  }

  return (
    <div className="my-2 rounded-lg border p-4 shadow-soft"
      style={{ borderColor: "color-mix(in oklch, var(--c-user) 32%, var(--border))", background: "color-mix(in oklch, var(--c-user) 6%, var(--surface))" }}>
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <CircleHelp className="size-4" style={{ color: "var(--c-user)" }} /> The agent needs your input
      </div>

      <div className="space-y-4">
        {questions.map((q, qi) => {
          const lockedVal = locked ? answered![q.question] : undefined;
          const lockedSet = new Set(Array.isArray(lockedVal) ? lockedVal : lockedVal != null ? [String(lockedVal)] : []);
          const chosen = sel[qi] ?? new Set<string>();
          const isOn = (label: string) => (locked ? lockedSet.has(label) : chosen.has(label));
          // free-text answers (Other) won't match a label → surface them as locked chips
          const extraLocked = locked ? [...lockedSet].filter((v) => !q.options.some((o) => o.label === v)) : [];
          return (
            <div key={qi} className="space-y-2">
              {q.header && <Chip className="eyebrow inline-block text-faint">{q.header}</Chip>}
              <div className="text-sm font-medium text-text">
                {q.question}
                {q.multiSelect && <span className="ml-1.5 text-xs font-normal text-faint">(select all that apply)</span>}
              </div>
              <div className="space-y-1.5" role={q.multiSelect ? "group" : "radiogroup"} aria-label={q.question}>
                {q.options.map((o) => {
                  const on = isOn(o.label);
                  return (
                    <button key={o.label} type="button" disabled={locked || busy} onClick={() => toggle(qi, o.label, !!q.multiSelect)}
                      role={q.multiSelect ? "checkbox" : "radio"} aria-checked={on}
                      className={cn("flex w-full items-start gap-2.5 rounded-lg border p-2.5 text-left outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default", !on && "border-border hover:border-border-2")}
                      style={on ? { borderColor: "color-mix(in oklch, var(--accent) 45%, var(--border))", background: "color-mix(in oklch, var(--accent) 12%, transparent)" } : undefined}>
                      <span className={cn("mt-0.5 grid size-4 flex-none place-items-center border", q.multiSelect ? "rounded-[5px]" : "rounded-full", on ? "border-accent bg-accent text-on-accent" : "border-border-2")}>
                        {on && <Check className="size-3" strokeWidth={3} />}
                      </span>
                      <span className="min-w-0">
                        <span className="text-sm font-medium">{o.label}</span>
                        {o.description && <span className="mt-0.5 block text-xs leading-snug text-muted">{o.description}</span>}
                      </span>
                    </button>
                  );
                })}

                {/* free-text "Other" (locked: show any free-text answers as chips) */}
                {locked ? (
                  extraLocked.map((v) => (
                    <div key={v} className="rounded-lg border p-2.5 text-sm" style={{ borderColor: "color-mix(in oklch, var(--accent) 45%, var(--border))", background: "color-mix(in oklch, var(--accent) 12%, transparent)" }}>
                      <span className="text-xs text-faint">Other: </span>{v}
                    </div>
                  ))
                ) : (
                  <div className="space-y-1.5">
                    <button type="button" disabled={busy} onClick={() => toggle(qi, OTHER, !!q.multiSelect)} role={q.multiSelect ? "checkbox" : "radio"} aria-checked={chosen.has(OTHER)}
                      className={cn("flex w-full items-center gap-2.5 rounded-lg border p-2.5 text-left text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent", !chosen.has(OTHER) && "border-border hover:border-border-2")}
                      style={chosen.has(OTHER) ? { borderColor: "color-mix(in oklch, var(--accent) 45%, var(--border))", background: "color-mix(in oklch, var(--accent) 12%, transparent)" } : undefined}>
                      <span className={cn("grid size-4 flex-none place-items-center border", q.multiSelect ? "rounded-[5px]" : "rounded-full", chosen.has(OTHER) ? "border-accent bg-accent text-on-accent" : "border-border-2")}>
                        {chosen.has(OTHER) && <Check className="size-3" strokeWidth={3} />}
                      </span>
                      Other…
                    </button>
                    {chosen.has(OTHER) && (
                      <Input autoFocus value={other[qi] ?? ""} onChange={(e) => setOther((p) => ({ ...p, [qi]: e.target.value }))} placeholder="Type your answer" />
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {err && <div className="mt-2 text-xs text-error">{err}</div>}
      {locked ? (
        <div className="mt-3 flex items-center gap-1.5 text-xs text-agent"><Check className="size-3.5" /> Answered</div>
      ) : (
        <div className="mt-3 flex justify-end">
          <Button size="sm" onClick={submit} disabled={busy}>{busy ? "Sending…" : "Submit answer"}</Button>
        </div>
      )}
    </div>
  );
}
