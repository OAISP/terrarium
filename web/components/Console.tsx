"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { LogoBadge } from "./Logo";
import { deleteSession, getAuthStatus, logout, type AuthStatus } from "@/lib/api";

import { onUnauthorized, qk, useAgents, useEnvironments, useHealth, useInvalidate, useSchedules, useSecrets, useSession, useSessions, useTokens } from "@/lib/queries";
import { toast } from "@/components/ui/toast";
import { LoginForm } from "@/components/LoginForm";
import { Dock, DEFAULT_TAB, type View } from "@/components/Dock";
import { Hud } from "@/components/Hud";
import { CommandPalette } from "@/components/CommandPalette";
import { AgentsView } from "@/components/AgentsView";
import { SessionsView } from "@/components/SessionsView";
import { SessionView } from "@/components/SessionView";
import { UsageView } from "@/components/UsageView";
import { SchedulesView } from "@/components/SchedulesView";
import { TokensView } from "@/components/TokensView";
import { EnvironmentsView } from "@/components/EnvironmentsView";
import { EgressView } from "@/components/EgressView";
import { SecretsView } from "@/components/SecretsView";
import { LogsView } from "@/components/LogsView";

// Most titles derive from the view id; these override where the label differs.
const TITLE: Partial<Record<View, string>> = {
  usage: "Usage",
  schedules: "Schedules",
  settings: "Settings",
};

const SUBTITLE: Record<View, string> = {
  sessions: "Running and historical agent runs.",
  agents: "Reusable agent configs · model, tools, limits. Launch sessions against them.",
  boundary: "What agents may reach and what they carry.",
  logs: "Session activity and egress decisions across all sessions, with filters.",
  settings: "Scoped API access tokens for CI/cron callers.",
  usage: "Cost and token spend across all sessions.",
  schedules: "Recurring agents, launched on a cron schedule.",
  environments: "Named bundles an agent attaches to · the secrets it carries and where it may reach.",
  egress: "Where agents may reach out. Firewall rules (domains, IPs, CIDRs) plus a live audit.",
  secrets: "Host-scoped header credentials, injected at the egress boundary.",
};

const VIEWS: View[] = ["sessions", "agents", "boundary", "logs", "settings", "usage", "schedules",
  "environments", "egress", "secrets"];

// /boundary is a tab group, not a page — land on its first tab. /tokens kept working when it
// became Settings; a bookmark or a link in an old ticket must not 404 because the IA improved.
const ALIASES: Record<string, View> = { boundary: "environments", tokens: "settings" };

// Sub-views that live under a primary destination, surfaced as tabs rather than rail slots. Each
// keeps its own URL, so every one of these is still linkable and refresh-safe.
const BOUNDARY_TABS = [
  { id: "environments" as View, label: "Environments" },
  { id: "egress" as View, label: "Egress" },
  { id: "secrets" as View, label: "Secrets" },
];
const SESSION_TABS = [{ id: "sessions" as View, label: "Sessions" }, { id: "usage" as View, label: "Usage" }];
const AGENT_TABS = [{ id: "agents" as View, label: "Agents" }, { id: "schedules" as View, label: "Schedules" }];
const TABS: Partial<Record<View, { id: View; label: string }[]>> = {
  sessions: SESSION_TABS, usage: SESSION_TABS,
  agents: AGENT_TABS, schedules: AGENT_TABS,
  environments: BOUNDARY_TABS, egress: BOUNDARY_TABS, secrets: BOUNDARY_TABS,
};

