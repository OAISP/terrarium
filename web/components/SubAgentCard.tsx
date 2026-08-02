"use client";

import * as React from "react";
import { Bot, ChevronDown } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { StateDot, TokenMeter, ActivityCard } from "@/components/ui/activity";
import { Chip } from "@/components/ui/chip";
import { Markdown } from "@/components/ui/markdown";
import { activityState } from "@/lib/tint";
import { fmtDuration } from "@/lib/format";

export type SubAgentTask = {
  subagentType?: string;
  description?: string;
  model?: string;
  tokens?: number;
  toolCalls?: number;
  lastTool?: string;
  lastSummary?: string;
  durationMs?: number;
};

/**
 * A single subagent run (the Claude Code `Agent` tool) — its type + task, a persistent
 * activity summary (tokens · tools · last tool, kept after completion), and its final
 * output as markdown, folded into one collapsible card.
 */
export function SubAgentCard({ task, output, isError, live, detached }: { task: SubAgentTask; output?: string; isError?: boolean; live: boolean; detached?: boolean }) {
  const done = output != null || !live;
  const [open, setOpen] = React.useState(false);
  // Detached = the session went idle while this background subagent was still "live" and it
  // never reported any activity (no tool, no tokens) — stranded, not running. Scoped tightly
  // so a progressing background subagent still shows "running".
  const stalled = !!detached && live && !isError && !task.lastTool && !task.tokens;
  const state = stalled ? "detached" : activityState(undefined, { live, isError });
  const activity = task.lastSummary || task.lastTool;

  return (
    <ActivityCard tone="var(--c-agent)" className="p-0">
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger className="block w-full px-3.5 py-2.5 text-left outline-none transition-colors hover:bg-[color-mix(in_oklch,var(--c-agent)_8%,transparent)] focus-visible:ring-2 focus-visible:ring-accent">
          {/* identity + state — metrics get their own row so the name/type never get squeezed */}
          <div className="flex items-center gap-2">
            <Bot className="size-4 flex-none" style={{ color: "var(--accent)" }} />
            <span className="flex-none text-sm font-semibold">Subagent</span>
            {task.subagentType && <Chip mono className="min-w-0 truncate text-faint">{task.subagentType}</Chip>}
            <StateDot state={state} size={14} className="ml-auto flex-none" />
            {done && <ChevronDown size={13} className="flex-none text-faint transition-transform" style={{ transform: open ? "rotate(180deg)" : "" }} />}
          </div>
          {task.description && <div className="mt-1.5 truncate text-xs text-muted">{task.description}</div>}
          {(task.tokens || task.toolCalls || (done && task.durationMs) || activity) && (
            <div className="mt-1.5 flex items-center gap-2.5 font-mono text-2xs text-faint">
              {done && task.durationMs ? <span className="flex-none tabular-nums">{fmtDuration(task.durationMs)}</span> : null}
              <TokenMeter tokens={task.tokens} tools={task.toolCalls} className="flex-none" />
              {activity && <span className="min-w-0 flex-1 truncate">{activity}</span>}
            </div>
          )}
        </CollapsibleTrigger>
        {done && (
          <CollapsibleContent className="border-t px-3.5 py-2.5" style={{ borderColor: "color-mix(in oklch, var(--c-agent) 18%, var(--border))" }}>
            {output ? <Markdown>{output}</Markdown> : <span className="text-xs text-faint">No output.</span>}
          </CollapsibleContent>
        )}
      </Collapsible>
    </ActivityCard>
  );
}
