"use client";

import { useEffect, useRef, useState } from "react";
import { Box, Activity, Sun, Moon, LogOut, ShieldHalf, ScrollText, SlidersHorizontal } from "lucide-react";
import { useTheme } from "next-themes";
import { LogoBadge } from "./Logo";

// Every view keeps its own URL; these four are the primary DESTINATIONS. A rail slot costs
// the same whether its view holds one row or a thousand, so the thin ones fold into the
// destination they belong to: Usage is a rollup of the sessions list, Schedules are agents on
// cron, Secrets + Environments + Egress are one concept (Boundary), Tokens are config
// (Settings).
export type View =
  | "sessions" | "agents" | "boundary" | "logs" | "settings"
  // sub-views: addressable, but they highlight their parent destination in the rail
  | "usage" | "schedules" | "environments" | "egress" | "secrets";

const ITEMS: { id: View; label: string; Icon: React.ElementType }[] = [
  { id: "sessions", label: "Sessions", Icon: Activity },
  { id: "agents", label: "Agents", Icon: Box },
  { id: "boundary", label: "Boundary", Icon: ShieldHalf },
  { id: "logs", label: "Logs", Icon: ScrollText },
];

// A sub-view lights up its parent icon, so the rail always shows where you are.
// Boundary is one destination made of three tabs rather than one long scroll: stacking Egress +
// Environments + Secrets end-to-end merged the nav cost but not the reading cost — it just made one
// very long, very dense page. Tabs keep the 4-slot rail AND give each part its own address back.
export const PARENT: Partial<Record<View, View>> = {
  usage: "sessions",
  schedules: "agents",
  environments: "boundary", egress: "boundary", secrets: "boundary",
};

// Clicking a destination that is only a tab group lands on its first tab.
export const DEFAULT_TAB: Partial<Record<View, View>> = { boundary: "environments" };

