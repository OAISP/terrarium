"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import type { TokenTotals } from "@/lib/types";

/** A small uppercase field/section heading — the one `.eyebrow` recipe behind the ~6
 *  hand-rolled `text-2xs font-medium uppercase tracking-wide text-faint` labels. */
export function FieldLabel({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("eyebrow", className)}>{children}</div>;
}

/** The 4 token buckets + their colors — single source for every token visualization. */
export const TOKEN_ROWS: { key: keyof TokenTotals; label: string; color: string }[] = [
  { key: "input", label: "Input", color: "var(--c-user)" },
  { key: "output", label: "Output", color: "var(--c-agent)" },
  { key: "cacheRead", label: "Cache read", color: "var(--c-result)" },
  { key: "cacheCreate", label: "Cache create", color: "var(--c-tool)" },
  { key: "subagent", label: "Sub-agents", color: "var(--accent)" },
];

/** KPI / metric tile — shared `eyebrow` label + `text-stat` scale (was hand-rolled
 *  with text-[28px]/text-[22px] literals in UsageView and LiveScorecard). */
export function StatCard({
  label,
  icon: Icon,
  color,
  value,
  hint,
  size = "kpi",
  className,
}: {
  label: string;
  icon?: React.ElementType;
  color?: string;
  value: React.ReactNode;
  /** One line of context under the figure — e.g. the all-time total behind a windowed one.
   *  A windowed stat is ambiguous on its own ("is $4 everything, or just this week?"). */
  hint?: React.ReactNode;
  size?: "kpi" | "tile";
  className?: string;
}) {
  return (
    <div className={cn("relative overflow-hidden rounded-lg border border-border bg-surface shadow-soft", size === "kpi" ? "p-4" : "p-3", className)}>
      {color && <span className="absolute inset-x-0 top-0 h-0.5" style={{ background: color }} aria-hidden />}
      <div className="eyebrow flex items-center gap-2">
        {Icon && <Icon className="size-3.5" style={color ? { color } : undefined} />}
        {label}
      </div>
      <div className={cn("mt-1.5 font-bold tabular-nums tracking-tight", size === "kpi" ? "text-stat" : "text-xl")}>{value}</div>
      {hint && <div className="mt-0.5 text-2xs text-faint">{hint}</div>}
    </div>
  );
}

/** Stacked token-composition bar + legend — shared by UsageView and LiveScorecard
 *  (the bar math + legend were duplicated almost verbatim in both). */
export function TokenBar({
  tokens,
  format,
  showPercent,
  barClassName,
}: {
  tokens: TokenTotals;
  format: (n: number) => string;
  showPercent?: boolean;
  barClassName?: string;
}) {
  const total = TOKEN_ROWS.reduce((s, r) => s + (tokens[r.key] || 0), 0) || 1;
  return (
    <div>
      <div className={cn("mb-3 flex overflow-hidden rounded-md bg-surface-3", barClassName ?? "h-2.5")}>
        {TOKEN_ROWS.map((r) => (
          <div key={r.key} className="h-full transition-[width] duration-500" style={{ width: `${(tokens[r.key] / total) * 100}%`, background: r.color }} />
        ))}
      </div>
      <div className={showPercent ? "grid grid-cols-2 gap-2" : "flex flex-col gap-2"}>
        {TOKEN_ROWS.map((r) => (
          <div key={r.key} className="flex items-center gap-2 text-xs">
            <span className="h-2 w-2 flex-none rounded-sm" style={{ background: r.color }} />
            <span className="flex-1 text-muted">{r.label}</span>
            <span className="font-mono text-text">{format(tokens[r.key])}</span>
            {showPercent && <span className="w-9 text-right font-mono text-faint">{Math.round((tokens[r.key] / total) * 100)}%</span>}
          </div>
        ))}
      </div>
    </div>
  );
}
