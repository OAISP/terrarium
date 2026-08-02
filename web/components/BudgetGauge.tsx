"use client";

import { motion, useReducedMotion } from "framer-motion";

/** SVG ring: cost vs max_budget_usd. accent → tool (>60%) → error (>85%). */
export function BudgetGauge({ cost, budget, size = 40, stroke = 4 }: {
  cost: number; budget: number | null; size?: number; stroke?: number;
}) {
  const reduce = useReducedMotion();
  const has = budget != null && budget > 0;
  const pct = has ? Math.max(0, Math.min(1, cost / (budget as number))) : 0;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const color = pct > 0.85 ? "var(--c-error)" : pct > 0.6 ? "var(--c-tool)" : "var(--accent)";

  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size} style={{ transform: "rotate(-90deg)" }}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-3)" strokeWidth={stroke} />
        {has && (
          <motion.circle
            cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={stroke} strokeLinecap="round"
            strokeDasharray={c} initial={false} animate={{ strokeDashoffset: c * (1 - pct) }}
            transition={reduce ? { duration: 0 } : { duration: 0.5, ease: [0.2, 0.7, 0.3, 1] }}
          />
        )}
      </svg>
      <span className="absolute inset-0 grid place-items-center font-mono text-[10px] font-bold" style={{ color: has ? color : "var(--faint)" }}>
        {has ? `${Math.round(pct * 100)}%` : "∞"}
      </span>
    </div>
  );
}