export function Dock({ view, onNavigate, runningCount, onLogout }: { view: View; onNavigate: (v: View) => void; runningCount: number; onLogout?: () => void }) {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  const navRef = useRef<HTMLDivElement>(null);
  // Cache the item elements + their vertical centers once on pointer-enter, so the per-move
  // magnification reads from memory instead of forcing a layout (getBoundingClientRect) per
  // mousemove — the reflow was the only cost in the hot path.
  const itemsRef = useRef<HTMLElement[]>([]);
  const centersRef = useRef<number[]>([]);
  // The next-themes hydration guard: the resolved theme is unknown on the server, so the icon
  // must not render until after mount or client and server markup disagree.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => setMounted(true), []);

  function cache() {
    itemsRef.current = navRef.current ? [...navRef.current.querySelectorAll<HTMLElement>("[data-dock]")] : [];
    centersRef.current = itemsRef.current.map((it) => { const r = it.getBoundingClientRect(); return r.top + r.height / 2; });
  }
  function onMove(e: React.MouseEvent) {
    itemsRef.current.forEach((it, i) => {
      const dist = Math.abs(e.clientY - centersRef.current[i]);
      // gentle magnification, capped at 1.12 per the design inventory
      it.style.transform = `scale(${1 + 0.12 * Math.max(0, 1 - dist / 96)})`;
    });
  }
  const reset = () => itemsRef.current.forEach((it) => (it.style.transform = "scale(1)"));

  // The console's PRIMARY navigation, so it must be a LABELLED <nav> landmark: an <aside>
  // (role=complementary) or an unlabelled <nav> gives screen-reader users no way to jump here.
  return (
    <nav aria-label="Primary" className="flex w-full flex-none flex-row items-center gap-2 overflow-x-auto rounded-xl border border-border bg-panel px-2 py-2 shadow-soft md:w-16 md:flex-col md:gap-3.5 md:overflow-visible md:px-0 md:py-3.5">
      <LogoBadge size={38} className="hidden shadow-soft md:block" />
      <div className="hidden h-px w-6 bg-border md:block" />

      <div ref={navRef} onMouseEnter={cache} onMouseMove={onMove} onMouseLeave={reset} className="flex flex-row items-center gap-2 md:flex-col md:gap-3 md:py-1">
        {ITEMS.map(({ id, label, Icon }) => {
          // /usage lights Sessions, /schedules lights Agents — the rail always shows where you are.
          const active = view === id || PARENT[view] === id;
          return (
            <button
              key={id}
              data-dock
              title={label}
              // aria-label OVERRIDES inner content, so the running-count badge below is not
              // announced unless the count is folded into the label itself.
              aria-label={id === "sessions" && runningCount > 0 ? `${label}, ${runningCount} running` : label}
              aria-current={active ? "page" : undefined}
              onClick={() => onNavigate(id)}
              className="relative grid h-[42px] w-[42px] origin-center place-items-center rounded-xl border transition-[transform,color,background,border-color] duration-150 motion-safe:[transition-timing-function:var(--ease)]"
              style={{
                borderColor: active ? "color-mix(in oklch, var(--accent) 40%, var(--border))" : "var(--border)",
                background: active ? "color-mix(in oklch, var(--accent) 18%, transparent)" : "var(--surface-2)",
                color: active ? "var(--accent)" : "var(--muted)",
              }}
            >
              {/* active indicator rail */}
              {active && <span className="absolute -bottom-[7px] h-[3px] w-5 rounded-full bg-accent md:-left-[10px] md:bottom-auto md:h-5 md:w-[3px]" aria-hidden />}
              <Icon size={20} strokeWidth={1.7} />
              {id === "sessions" && runningCount > 0 && (
                <span className="absolute -right-1 -top-1 grid h-4 min-w-4 place-items-center rounded-full px-1 text-[10px] font-bold tabular-nums motion-safe:animate-[terra-pop_0.3s_var(--ease)]"
                  style={{ background: "var(--c-agent)", color: "var(--on-accent)", boxShadow: "0 0 0 2px var(--panel)" }}>
                  {runningCount}
                </span>
              )}
            </button>
          );
        })}
      </div>

      <div className="min-w-1 flex-1" />
      {/* Settings sits with the account controls, not in primary nav: it is pure config and
          shouldn't compete for a rail slot with the operational views. */}
      <button
        data-dock
        title="Settings"
        aria-label="Settings"
        aria-current={view === "settings" ? "page" : undefined}
        onClick={() => onNavigate("settings")}
        className="grid h-10 w-10 place-items-center rounded-xl border transition-colors"
        style={{
          borderColor: view === "settings" ? "color-mix(in oklch, var(--accent) 40%, var(--border))" : "var(--border)",
          background: view === "settings" ? "color-mix(in oklch, var(--accent) 18%, transparent)" : "var(--surface-2)",
          color: view === "settings" ? "var(--accent)" : "var(--muted)",
        }}
      >
        <SlidersHorizontal size={16} />
      </button>
      {onLogout && (
        <button title="Sign out" aria-label="Sign out" onClick={onLogout}
          className="grid h-10 w-10 place-items-center rounded-xl border border-border bg-surface-2 text-muted transition-colors hover:border-[color-mix(in_oklch,var(--c-error)_35%,var(--border))] hover:text-error">
          <LogOut size={16} />
        </button>
      )}
      <button
        title={`Switch to ${mounted && resolvedTheme === "dark" ? "light" : "dark"} theme`}
        // aria-label beats title, so it must mirror the DYNAMIC title — a static "Toggle theme"
        // would leave screen-reader users the only ones who can't tell which theme is current.
        aria-label={`Switch to ${mounted && resolvedTheme === "dark" ? "light" : "dark"} theme`}
        onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
        className="grid h-10 w-10 place-items-center rounded-xl border border-border bg-surface-2 text-muted transition-colors hover:border-border-2 hover:text-text"
      >
        {mounted && resolvedTheme === "dark" ? <Moon size={16} /> : <Sun size={16} />}
      </button>
    </nav>
  );
}
