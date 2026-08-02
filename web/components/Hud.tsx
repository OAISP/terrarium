"use client";

import * as React from "react";
import { Command, Wifi, WifiOff, Loader2 } from "lucide-react";
import type { Health } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CredentialBadge } from "@/components/CredentialBadge";
import { KillSwitch } from "@/components/KillSwitch";

export function RunnerBadge({ health }: { health: Health | null }) {
  const ok = !!health?.ok;
  const color = ok ? "var(--c-agent)" : "var(--c-error)";
  return (
    <div
      // Never display:none a STATE indicator at a breakpoint. This was `hidden sm:flex`, so below
      // 640px the orchestrator online/offline badge left the DOM entirely — for the operator most
      // likely to be checking a long run from a phone. Degrade it (the image tag already drops at
      // md), don't delete it.
      className="flex items-center gap-2 rounded-lg border px-2 py-1.5 sm:px-2.5"
      style={{ background: `color-mix(in oklch, ${color} 10%, var(--surface-2))`, borderColor: `color-mix(in oklch, ${color} 24%, var(--border))` }}
      title={ok ? `runner: ${health?.runner} · image: ${health?.image}` : "orchestrator unreachable"}
    >
      <span className="h-1.5 w-1.5 rounded-full motion-safe:animate-[terra-breathe_1.8s_ease-in-out_infinite]" style={{ background: color }} />
      <span className="font-mono text-2xs font-semibold uppercase tracking-wide" style={{ color }}>{ok ? health?.runner ?? "online" : "offline"}</span>
      {ok && health?.image && <span className="hidden font-mono text-2xs text-muted md:inline">{health.image.split("/").pop()}</span>}
    </div>
  );
}

const CONN = {
  connecting: { label: "Connecting", icon: Loader2, color: "var(--muted)", spin: true },
  open: { label: "Stream live", icon: Wifi, color: "var(--c-agent)", spin: false },
  error: { label: "Reconnecting", icon: WifiOff, color: "var(--c-result)", spin: false },
  closed: { label: "Offline", icon: WifiOff, color: "var(--muted)", spin: false },
} as const;

export function ConnectionPill({ conn, retry, onRetry }: { conn: keyof typeof CONN; retry?: { attempt: number; at: number } | null; onRetry?: () => void }) {
  const c = CONN[conn];
  // Healthy stream recedes to a single breathing dot — the labeled pill is reserved for the
  // states you actually need to read (connecting / reconnecting / offline).
  if (conn === "open") {
    // role="status" + sr-only text, NOT aria-label on a bare span: aria-label is name-prohibited on
    // a generic role, so assistive tech ignored it and the state was conveyed ONLY by a dot that
    // prefers-reduced-motion freezes. Two independent failures on one pixel.
    return (
      <span role="status" className="grid size-7 flex-none place-items-center" title={c.label}>
        <span className="size-2 rounded-full motion-safe:animate-[terra-breathe_1.8s_ease-in-out_infinite]" style={{ background: c.color }} />
        <span className="sr-only">{c.label}</span>
      </span>
    );
  }
  const Icon = c.icon;
  return (
    <div role="status" className="flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-2xs font-medium"
      style={{ color: c.color, background: `color-mix(in oklch, ${c.color} 12%, transparent)`, borderColor: `color-mix(in oklch, ${c.color} 26%, var(--border))` }}>
      <Icon className={cn("size-3.5", c.spin && "animate-spin")} />{c.label}
      {/* Show the attempt count: without it a connection that blipped once and one that has
          been retrying for an hour look identical. The button escapes the backoff. */}
      {conn === "error" && retry && retry.attempt > 1 && <span className="tabular-nums opacity-80">· try {retry.attempt}</span>}
      {conn === "error" && onRetry && (
        <button type="button" onClick={onRetry} className="ml-0.5 rounded px-1 underline underline-offset-2 outline-none hover:opacity-80">
          Retry now
        </button>
      )}
    </div>
  );
}

export function PaletteButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      aria-label="Open command palette"
      className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 px-2.5 py-1.5 text-2xs text-muted transition-colors hover:border-border-2 hover:text-text"
    >
      <Command className="size-3.5" /> <kbd className="font-mono tracking-tight">⌘K</kbd>
    </button>
  );
}

export function Hud<V extends string>({ title, subtitle, health, onPalette, tabs, activeTab, onTab }: {
  title: string; subtitle?: string; health: Health | null; onPalette: () => void;
  // Sub-views (Usage under Sessions, Schedules under Agents) surface here instead of costing a
  // permanent rail slot each. Each keeps its own URL, so they stay linkable.
  tabs?: { id: V; label: string }[]; activeTab?: V; onTab?: (v: V) => void;
}) {
  return (
    <header className="flex flex-wrap items-center gap-2 sm:gap-3 rounded-xl border border-border bg-panel px-3 py-3 shadow-soft sm:px-[18px]">
      <div className="min-w-0 flex-1">
        <h1 className="truncate text-lg font-bold tracking-tight">{title}</h1>
        {subtitle && <p className="truncate text-xs text-muted">{subtitle}</p>}
      </div>
      {tabs && onTab && (
        <div className="order-last flex w-full items-center gap-1 overflow-x-auto rounded-lg border border-border bg-surface-2 p-1 sm:order-none sm:w-auto">
          {tabs.map((t) => {
            const on = t.id === activeTab;
            return (
              <button key={t.id} type="button" onClick={() => onTab(t.id)} aria-current={on ? "page" : undefined}
                className="rounded-md px-2.5 py-1 text-2xs font-medium transition-colors"
                style={on
                  ? { background: "color-mix(in oklch, var(--accent) 18%, transparent)", color: "var(--accent)" }
                  : { color: "var(--muted)" }}>
                {t.label}
              </button>
            );
          })}
        </div>
      )}
      {/* The kill switch lived inside the Egress page: to cut every agent's network you first had
          to navigate to the one page that could do it. A panic button you have to go and find isn't
          one. It sits with ⌘K now — reachable from every view. */}
      <KillSwitch />
      <PaletteButton onClick={onPalette} />
      <div className="hidden h-5 w-px bg-border sm:block" aria-hidden />
      <CredentialBadge />
      <RunnerBadge health={health} />
    </header>
  );
}
