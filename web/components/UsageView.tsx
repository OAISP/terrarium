"use client";

import { useMemo, useState } from "react";
import { DollarSign, Hash, Activity, Wrench, BarChart3 } from "lucide-react";
import type { AgentSpec } from "@/lib/types";
import { fmtCost, fmtNum } from "@/lib/format";
import { useUsage } from "@/lib/queries";
import { CountUp } from "@/components/ui/countup";
import { EmptyState, ErrorBox } from "@/components/ui/feedback";
import { PageContainer } from "@/components/ui/page";
import { Panel } from "@/components/ui/section";
import { StatCard, TokenBar } from "@/components/ui/stat";
import { SpendOverTime } from "./SpendOverTime";

// Time ranges, in one row above the charts.
const RANGES = [
  { days: 7, label: "7d" },
  { days: 30, label: "30d" },
  { days: 90, label: "90d" },
] as const;

export function UsageView({ agents }: { agents: AgentSpec[] }) {
  const [days, setDays] = useState<number>(30);
  const usageQ = useUsage(days);
  const usage = usageQ.data ?? null;
  const agentName = useMemo(() => new Map(agents.map((a) => [a.id, a.name])), [agents]);

  // Everything on this page comes from the orchestrator, nothing from the session list.
  // Cost is read from the durable ledger (it outlives session deletion); tokens, tool calls
  // and cost-by-model are folded server-side from the logs. Folding the list here would now
  // report page one as if it were the fleet — and it already under-reported by whatever had
  // been deleted.
  const byModel = useMemo(
    () => (usage?.by_model ?? []).map((m) => ({ name: m.model || "unknown", cost: m.total_cost_usd })),
    [usage],
  );
  const tokens = usage?.tokens ?? { input: 0, output: 0, cacheRead: 0, cacheCreate: 0, subagent: 0, total: 0 };

  // Per-agent spend from the ledger, resolved to names. A deleted agent keeps its row (the
  // ledger has the id but no agent to look up), so fall back to the raw id rather than drop
  // spend that really happened.
  const byAgent = useMemo(() => (usage?.by_agent ?? []).slice(0, 8).map((a) => ({
    name: a.agent_id ? agentName.get(a.agent_id) ?? a.agent_id : "inline",
    cost: a.total_cost_usd,
  })), [usage, agentName]);

  const windowCost = usage?.totals.total_cost_usd ?? 0;
  const allTimeCost = usage?.all_time.total_cost_usd ?? 0;

  if ((usage?.all_time.sessions ?? 0) === 0 && !usageQ.isLoading) {
    return (
      <PageContainer>
        <EmptyState
          icon={BarChart3}
          title="No usage yet"
          description="Cost and token analytics appear here once you run a session."
        />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      {/* Filters in one row above the charts. */}
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Spend time range" className="flex items-center gap-1 rounded-lg border border-border bg-surface-2 p-1">
          {RANGES.map((r) => (
            <button key={r.days} type="button" aria-pressed={days === r.days} onClick={() => setDays(r.days)}
              className={`rounded-md px-2.5 py-1 text-xs font-medium outline-none transition-colors ${days === r.days ? "bg-surface text-text shadow-soft" : "text-muted hover:text-text"}`}>
              {r.label}
            </button>
          ))}
        </div>
        <span className="text-2xs text-faint">
          Spend comes from the durable ledger, so it survives session deletion. Token and
          tool counts are folded from the session logs, so those do not.
        </span>
      </div>

      {usageQ.error && <ErrorBox>{usageQ.error instanceof Error ? usageQ.error.message : String(usageQ.error)}</ErrorBox>}

      <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label={`Spend · last ${days}d`} icon={DollarSign} color="var(--c-result)"
          value={<span className="font-mono">$<CountUp to={windowCost} decimals={2} /></span>}
          hint={allTimeCost > windowCost ? `${fmtCost(allTimeCost)} all time` : undefined} />
        <StatCard label={`Tokens · last ${days}d`} icon={Hash} color="var(--accent)" value={<span className="font-mono"><CountUp to={tokens.total} /></span>} />
        <StatCard label={`Runs · last ${days}d`} icon={Activity} color="var(--c-agent)" value={<span className="font-mono"><CountUp to={usage?.totals.sessions ?? 0} /></span>} />
        <StatCard label={`Tool calls · last ${days}d`} icon={Wrench} color="var(--c-tool)" value={<span className="font-mono"><CountUp to={usage?.tool_calls ?? 0} /></span>} />
      </div>

      <Panel title={`Spend over time · last ${days} days`}>
        <SpendOverTime daily={usage?.daily ?? []} days={days} />
      </Panel>

      <div className="grid gap-3.5 lg:grid-cols-2">
        <Panel title="Token composition">
          <TokenBar tokens={tokens} format={fmtNum} showPercent />
        </Panel>
        <Panel title={`Cost by model · last ${days} days`}><Bars data={byModel} /></Panel>
      </div>

      <Panel title={`Spend by agent · last ${days} days`}><Bars data={byAgent} /></Panel>
    </PageContainer>
  );
}

function Bars({ data }: { data: { name: string; cost: number }[] }) {
  const max = Math.max(...data.map((d) => d.cost), 0.0001);
  if (data.length === 0) return <div className="py-8 text-center text-sm text-muted">No data yet</div>;
  return (
    <div className="space-y-2.5">
      {data.map((d, i) => (
        <div key={d.name} className="flex items-center gap-3">
          <span className="w-28 flex-none truncate text-xs text-muted">{d.name}</span>
          <div className="h-5 flex-1 overflow-hidden rounded-md bg-surface-2">
            <div className="h-full rounded-md transition-[width] duration-500" style={{ width: `${(d.cost / max) * 100}%`, background: i === 0 ? "var(--accent)" : "color-mix(in oklch, var(--accent) 55%, var(--surface-3))" }} />
          </div>
          <span className="w-16 flex-none text-right font-mono text-xs font-semibold">{fmtCost(d.cost)}</span>
        </div>
      ))}
    </div>
  );
}
