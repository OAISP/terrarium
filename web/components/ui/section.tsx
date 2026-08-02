"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * A view's top toolbar: optional helper copy on the left, controls on the
 * right. Gives Agents / Sessions / Schedules / Tokens / Egress one shared
 * rhythm under the global HUD title.
 */
export function SectionHeader({
  hint,
  children,
  className,
}: {
  hint?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-2", className)}>
      {hint != null && <p className="min-w-0 flex-1 text-sm text-muted">{hint}</p>}
      {children != null && (
        <div className={cn("flex items-center gap-2", hint == null && "ml-auto")}>{children}</div>
      )}
    </div>
  );
}

/** Token-driven elevated panel used for grouped content across views. */
export const PanelCard = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("panel-card", className)} {...props} />
  ),
);
PanelCard.displayName = "PanelCard";

/** A labelled section inside a PanelCard with a consistent title row. */
export function Panel({
  title,
  aside,
  children,
  className,
}: {
  title: React.ReactNode;
  aside?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <PanelCard className={cn("p-4", className)}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="text-sm font-medium text-text">{title}</div>
        {aside}
      </div>
      {children}
    </PanelCard>
  );
}
