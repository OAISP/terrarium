"use client";

import { Command } from "cmdk";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Box, Activity, BarChart3, Plus, Moon, Sun, Search, Clock, ShieldHalf, ScrollText, SlidersHorizontal, Ban, Play, Boxes, KeyRound, Network } from "lucide-react";
import { useTheme } from "next-themes";
import type { AgentSpec, SessionSummary } from "@/lib/types";
import { StatusPill } from "./StatusPill";
import type { View } from "./Dock";

// One source for the palette's destinations. Kept beside the dock's own list intentionally: the
// palette also reaches the sub-views (Usage/Schedules) and Settings, which are not rail slots.
const DESTINATIONS: { id: View; label: string; Icon: React.ElementType }[] = [
  { id: "sessions", label: "Sessions", Icon: Activity },
  { id: "agents", label: "Agents", Icon: Box },
  { id: "boundary", label: "Boundary", Icon: ShieldHalf },
  { id: "environments", label: "Environments", Icon: Boxes },
  { id: "egress", label: "Egress", Icon: Network },
  { id: "secrets", label: "Secrets", Icon: KeyRound },
  { id: "logs", label: "Logs", Icon: ScrollText },
  { id: "usage", label: "Usage", Icon: BarChart3 },
  { id: "schedules", label: "Schedules", Icon: Clock },
  { id: "settings", label: "Settings", Icon: SlidersHorizontal },
];

export function CommandPalette({ open, onOpenChange, agents, sessions, onNavigate, onOpenSession, onNewAgent, onNewSession, onLaunchAgent, onFreezeEgress }: {
  open: boolean; onOpenChange: (o: boolean) => void;
  agents: AgentSpec[]; sessions: SessionSummary[];
  onNavigate: (v: View) => void; onOpenSession: (id: string) => void; onNewAgent: () => void;
  onNewSession: () => void; onLaunchAgent: (id: string) => void; onFreezeEgress: () => void;
}) {
  const { resolvedTheme, setTheme } = useTheme();
  const run = (fn: () => void) => { onOpenChange(false); fn(); };

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-[oklch(0_0_0/0.5)] backdrop-blur-[2px] data-[state=open]:animate-[terra-in_0.2s_ease]" />
        <DialogPrimitive.Content className="fixed left-1/2 top-[16%] z-50 w-[92vw] max-w-xl -translate-x-1/2 overflow-hidden rounded-xl border border-border bg-panel shadow-pop data-[state=open]:animate-[terra-in_0.22s_ease]">
          <DialogPrimitive.Title className="sr-only">Command palette</DialogPrimitive.Title>
          <Command className="[&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:text-faint">
            <div className="flex items-center gap-2 border-b border-border px-3">
              <Search className="size-4 text-faint" />
              <Command.Input autoFocus placeholder="Search agents, sessions, actions…" className="h-12 w-full bg-transparent text-sm text-text outline-none placeholder:text-faint" />
            </div>
            <Command.List className="max-h-[58vh] overflow-y-auto p-2">
              <Command.Empty className="py-8 text-center text-sm text-muted">No results.</Command.Empty>
              {/* Generated from the dock's own list, never hand-maintained — a second copy of
                  the destinations drifts, and the palette is where you'd never notice. */}
              <Command.Group heading="Navigate">
                {DESTINATIONS.map(({ id, label, Icon }) => (
                  <Item key={id} value={`nav ${label.toLowerCase()}`} onSelect={() => run(() => onNavigate(id))}>
                    <Icon className="size-4" /> {label}
                  </Item>
                ))}
              </Command.Group>
              {/* Verbs, not just destinations. "New session" — the console's entire purpose — was
                  missing, as was the egress kill switch; the only two actions were New agent and
                  Toggle theme. */}
              <Command.Group heading="Actions">
                <Item value="new session run agent" onSelect={() => run(onNewSession)}><Plus className="size-4" /> New session</Item>
                <Item value="new agent create" onSelect={() => run(onNewAgent)}><Plus className="size-4" /> New agent</Item>
                <Item value="freeze egress kill switch panic" onSelect={() => run(onFreezeEgress)}>
                  <Ban className="size-4" style={{ color: "var(--c-error)" }} /> Freeze egress
                  <span className="ml-auto text-2xs text-faint">kill switch</span>
                </Item>
                <Item value="toggle theme dark light" onSelect={() => run(() => setTheme(resolvedTheme === "dark" ? "light" : "dark"))}>
                  {resolvedTheme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />} Toggle theme
                </Item>
              </Command.Group>
              {agents.length > 0 && (
                <Command.Group heading="Launch an agent">
                  {agents.map((a) => (
                    // Objects OPEN: selecting an agent launches a session against it, rather than
                    // navigating to the grid it lives on with nothing selected.
                    <Item key={a.id} value={`agent ${a.name} ${a.id} launch run`} onSelect={() => run(() => onLaunchAgent(a.id))}>
                      <Play className="size-4" style={{ color: "var(--c-agent)" }} /> {a.name}
                      <span className="ml-auto font-mono text-xs text-faint">{a.harness.model}</span>
                    </Item>
                  ))}
                </Command.Group>
              )}
              {sessions.length > 0 && (
                <Command.Group heading="Open session">
                  {sessions.slice(0, 8).map((s) => (
                    <Item key={s.id} value={`session ${s.title ?? ""} ${s.id}`} onSelect={() => run(() => onOpenSession(s.id))}>
                      <Activity className="size-4 flex-none text-accent" /> <span className="min-w-0 truncate">{s.title || s.id}</span>
                      <span className="ml-auto flex-none"><StatusPill status={s.status} /></span>
                    </Item>
                  ))}
                </Command.Group>
              )}
            </Command.List>
          </Command>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

// DOM focus never leaves the palette input, so the selected state below IS the keyboard focus
// indicator. bg-surface-2 on bg-panel is 1.12:1 — invisible (WCAG 1.4.11/2.4.7). No fill reaches
// 3:1 without becoming a different surface, so carry the state on an inset accent bar (9.54:1 vs
// panel) and let the fill stay decorative.
function Item({ children, onSelect, value }: { children: React.ReactNode; onSelect: () => void; value?: string }) {
  return (
    <Command.Item value={value} onSelect={onSelect} className="flex cursor-pointer items-center gap-2.5 rounded-md px-3 py-2 text-sm text-text outline-none data-[selected=true]:bg-surface-2 data-[selected=true]:shadow-[inset_2px_0_0_var(--accent)]">
      {children}
    </Command.Item>
  );
}
