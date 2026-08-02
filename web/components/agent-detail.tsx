"use client";

import * as React from "react";
import { Sheet, SheetContent, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import { StateDot, TokenMeter, type InspectTarget } from "@/components/ui/activity";
import { Chip } from "@/components/ui/chip";
import { FieldLabel } from "@/components/ui/stat";
import { Markdown } from "@/components/ui/markdown";
import { STATE_LABEL, tint, tintBorder } from "@/lib/tint";
import { fmtDuration } from "@/lib/format";

/** A subagent's tool trail: the sequence of tools it ran (each with an optional one-line
 *  target), the latest marked. Falls back to a single row built from the live `activity`. */
function ActivityTimeline({ timeline, activity }: { timeline?: { tool: string; summary?: string; last?: boolean }[]; activity?: string }) {
  const rows = timeline?.length ? timeline : activity ? [{ tool: "tool", summary: activity, last: true }] : [];
  if (!rows.length) return null;
  return (
    <div className="text-xs">
      <FieldLabel className="mb-1">Activity</FieldLabel>
      <div className="space-y-1 rounded-lg border border-border bg-surface-2 px-3 py-2">
        {rows.map((r, i) => (
          <div key={i} className="flex items-center gap-2 font-mono">
            <span className="size-1.5 flex-none rounded-full" style={{ background: r.last ? "var(--accent)" : "var(--faint)" }} />
            <span className="flex-none font-medium text-muted">{r.tool}</span>
            {r.summary && <span className="min-w-0 flex-1 truncate text-faint" title={r.summary}>{r.summary}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Right-side drawer showing one subagent's detail (summary stats + activity + any output),
 *  opened by clicking an agent row in a workflow. Keeps the thread's scroll position stable. */
export function DetailSheet({ target, onClose }: { target: InspectTarget | null; onClose: () => void }) {
  return (
    <Sheet open={!!target} onOpenChange={(o) => { if (!o) onClose(); }}>
      <SheetContent aria-describedby={undefined}>
        {target && (
          <>
            <div className="flex items-center gap-2.5 pr-8">
              <StateDot state={target.state} size={16} />
              <SheetTitle className="min-w-0 truncate">{target.title}</SheetTitle>
              {target.badge && <Chip mono className="flex-none text-faint">{target.badge}</Chip>}
            </div>
            {target.subtitle && <SheetDescription className="-mt-2">{target.subtitle}</SheetDescription>}
            {target.phase && (
              <span className="inline-flex w-fit items-center gap-1.5 rounded-md border border-border bg-surface-2 px-2 py-0.5 text-2xs font-medium text-muted">
                <span className="text-faint">phase</span>{target.phase}
              </span>
            )}
            {target.task && (
              <div className="text-xs">
                <FieldLabel className="mb-1">Task</FieldLabel>
                <div className="whitespace-pre-wrap rounded-lg border border-border bg-surface-2 px-3 py-2 leading-relaxed text-muted">{target.task}</div>
              </div>
            )}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-lg border border-border bg-surface px-3 py-2.5 text-xs">
              <span className="inline-flex items-center gap-1.5"><span className="text-faint">state</span><span className="font-medium" style={{ color: "var(--text)" }}>{STATE_LABEL[target.state]}</span></span>
              {target.model && <span className="inline-flex items-center gap-1.5"><span className="text-faint">model</span><span className="font-mono text-2xs text-muted">{target.model}</span></span>}
              {target.durationMs != null && target.durationMs > 0 && <span className="inline-flex items-center gap-1.5"><span className="text-faint">dur</span><span className="font-mono tabular-nums text-muted">{fmtDuration(target.durationMs)}</span></span>}
              <TokenMeter tokens={target.tokens} tools={target.tools} />
            </div>
            <ActivityTimeline timeline={target.timeline} activity={target.activity} />
            {target.error && (
              <div className="text-xs">
                <FieldLabel className="mb-1">Error</FieldLabel>
                <div className="whitespace-pre-wrap rounded-lg border px-3 py-2 font-mono" style={{ borderColor: tintBorder("var(--c-error)", 30), background: tint("var(--c-error)", 10), color: "color-mix(in oklch, var(--c-error) 58%, var(--text))" }}>{target.error}</div>
              </div>
            )}
            {target.output != null && (
              <div>
                <FieldLabel className="mb-1.5">Output</FieldLabel>
                <Markdown>{target.output}</Markdown>
              </div>
            )}
            {!target.task && !target.timeline?.length && target.output == null && (
              <p className="text-xs text-faint">No further detail captured for this agent. Only its live progress summary is available.</p>
            )}
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
