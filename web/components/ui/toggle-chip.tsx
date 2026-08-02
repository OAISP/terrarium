"use client";

import * as React from "react";

/**
 * Selectable pill — one recipe for the token-scope picker, the agent tool toggles and
 * the egress mode/host chips. Pass `tone` to theme it (default accent).
 */
export function ToggleChip({
  selected,
  tone = "var(--accent)",
  onClick,
  disabled,
  className,
  children,
}: {
  selected: boolean;
  tone?: string;
  onClick?: () => void;
  disabled?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={selected}
      className={
        "rounded-md border px-2.5 py-1 text-xs font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 " +
        (className ?? "")
      }
      style={
        selected
          ? { borderColor: `color-mix(in oklch, ${tone} 40%, var(--border))`, background: `color-mix(in oklch, ${tone} 14%, transparent)`, color: tone }
          : { borderColor: "var(--border)", background: "var(--surface-2)", color: "var(--muted)" }
      }
    >
      {children}
    </button>
  );
}
