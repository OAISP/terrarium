"use client";

import { cn } from "@/lib/utils";

const META: Record<string, { color: string; live?: boolean }> = {
  running:    { color: "var(--c-agent)", live: true },
  starting:   { color: "var(--c-tool)", live: true },
  ready:      { color: "var(--c-tool)", live: true },
  idle:       { color: "var(--c-result)" },
  ended:      { color: "var(--muted)" },
  terminated: { color: "var(--muted)" },
  error:      { color: "var(--c-error)" },
  failed:     { color: "var(--c-error)" },
};

export function StatusPill({ status, className }: { status: string | null | undefined; className?: string }) {
  const key = (status ?? "unknown").toLowerCase();
  const m = META[key] ?? { color: "var(--muted)" };
  return (
    <span
      className={cn("inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-2xs font-medium capitalize", className)}
      style={{
        color: m.color,
        background: `color-mix(in oklch, ${m.color} 13%, transparent)`,
        borderColor: `color-mix(in oklch, ${m.color} 28%, var(--border))`,
      }}
    >
      <span
        className={cn("h-1.5 w-1.5 rounded-full", m.live && "motion-safe:animate-[terra-breathe_1.8s_ease-in-out_infinite]")}
        style={{ background: m.color }}
      />
      {status ?? "unknown"}
    </span>
  );
}
