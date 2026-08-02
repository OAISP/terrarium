"use client";

import * as React from "react";
import { Loader2, Check, AlertTriangle, Clock, Unplug } from "lucide-react";
import { cn } from "@/lib/utils";
import { fmtNum } from "@/lib/format";
import { tint, tintBorder, type ActivityState, STATE_TONE } from "@/lib/tint";

/** A drill-down target for the detail drawer — one subagent's summary (+ optional output). */
export type InspectTarget = {
  title: string;
  subtitle?: string;
  badge?: string;
  state: ActivityState;
  tokens?: number;
  tools?: number;
  activity?: string;
  phase?: string;
  task?: string;
  model?: string;
  durationMs?: number;
  timeline?: { tool: string; summary?: string; last?: boolean }[];
  output?: string;
  error?: string;
};

const STATE_ICON: Record<ActivityState, React.ElementType> = {
  queued: Clock,
  running: Loader2,
  done: Check,
  error: AlertTriangle,
  detached: Unplug,
};

/** The status atom shared by every agent-activity surface: one dot whose icon+color is the
 *  single source of truth for queued / running / done / error. */
export function StateDot({ state, size = 14, className }: { state: ActivityState; size?: number; className?: string }) {
  const Icon = STATE_ICON[state];
  return <Icon className={cn(state === "running" && "animate-spin", className)} style={{ width: size, height: size, color: STATE_TONE[state] }} />;
}

/** A compact token (and optional tool-count) chip — tabular, muted. Replaces the three
 *  hand-written `fmtNum(tokens) + " tok"` spans across the cards. */
export function TokenMeter({ tokens, tools, className }: { tokens?: number; tools?: number; className?: string }) {
  if (!tokens && !tools) return null;
  return (
    <span className={cn("flex items-center gap-2 font-mono text-2xs tabular-nums text-muted", className)}>
      {tokens != null && tokens > 0 && <span>{fmtNum(tokens)} tok</span>}
      {tools != null && tools > 0 && <span className="hidden text-faint sm:inline">{tools} {tools === 1 ? "tool" : "tools"}</span>}
    </span>
  );
}

/** Shared card chrome (radius / padding / tinted background + border / soft shadow),
 *  parameterized by a single `tone`. Question / Permission / Workflow / SubAgent all wrap
 *  this so the five bespoke tints collapse to one consistent scale. */
export function ActivityCard({ tone = "var(--accent)", className, children, ...rest }: React.HTMLAttributes<HTMLDivElement> & { tone?: string }) {
  return (
    <div className={cn("my-2 rounded-xl border p-4 shadow-soft", className)}
      style={{ borderColor: tintBorder(tone, 30), background: tint(tone, 6) }} {...rest}>
      {children}
    </div>
  );
}
