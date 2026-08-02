"use client";

// Client cache/query layer (TanStack Query). One hook per REST resource, so views
// subscribe to exactly the data they render — automatic dedup + caching +
// stale-while-revalidate + focus refetch, and polling that PAUSES on a hidden tab.
// The live event stream stays on SSE (useEventStream), not here.

import {
  MutationCache,
  QueryCache,
  QueryClient,
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  UnauthorizedError,
  getCredStatus,
  getEgressAudit,
  getEgressPolicy,
  getHealth,
  getLogs,
  getSession,
  getUsage,
  listAgents,
  listEgressPresets,
  listEgressProfiles,
  listEnvironments,
  listModels,
  listTools,
  listSchedules,
  listSecrets,
  listSessions,
  listTemplates,
  listTokens,
} from "@/lib/api";
import type { LogFilters } from "@/lib/types";

// ── global 401 bus ───────────────────────────────────────────────────────────
// Any query/mutation that 401s fires this once, so Console can flip to the login
// screen from ONE place (replaces onUnauthorized threaded through every fetch).
type Listener = () => void;
const authListeners = new Set<Listener>();
export function onUnauthorized(fn: Listener): () => void {
  authListeners.add(fn);
  return () => authListeners.delete(fn);
}
function handleErr(e: unknown) {
  if (e instanceof UnauthorizedError) authListeners.forEach((fn) => fn());
}

// ── query keys (tuples → targeted invalidation) ──────────────────────────────
export const qk = {
  health: ["health"] as const,
  agents: ["agents"] as const,
  sessions: ["sessions"] as const,
  session: (id: string) => ["session", id] as const,
  schedules: ["schedules"] as const,
  tokens: ["tokens"] as const,
  secrets: ["secrets"] as const,
  egressPolicy: ["egress", "policy"] as const,
  egressProfiles: ["egress", "profiles"] as const,
  egressPresets: ["egress", "presets"] as const,
  egressAudit: ["egress", "audit"] as const,
  environments: ["environments"] as const,
  credStatus: ["credentials", "status"] as const,
  templates: ["templates"] as const,
  models: ["models"] as const,
  tools: ["tools"] as const,
  usage: (days: number) => ["usage", days] as const,
  logs: (f: LogFilters) => ["logs", f] as const,
};

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: true,
        refetchIntervalInBackground: false, // never poll a backgrounded tab
        retry: (n, e) => !(e instanceof UnauthorizedError) && n < 2,
      },
    },
    queryCache: new QueryCache({ onError: handleErr }),
    mutationCache: new MutationCache({ onError: handleErr }),
  });
}

// ── invalidation helper (used after a mutation; pair with AsyncButton.onSuccess) ─
export function useInvalidate(): (...keys: readonly (readonly unknown[])[]) => void {
  const qc = useQueryClient();
  return (...keys) => keys.forEach((queryKey) => qc.invalidateQueries({ queryKey }));
}

// ── resource hooks ───────────────────────────────────────────────────────────
// Only sessions/health/credStatus poll (the live-ish data); everything else just
// caches + refetches on window focus and after a mutation.
export const useHealth = () =>
  useQuery({ queryKey: qk.health, queryFn: getHealth, staleTime: 10_000, refetchInterval: 15_000 });
// Sessions are durable and accumulate forever, so the list is paged. useInfiniteQuery
// rather than a hand-rolled append: it keeps the loaded pages in ONE cache entry, so the
// 5s poll refreshes everything you have open (a running session on page 2 keeps ticking)
// and an invalidate after a mutation doesn't silently collapse you back to page one.
export const useSessions = () =>
  useInfiniteQuery({
    queryKey: qk.sessions,
    queryFn: ({ pageParam }) => listSessions(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    staleTime: 4_000,
    refetchInterval: 5_000,
  });
// A single session's summary, for one reached by URL that isn't in the loaded pages.
// Polled like the list so a live session opened this way still ticks.
export const useSession = (id: string | null) =>
  useQuery({ queryKey: qk.session(id ?? ""), queryFn: () => getSession(id as string),
             enabled: !!id, staleTime: 4_000, refetchInterval: 5_000 });
export const useAgents = () =>
  useQuery({ queryKey: qk.agents, queryFn: listAgents, staleTime: 60_000 });
export const useSchedules = () =>
  useQuery({ queryKey: qk.schedules, queryFn: listSchedules, staleTime: 60_000 });
export const useTokens = () =>
  useQuery({ queryKey: qk.tokens, queryFn: listTokens, staleTime: 60_000 });
export const useSecrets = () =>
  useQuery({ queryKey: qk.secrets, queryFn: listSecrets, staleTime: 60_000 });
export const useEgressPolicy = () =>
  useQuery({ queryKey: qk.egressPolicy, queryFn: getEgressPolicy, staleTime: 30_000 });
export const useEgressProfiles = () =>
  useQuery({ queryKey: qk.egressProfiles, queryFn: listEgressProfiles, staleTime: 30_000 });
// Built-in presets are static for the life of the orchestrator → cache hard.
export const useEgressPresets = () =>
  useQuery({ queryKey: qk.egressPresets, queryFn: listEgressPresets, staleTime: Infinity, gcTime: Infinity });
// Recent Warden decisions — live-ish, so poll (paused on a hidden tab by default).
export const useEgressAudit = (limit = 100) =>
  useQuery({ queryKey: qk.egressAudit, queryFn: () => getEgressAudit(limit), staleTime: 4_000, refetchInterval: 6_000 });
export const useEnvironments = () =>
  useQuery({ queryKey: qk.environments, queryFn: listEnvironments, staleTime: 30_000 });
// Built-in agent presets — static for the orchestrator's life; only fetched when creating.
export const useTemplates = (enabled = true) =>
  useQuery({ queryKey: qk.templates, queryFn: listTemplates, staleTime: Infinity, gcTime: Infinity, enabled });
// Fleet spend for a window, from the durable ledger. Keyed by the window so switching ranges
// is a cache miss rather than a refetch of the same key; polls because a running agent moves
// the number.
export const useUsage = (days: number) =>
  useQuery({ queryKey: qk.usage(days), queryFn: () => getUsage(days), staleTime: 15_000, refetchInterval: 30_000 });
// The model catalog — static for the orchestrator's life, so cache it hard. Every model picker
// in the console reads THIS (the agent form, the new-session dialog, the live switcher);
// keeping separate lists is what let them drift out of sync.
export const useModels = () =>
  useQuery({ queryKey: qk.models, queryFn: listModels, staleTime: Infinity, gcTime: Infinity });
// The tool + skill catalog — static for the orchestrator's life, so cache it hard.
export const useTools = () =>
  useQuery({ queryKey: qk.tools, queryFn: listTools, staleTime: Infinity, gcTime: Infinity });
export const useCredStatus = () =>
  useQuery({ queryKey: qk.credStatus, queryFn: getCredStatus, staleTime: 20_000, refetchInterval: 30_000 });
// The dedicated Logs view is live-ish, like the egress audit panel — poll it (paused on a
// hidden tab by the global default) so it isn't staler than the mini panel in EgressView.
export const useLogs = (f: LogFilters) =>
  useQuery({ queryKey: qk.logs(f), queryFn: () => getLogs(f), placeholderData: (prev) => prev, refetchInterval: 6_000 });