export function Console() {
  // The URL is the source of truth for "where am I", so every view is linkable, Back works,
  // and a refresh keeps the run you were watching. Path: /<view>[/<sessionId>], plus
  // ?new=<agentId> to pre-arm the launch dialog. Derived, not mirrored into state.
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const seg = useMemo(() => pathname.split("/").filter(Boolean).map(decodeURIComponent), [pathname]);
  const alias = ALIASES[seg[0]];
  const view: View = alias ?? (VIEWS.includes(seg[0] as View) ? (seg[0] as View) : "sessions");
  const openSessionId = view === "sessions" && seg[1] ? seg[1] : null;
  const newSessionAgentId = searchParams.get("new");
  const [palette, setPalette] = useState(false);

  // Old address → new one, in the URL bar (not just on screen), so the bookmark heals itself.
  useEffect(() => {
    if (alias) router.replace(`/${alias}#${seg[0]}`);
  }, [alias, router, seg]);

  // Data comes from the query cache (dedup + cache + focus-refetch + polling that
  // pauses on a hidden tab) — no more god-state + blind 5s setInterval re-rendering
  // the whole tree. Only sessions/health poll (see lib/queries); the rest cache.
  const queryClient = useQueryClient();
  const invalidate = useInvalidate();
  const errOf = (e: unknown) => (e instanceof Error ? e.message : e ? String(e) : null);

  const agentsQ = useAgents();
  const sessionsQ = useSessions();
  const healthQ = useHealth();
  const schedulesQ = useSchedules();
  const tokensQ = useTokens();
  const secretsQ = useSecrets();
  const environmentsQ = useEnvironments();

  const agents = useMemo(() => agentsQ.data ?? [], [agentsQ.data]);
  // Flatten the loaded pages. `running`/`total` come from the newest page's envelope and
  // count the WHOLE fleet — a live badge computed from the loaded rows would under-report
  // the moment a running session fell past page one.
  const sessions = useMemo(() => (sessionsQ.data?.pages ?? []).flatMap((p) => p.sessions), [sessionsQ.data]);
  const fleet = sessionsQ.data?.pages?.[0] ?? null;
  const health = healthQ.data ?? null;
  const schedules = useMemo(() => schedulesQ.data ?? [], [schedulesQ.data]);
  const tokens = useMemo(() => tokensQ.data ?? [], [tokensQ.data]);
  const secrets = useMemo(() => secretsQ.data ?? [], [secretsQ.data]);
  const environments = useMemo(() => environmentsQ.data ?? [], [environmentsQ.data]);

  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const recheckAuth = useCallback(async () => { try { setAuth(await getAuthStatus()); } catch { setAuth({ required: false, authed: true, reachable: false }); } }, []);
  async function doLogout() { await logout(); setAuth({ required: true, authed: false, reachable: true }); }

  // Any query/mutation that 401s flips the whole console to the login screen, once.
  useEffect(() => onUnauthorized(() => setAuth({ required: true, authed: false, reachable: true })), []);
  // An async probe of the orchestrator on mount: the setState runs in the promise callback,
  // not synchronously in the effect body.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { recheckAuth(); }, [recheckAuth]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette((v) => !v); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const runningCount = fleet?.running ?? 0;
  // An older session reached by URL may sit past the pages we've loaded, so fall back to
  // fetching just that one. (SessionView degrades without it — the transcript carries most
  // of what it renders — but the age and isolation badges come only from the summary.)
  const listed = useMemo(() => sessions.find((s) => s.id === openSessionId) ?? null, [sessions, openSessionId]);
  const fetched = useSession(openSessionId && !listed ? openSessionId : null);
  const openSummary = listed ?? fetched.data ?? null;
  const openAgent = useMemo(() => agents.find((a) => a.id === openSummary?.agent_id) ?? null, [agents, openSummary]);

  async function handleSessionDelete(id: string) {
    try { await deleteSession(id); }
    catch (e) { toast.error(`Delete failed: ${e instanceof Error ? e.message : String(e)}`); return; }
    router.push("/sessions"); invalidate(qk.sessions);
  }
  const navigate = useCallback((v: View) => router.push(`/${DEFAULT_TAB[v] ?? v}`), [router]);
  const openSession = useCallback((id: string) => router.push(`/sessions/${encodeURIComponent(id)}`), [router]);

  if (auth === null) return (
    <div className="grid min-h-screen place-items-center bg-bg">
      <div className="flex flex-col items-center gap-3 text-sm text-muted">
        <LogoBadge size={44} className="motion-safe:animate-[terra-breathe_2s_ease-in-out_infinite]" />
        Connecting to Terrarium…
      </div>
    </div>
  );
  // The orchestrator didn't answer the auth probe. Say so, rather than dropping into a
  // console whose every query then fails for no visible reason.
  if (!auth.reachable && !(auth.required && !auth.authed)) return (
    <div className="grid min-h-screen place-items-center bg-bg">
      <div className="flex max-w-sm flex-col items-center gap-3 text-center text-sm text-muted">
        <LogoBadge size={44} />
        <div className="font-medium text-text">Can&apos;t reach the orchestrator</div>
        <p className="text-muted">The console loaded but the Terrarium API didn&apos;t respond. Check that it&apos;s running and reachable.</p>
        <button onClick={() => { setAuth(null); recheckAuth(); }}
          className="mt-1 rounded-lg border border-border bg-surface-2 px-4 py-2 text-sm font-medium text-text outline-none transition-colors hover:bg-surface-3 focus-visible:ring-2 focus-visible:ring-accent">
          Retry
        </button>
      </div>
    </div>
  );
  if (auth.required && !auth.authed) return <LoginForm reachable={auth.reachable} onSuccess={() => { recheckAuth(); queryClient.invalidateQueries(); }} />;

  return (
    <div className="flex min-h-dvh flex-col gap-3.5 bg-bg p-2.5 md:h-screen md:flex-row md:p-3.5">
      {/* Nine dock buttons + logout + theme precede the content on every view, and with no browser
          Back there was no way past them — a keyboard user paid ~11 stops to reach the page. */}
      <a href="#main" className="sr-only rounded-lg bg-surface-2 px-4 py-2 text-sm font-medium text-text focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50">
        Skip to content
      </a>
      <Dock view={view} onNavigate={navigate} runningCount={runningCount} onLogout={auth.required ? doLogout : undefined} />

      <main id="main" className="flex min-h-0 min-w-0 flex-1 flex-col gap-3.5">
        {openSessionId ? (
          // key = the session id, so navigating from one session to another REMOUNTS this
          // subtree. Without it React reused the instance and its state leaked across: the
          // composer draft, the attached-upload chips, the inline error and the optimistic
          // echo all carried over, so you could open session B and see A's unsent message
          // (and, worse, send it there).
          <SessionView key={openSessionId} sessionId={openSessionId} summary={openSummary} agent={openAgent} health={health}
            onBack={() => router.push("/sessions")} onDeleted={() => handleSessionDelete(openSessionId)} />
        ) : (
          <>
            <Hud title={TITLE[view] ?? view[0].toUpperCase() + view.slice(1)} subtitle={SUBTITLE[view]} health={health} onPalette={() => setPalette(true)}
              tabs={TABS[view]} activeTab={view} onTab={navigate} />
            <div className="min-h-0 flex-1">
              {view === "agents" ? (
                <AgentsView agents={agents} loading={agentsQ.isLoading} error={errOf(agentsQ.error)} onChanged={() => invalidate(qk.agents, qk.sessions)} onNewSession={(id) => router.push(`/sessions?new=${encodeURIComponent(id)}`)} />
              ) : view === "usage" ? (
                <UsageView agents={agents} />
              ) : view === "schedules" ? (
                <SchedulesView schedules={schedules} agents={agents} loading={schedulesQ.isLoading} error={errOf(schedulesQ.error)} onChanged={() => invalidate(qk.schedules, qk.sessions)} />
              ) : view === "settings" ? (
                <TokensView tokens={tokens} loading={tokensQ.isLoading} error={errOf(tokensQ.error)} onChanged={() => invalidate(qk.tokens)} />
              ) : view === "environments" ? (
                <EnvironmentsView agents={agents} secrets={secrets} />
              ) : view === "egress" ? (
                <EgressView />
              ) : view === "secrets" ? (
                <SecretsView secrets={secrets} environments={environments}
                  loading={secretsQ.isLoading} error={errOf(secretsQ.error)} onChanged={() => invalidate(qk.secrets)} />
              ) : view === "logs" ? (
                <LogsView />
              ) : (
                <SessionsView sessions={sessions} agents={agents} loading={sessionsQ.isLoading} error={errOf(sessionsQ.error)}
                  total={fleet?.total ?? sessions.length}
                  hasMore={!!sessionsQ.hasNextPage} loadingMore={sessionsQ.isFetchingNextPage}
                  onLoadMore={() => sessionsQ.fetchNextPage()}
                  newSessionAgentId={newSessionAgentId} clearNewSessionAgentId={() => router.replace("/sessions")} onChanged={() => invalidate(qk.sessions)} onOpen={openSession} />
              )}
            </div>
          </>
        )}
      </main>

      <CommandPalette open={palette} onOpenChange={setPalette} agents={agents} sessions={sessions}
        onNavigate={navigate} onOpenSession={openSession}
        onNewAgent={() => router.push("/agents")}
        onNewSession={() => router.push("/sessions?new=")}
        onLaunchAgent={(id) => router.push(`/sessions?new=${encodeURIComponent(id)}`)}
        // Deliberately navigates to the control rather than firing the kill switch: freezing ALL
        // egress (even Anthropic) from a fuzzy-matched palette row, one stray ↵ away, is exactly
        // the accident a panic button must not enable. It lands you on the confirm, armed.
        onFreezeEgress={() => router.push("/boundary#egress")} />
    </div>
  );
}
