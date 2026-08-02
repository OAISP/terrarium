"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * The "icon tile · title + meta · actions" row used by Schedules / Tokens / Egress
 * profiles — one component so padding, radius and hover stay identical (they had
 * drifted: p-3.5 vs p-4, rounded-lg vs rounded-xl).
 */
export function ListRow({
  icon: Icon,
  iconTone,
  title,
  badges,
  subtitle,
  actions,
  className,
}: {
  icon?: React.ElementType;
  iconTone?: string; // color var → tinted tile; omitted → neutral
  title: React.ReactNode;
  badges?: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "group flex items-start gap-3 rounded-lg border border-border bg-surface p-3.5 shadow-soft transition-[border-color] [transition-timing-function:var(--ease)] hover:border-border-2",
        className,
      )}
    >
      {Icon && (
        <div
          className={cn("grid size-9 flex-none place-items-center rounded-lg", !iconTone && "bg-surface-2 text-muted")}
          style={iconTone ? { background: `color-mix(in oklch, ${iconTone} 14%, transparent)`, color: iconTone } : undefined}
        >
          <Icon size={17} strokeWidth={1.8} />
        </div>
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-base font-semibold">{title}</span>
          {badges}
        </div>
        {subtitle != null && <div className="mt-0.5 text-xs text-muted">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-none items-center gap-1">{actions}</div>}
    </div>
  );
}
