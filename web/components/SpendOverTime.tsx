"use client";

import { useMemo, useState } from "react";
import { Table2, BarChart3 } from "lucide-react";
import type { SpendDay } from "@/lib/types";
import { fmtCost } from "@/lib/format";

// Daily fleet spend. Form choice: columns, not an area chart — days are discrete buckets and
// most of them are empty on a small fleet, so an area would draw a slope between two runs a
// week apart and imply spending that never happened. One measure, one hue: the same
// `--c-result` the rest of the console uses for cost, which is dark-mode-*selected* (its own
// step per theme, not an auto-flip) and clears 3:1 against both surfaces.
//
// No legend: a single series is named by the panel title, so a one-swatch box would just
// restate it. Identity never rests on color alone regardless — the axis, the labelled peak,
// the per-column tooltip and the table view all carry the values.

const BAR_MAX_PX = 24;   // cap the mark; the band's leftover is deliberate air
const GAP_PX = 2;        // surface gap — what separates touching columns (never a stroke)

/** Fill the gaps: the API returns only days that had activity, but a spend chart has to show
 *  the quiet days as zero — otherwise three runs in a month render as three adjacent columns
 *  and read as continuous daily activity. */
function densify(daily: SpendDay[], days: number): SpendDay[] {
  const bySpendDay = new Map(daily.map((d) => [d.day, d]));
  const out: SpendDay[] = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() - i));
    const key = d.toISOString().slice(0, 10);
    out.push(bySpendDay.get(key) ?? { day: key, sessions: 0, total_cost_usd: 0 });
  }
  return out;
}

/** Clean y-axis ceiling so ticks land on numbers a reader can hold — but a FINE ladder, not
 *  just 1/2/5. A coarse ladder rounds a $5.69 peak up to $10 and leaves the tallest column at
 *  57% of the plot, which reads as "nothing much happened" when the opposite is true. These
 *  steps are all still halvable, so the midpoint tick stays a clean number too. */
function niceMax(v: number): number {
  if (v <= 0) return 1;
  const mag = 10 ** Math.floor(Math.log10(v));
  for (const step of [1, 1.2, 1.6, 2, 2.4, 3, 4, 5, 6, 8, 10]) {
    if (v <= step * mag) return step * mag;
  }
  return 10 * mag;
}

export function SpendOverTime({ daily, days }: { daily: SpendDay[]; days: number }) {
  const [asTable, setAsTable] = useState(false);
  const series = useMemo(() => densify(daily, days), [daily, days]);
  const max = useMemo(() => niceMax(Math.max(...series.map((d) => d.total_cost_usd), 0)), [series]);
  // Label the peak only — a value on every column is unreadable and goes unread.
  const peak = useMemo(
    () => series.reduce((best, d) => (d.total_cost_usd > (best?.total_cost_usd ?? 0) ? d : best), series[0]),
    [series],
  );
  const hasSpend = series.some((d) => d.total_cost_usd > 0);

  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="text-2xs text-faint">
          {/* The axis carries the rest of the values; state the scale once. */}
          Daily spend · peak {fmtCost(peak?.total_cost_usd ?? 0)}
        </div>
        <button type="button" onClick={() => setAsTable((v) => !v)} aria-pressed={asTable}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-2xs font-medium text-muted outline-none transition-colors hover:border-accent hover:text-accent focus-visible:ring-2 focus-visible:ring-accent">
          {asTable ? <><BarChart3 className="size-3" /> Chart</> : <><Table2 className="size-3" /> Table</>}
        </button>
      </div>

      {asTable ? (
        // The table view is the accessibility floor: every value reachable without reading a
        // mark, and the only path to per-day numbers for a screen reader.
        <div className="max-h-64 overflow-auto rounded-lg border border-border">
          <table className="w-full text-left text-xs">
            <caption className="sr-only">Daily fleet spend for the last {days} days</caption>
            <thead className="sticky top-0 bg-surface-2 text-2xs uppercase tracking-wide text-faint">
              <tr><th scope="col" className="px-3 py-1.5 font-medium">Day</th>
                  <th scope="col" className="px-3 py-1.5 text-right font-medium">Sessions</th>
                  <th scope="col" className="px-3 py-1.5 text-right font-medium">Spend</th></tr>
            </thead>
            <tbody>
              {series.filter((d) => d.sessions > 0).map((d) => (
                <tr key={d.day} className="border-t border-border">
                  <td className="px-3 py-1 font-mono text-2xs text-muted">{d.day}</td>
                  <td className="px-3 py-1 text-right tabular-nums text-muted">{d.sessions}</td>
                  <td className="px-3 py-1 text-right font-mono tabular-nums">{fmtCost(d.total_cost_usd)}</td>
                </tr>
              ))}
              {!hasSpend && <tr><td colSpan={3} className="px-3 py-3 text-center text-faint">No spend in this window.</td></tr>}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="flex gap-2">
          {/* Y axis: three clean ticks (max / mid / 0). They carry every value the single
              direct label doesn't — which is why the ceiling ladder stays halvable. */}
          <div className="flex w-12 flex-none flex-col justify-between py-0.5 text-right text-2xs tabular-nums text-faint" aria-hidden>
            <span>{fmtCost(max)}</span><span>{fmtCost(max / 2)}</span><span>$0</span>
          </div>
          <div className="relative min-w-0 flex-1">
            {/* Hairline gridlines — solid (never dashed), one step off surface, recessive. */}
            <div className="absolute inset-x-0 bottom-0 h-px bg-border" aria-hidden />
            <div className="absolute inset-x-0 top-1/2 h-px bg-border opacity-50" aria-hidden />
            <ul className="flex h-32 items-end" style={{ gap: GAP_PX }}
              aria-label={`Daily spend for the last ${days} days. Peak ${fmtCost(peak?.total_cost_usd ?? 0)}.`}>
              {series.map((d) => {
                const pct = max > 0 ? (d.total_cost_usd / max) * 100 : 0;
                return (
                  <li key={d.day} className="group/col relative flex h-full flex-1 items-end justify-center"
                    style={{ maxWidth: BAR_MAX_PX }}>
                    {/* Hit target spans the full column height, not just the mark — a $0.01 day
                        is 1px tall and would otherwise be impossible to hover. */}
                    <span className="absolute inset-0 cursor-default" />
                    <span
                      className="w-full rounded-t-[4px] transition-[height]"
                      style={{
                        height: `${Math.max(pct, d.total_cost_usd > 0 ? 2 : 0)}%`,
                        background: "var(--c-result)",
                      }}
                    />
                    {/* Per-column tooltip — an HTML chart is interactive by default. */}
                    <span role="tooltip"
                      className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-1.5 hidden -translate-x-1/2 whitespace-nowrap rounded-md border border-border bg-panel px-2 py-1 text-2xs shadow-pop group-hover/col:block">
                      <span className="font-mono text-faint">{d.day}</span>
                      <span className="mx-1.5 font-semibold tabular-nums">{fmtCost(d.total_cost_usd)}</span>
                      <span className="text-muted">{d.sessions} run{d.sessions === 1 ? "" : "s"}</span>
                    </span>
                  </li>
                );
              })}
            </ul>
            {/* Date ends only — a tick per day is noise at 30+ columns. */}
            <div className="mt-1 flex justify-between font-mono text-2xs text-faint" aria-hidden>
              <span>{series[0]?.day.slice(5)}</span>
              <span>{series[series.length - 1]?.day.slice(5)}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
