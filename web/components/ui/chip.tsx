"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import { tint, tintBorder } from "@/lib/tint";

/**
 * A compact inline badge — the one recipe behind the ~9 hand-rolled
 * `rounded px-1.5 py-0.5 text-2xs` chips across the cards (counts, sub-agent type,
 * model, tool name…). No tone → neutral `bg-surface-2 text-muted`; pass `tone` for a
 * tinted (color/bg/border) badge, `mono` for monospaced content. Extra classes (e.g.
 * `flex-none`, `text-faint`, `rounded-md`) override the defaults via twMerge.
 */
export function Chip({
  tone,
  mono,
  className,
  children,
  ...rest
}: React.HTMLAttributes<HTMLSpanElement> & { tone?: string; mono?: boolean }) {
  return (
    <span
      className={cn("rounded px-1.5 py-0.5 text-2xs tabular-nums", mono && "font-mono", !tone && "bg-surface-2 text-muted", className)}
      style={tone ? { color: tone, background: tint(tone, 12), border: `1px solid ${tintBorder(tone, 28)}` } : undefined}
      {...rest}
    >
      {children}
    </span>
  );
}
