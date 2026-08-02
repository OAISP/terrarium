"use client";

import { useEffect, useState } from "react";
import { RefreshCw, Search, Loader2 } from "lucide-react";
import type { LogFilters } from "@/lib/types";
import { useLogs } from "@/lib/queries";
import { ErrorBox } from "@/components/ui/feedback";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Chip } from "@/components/ui/chip";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { fmtClock } from "@/lib/format";
import { decisionLabel, ANTHROPIC_HOSTS } from "@/lib/egress";

const SOURCES = [
  { v: "", label: "All sources" },
  { v: "event", label: "Session events" },
  { v: "egress", label: "Egress decisions" },
];

function isDeny(t: string | null): boolean {
  const s = String(t ?? "");
  return s.includes("deny") || s.includes("not-allow") || s.includes("error") || s.includes("kill");
}

export function LogsView() {
  const [f, setF] = useState<LogFilters>({ limit: 500 });
  const [search, setSearch] = useState("");

  // Cached + keyed by filters; placeholderData keeps the current rows visible while a
  // new filter loads (no skeleton flash), and refetch() backs the manual refresh.
  const logsQ = useLogs(f);
  const data = logsQ.data ?? null;
  const loading = logsQ.isFetching;
  const err = logsQ.error ? (logsQ.error instanceof Error ? logsQ.error.message : String(logsQ.error)) : null;

  useEffect(() => {
    const t = setTimeout(() => setF((p) => ({ ...p, q: search || undefined })), 350);  // debounce → q
    return () => clearTimeout(t);
  }, [search]);

  const setFilter = (k: keyof LogFilters, v: string | undefined) =>
    setF((p) => ({ ...p, [k]: v || undefined }));

  const facets = data?.facets;
  const all = data?.logs ?? [];
  const cut = data?.truncated;

  // isDeny() styled failures red but nothing let you SELECT them — the type facet is server-supplied
  // with no errors-only option, so finding the one failure meant reading every row.
  const [errorsOnly, setErrorsOnly] = useState(false);
  // ~350 of 367 rows were the agent's own inspected Anthropic calls: the harness burying its own
  // audit log. Hidden by default, never silently — the count is shown and it's one click back.
  const [showHarness, setShowHarness] = useState(false);
  const isHarness = (e: (typeof all)[number]) =>
    e.source === "egress" && e.type === "mitm" && ANTHROPIC_HOSTS.includes(e.host ?? "");
  const hiddenHarness = showHarness ? 0 : all.filter(isHarness).length;
  const logs = all.filter((e) => (!errorsOnly || isDeny(e.type)) && (showHarness || !isHarness(e)));

  // A column whose value is identical on every row is scope, not data — surface it once above the
  // table and drop the column. (Needs 2+ rows to mean anything.)
  const sameFor = <T,>(f: (e: (typeof logs)[number]) => T): T | null => {
    if (logs.length < 2) return null;
    const first = f(logs[0]);
    return logs.every((e) => f(e) === first) ? first : null;
  };
  const constant = {
    source: sameFor((e) => e.source),
    session: sameFor((e) => e.session_id),
    agent: sameFor((e) => e.agent_id),
  };
  const scope = [
    constant.source ? { k: "source", v: String(constant.source) } : null,
    constant.session ? { k: "session", v: String(constant.session).slice(-12) } : null,
    constant.agent ? { k: "agent", v: String(constant.agent) } : null,
  ].filter(Boolean) as { k: string; v: string }[];

  // ui/page.tsx exists to keep width/rhythm consistent; this view hardcoded a THIRD width (1100px)
  // and opted out of it, so the column jumped on every tab switch.
  return (
    <div className="mx-auto flex h-full max-w-6xl flex-col gap-3.5">
      {/* The Hud already renders <h1>Logs</h1> + this exact subtitle directly above. Rendering them
          again gave the page two <h1>s (WCAG 1.3.1) and said the same thing twice before any data. */}
      <div className="flex items-center justify-end gap-3">
        <Button variant="outline" size="sm" onClick={() => logsQ.refetch()} aria-label="Refresh">
          {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />} Refresh
        </Button>
      </div>

      {/* filter bar */}
      <div className="flex flex-wrap items-center gap-2">
        <FilterSelect value={f.source} onChange={(v) => setFilter("source", v)} allLabel="All sources"
          options={SOURCES.filter((s) => s.v).map((s) => ({ value: s.v, label: s.label }))} />
        <FilterSelect value={f.agent_id} onChange={(v) => setFilter("agent_id", v)} allLabel="All agents"
          options={(facets?.agents ?? []).map((a) => ({ value: a, label: a }))} />
        <FilterSelect value={f.session_id} onChange={(v) => setFilter("session_id", v)} allLabel="All sessions"
          options={(facets?.sessions ?? []).map((s) => ({ value: s.id, label: s.title || s.id }))} />
        <FilterSelect value={f.type} onChange={(v) => setFilter("type", v)} allLabel="All types"
          options={(facets?.types ?? []).map((t) => ({ value: t, label: t }))} />
        <div className="relative flex-1 min-w-[180px]">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 z-10 size-3.5 -translate-y-1/2 text-faint" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="search host or text…"
            className="w-full pl-8" />
        </div>
        <button type="button" aria-pressed={errorsOnly} onClick={() => setErrorsOnly((v) => !v)}
          className="rounded-md border px-2.5 py-1.5 text-2xs font-medium transition-colors"
          style={errorsOnly
            ? { borderColor: "color-mix(in oklch, var(--c-error) 40%, var(--border))", background: "color-mix(in oklch, var(--c-error) 12%, transparent)", color: "var(--c-error)" }
            : { borderColor: "var(--border)", color: "var(--muted)" }}>
          Errors only
        </button>
        {(hiddenHarness > 0 || showHarness) && (
          <button type="button" aria-pressed={showHarness} onClick={() => setShowHarness((v) => !v)}
            className="rounded-md border border-border px-2.5 py-1.5 text-2xs font-medium text-muted transition-colors hover:border-accent hover:text-accent">
            {showHarness ? "Hide harness traffic" : `Show harness traffic (${hiddenHarness})`}
          </button>
        )}
      </div>

      {err && <ErrorBox>{err}</ErrorBox>}

      {/* Columns that are constant across every row carry zero entropy — they're SCOPE, not data.
          Live, SESSION and AGENT were identical on all 19 rows and SOURCE on 18, burning ~250px
          while DETAIL (the only informative column) was truncated at 420px. Show the constants once,
          as chips, and give the space back to the column that says something. */}
      {scope.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-2xs text-faint">
          <span>every row:</span>
          {scope.map((s) => (
            <span key={s.k} className="inline-flex items-center gap-1 rounded-md border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-muted">
              {s.k} {s.v}
            </span>
          ))}
        </div>
      )}

      {/* results */}
      <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-surface">
        <table className="w-full border-collapse text-left">
          <thead className="sticky top-0 z-10 bg-surface-2 text-2xs uppercase tracking-wide text-faint">
            <tr>
              <th className="px-3 py-2 font-medium">Time</th>
              {!constant.source && <th className="px-3 py-2 font-medium">Source</th>}
              {!constant.session && <th className="px-3 py-2 font-medium">Session</th>}
              {!constant.agent && <th className="px-3 py-2 font-medium">Agent</th>}
              <th className="px-3 py-2 font-medium">Type</th>
              <th className="px-3 py-2 font-medium">Detail</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((e, i) => (
              <tr key={`${e.ts}-${i}`} className="border-t border-border align-top hover:bg-surface-2">
                <td className="whitespace-nowrap px-3 py-1.5 font-mono text-2xs text-faint">{fmtClock(e.ts)}</td>
                {!constant.source && (
                  <td className="px-3 py-1.5">
                    {e.source === "egress"
                      ? <Chip tone={isDeny(e.type) ? "var(--c-error)" : "var(--c-agent)"} className="flex-none font-medium">egress</Chip>
                      : <Chip tone="var(--c-think)" className="flex-none font-medium">event</Chip>}
                  </td>
                )}
                {!constant.session && <td className="whitespace-nowrap px-3 py-1.5 font-mono text-2xs text-muted">{e.session_id ? e.session_id.slice(-12) : "—"}</td>}
                {!constant.agent && <td className="whitespace-nowrap px-3 py-1.5 text-2xs text-muted">{e.agent_id ?? "—"}</td>}
                <td className="whitespace-nowrap px-3 py-1.5">
                  {/* Same raw-token bug as the Egress feed: this printed Warden's `mitm` verbatim. */}
                  <span className="font-mono text-2xs" style={{ color: e.source === "egress" && isDeny(e.type) ? "var(--c-error)" : undefined }}>
                    {e.source === "egress" && e.type ? decisionLabel(e.type) : e.type}
                  </span>
                </td>
                <td className="truncate px-3 py-1.5 text-sm" title={e.detail}>
                  {e.detail}{e.host ? <span className="text-muted"> · {e.host}{e.port ? `:${e.port}` : ""}</span> : ""}
                  {e.reason ? <span className="text-faint"> ({e.reason})</span> : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loading && logs.length === 0 && (
          <div className="p-8 text-center text-sm text-faint">
            No logs match these filters.
            {(cut?.sessions ?? 0) > 0 && <> Note that {cut?.sessions} older session{cut?.sessions === 1 ? " was" : "s were"} not scanned — narrow by agent or session to reach them.</>}
          </div>
        )}
        {loading && logs.length === 0 && (
          <div className="flex items-center justify-center gap-2 p-8 text-sm text-faint"><Loader2 className="size-4 animate-spin" /> loading…</div>
        )}
      </div>

      {/* This view stops looking in three places — the session fan-out, each session's
          history window, and the merged page. Say which one bit, and only when one did: an
          operator must never read an unscanned session as "that never happened". */}
      {(!!cut && (cut.sessions > 0 || cut.rows || cut.limit)) && (
        <div className="flex items-start gap-2 rounded-lg border px-2.5 py-2 text-2xs leading-relaxed"
          style={{ borderColor: "color-mix(in oklch, var(--c-result) 30%, var(--border))",
                   background: "color-mix(in oklch, var(--c-result) 8%, transparent)", color: "var(--muted)" }}>
          <span className="mt-1 size-1.5 flex-none rounded-full" style={{ background: "var(--c-result)" }} aria-hidden />
          <span>
            <span className="font-medium text-text">Partial view.</span>{" "}
            {cut.sessions > 0 && <>Only the {cut.scan_limit} most recent matching sessions were scanned ({cut.sessions} older skipped). </>}
            {cut.rows && <>At least one session had more history than the per-session window. </>}
            {cut.limit && <>More entries matched than the page holds. </>}
            Filter by agent or session, or narrow the time range, to see the rest.
          </span>
        </div>
      )}

      <p className="text-2xs text-faint">
        {/* Never let a filtered count read as "that's everything" — say what's hidden. */}
        Showing {logs.length} of {all.length} entries
        {hiddenHarness > 0 && <> · {hiddenHarness} harness call{hiddenHarness === 1 ? "" : "s"} hidden</>}
        {errorsOnly && <> · errors only</>}. Egress decisions come from each session&apos;s Warden, which filters by host and is invisible to the agent.
        Kernel‑level IP drops aren&apos;t audited. Audits are retained after a session ends, so its chain stays verifiable.
      </p>
    </div>
  );
}

// Radix Select for the filter bar (replaces native <select> — consistent styling +
// the accent focus ring). "" isn't a valid Radix item value, so map it to "__all".
function FilterSelect({ value, onChange, allLabel, options }: {
  value: string | undefined;
  onChange: (v: string | undefined) => void;
  allLabel: string;
  options: { value: string; label: string }[];
}) {
  return (
    <Select value={value ?? "__all"} onValueChange={(v) => onChange(v === "__all" ? undefined : v)}>
      <SelectTrigger className="h-8 w-auto gap-1.5 text-sm"><SelectValue /></SelectTrigger>
      <SelectContent>
        <SelectItem value="__all">{allLabel}</SelectItem>
        {options.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
      </SelectContent>
    </Select>
  );
}
