"use client";

import * as React from "react";
import { AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/misc";

/**
 * Cross-cutting feedback primitives shared by every view so error / empty /
 * loading treatments stay identical across the console.
 */

/** Recoverable inline error — one consistent treatment everywhere. */
export function ErrorBox({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div
      role="alert"
      className={cn("flex items-start gap-2 rounded-lg border p-3 text-sm", className)}
      style={{
        background: "color-mix(in oklch, var(--c-error) 10%, transparent)",
        borderColor: "color-mix(in oklch, var(--c-error) 30%, var(--border))",
        color: "var(--c-error)",
      }}
    >
      <AlertTriangle className="mt-px size-4 flex-none" />
      <span className="min-w-0">{children}</span>
    </div>
  );
}

/** Teaching empty state — icon tile, title, helper copy, optional CTA. */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon: React.ElementType;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-border px-6 py-16 text-center",
        className,
      )}
    >
      <div className="grid size-12 place-items-center rounded-xl bg-surface-2 text-muted">
        <Icon className="size-6" strokeWidth={1.7} />
      </div>
      <div className="space-y-1">
        <div className="font-medium text-text">{title}</div>
        {description && <div className="mx-auto max-w-sm text-sm text-muted">{description}</div>}
      </div>
      {action}
    </div>
  );
}

/**
 * One wrapper for the load → error → empty → content ladder, so every list view
 * shows the SAME treatment (shimmer skeleton, ErrorBox, EmptyState) instead of each
 * hand-wiring its own (some skeleton, some spinner, some nothing).
 */
export function ResourceState({
  loading,
  error,
  isEmpty,
  empty,
  skeleton,
  children,
}: {
  loading: boolean;
  error?: string | null;
  isEmpty?: boolean;
  empty?: React.ReactNode;
  skeleton?: React.ReactNode;
  children: React.ReactNode;
}) {
  if (error) return <ErrorBox>{error}</ErrorBox>;
  if (loading) return <>{skeleton ?? <ListSkeleton />}</>;
  if (isEmpty) return <>{empty}</>;
  return <>{children}</>;
}

/** A column of shimmer rows — replaces ad-hoc spinners while lists load. */
export function ListSkeleton({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("space-y-2", className)} aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-[58px]" />
      ))}
    </div>
  );
}
