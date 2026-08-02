// Browser-side helpers that call our Next route handlers.

import type {
  AgentPayload,
  AgentSpec,
  EgressDecision,
  EgressPolicy,
  EgressProfile,
  EgressRule,
  EgressHostOverride,
  Environment,
  Health,
  LogFilters,
  LogsResponse,
  ModelInfo,
  UsageWindow,
  Schedule,
  Secret,
  SessionPage,
  SessionSummary,
  Template,
  Token,
  ToolCatalog,
} from "./types";

export class UnauthorizedError extends Error {
  constructor() {
    super("Unauthorized");
    this.name = "UnauthorizedError";
  }
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    // non-JSON response
  }
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    // Don't surface a raw HTML body (e.g. a Next.js 404 page during a deploy) as the
    // error message — only use plain, short text; otherwise fall back to the status.
    const plain = text && !/^\s*</.test(text) && text.length <= 200 ? text : "";
    const msg =
      (data && typeof data === "object" && "error" in data
        ? String((data as Record<string, unknown>).error) +
          ("detail" in (data as object)
            ? `: ${(data as Record<string, unknown>).detail}`
            : "")
        : "") ||
      plain ||
      `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data as T;
}

export async function getHealth(): Promise<Health> {
  const res = await fetch("/api/health", { cache: "no-store" });
  return jsonOrThrow<Health>(res);
}

export async function listAgents(): Promise<AgentSpec[]> {
  const res = await fetch("/api/agents", { cache: "no-store" });
  const data = await jsonOrThrow<{ agents: AgentSpec[] }>(res);
  return data.agents ?? [];
}

export async function createAgent(payload: AgentPayload): Promise<AgentSpec> {
  const res = await fetch("/api/agents", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return jsonOrThrow<AgentSpec>(res);
}

export async function updateAgent(
  id: string,
  payload: AgentPayload,
): Promise<AgentSpec> {
  const res = await fetch(`/api/agents/${id}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return jsonOrThrow<AgentSpec>(res);
}

export async function deleteAgent(
  id: string,
  purgeMemory: boolean,
): Promise<void> {
  const res = await fetch(`/api/agents/${id}?purge_memory=${purgeMemory}`, {
    method: "DELETE",
  });
  await jsonOrThrow(res);
}

/** One page of sessions, newest first. `total`/`running` count the whole fleet, not the
 *  page, so a live badge doesn't shrink as you page. Pass a page's `next_cursor` back as
 *  `before` to continue. */
export async function listSessions(before?: string | null): Promise<SessionPage> {
  const qs = new URLSearchParams({ limit: "100" });
  if (before) qs.set("before", before);
  const res = await fetch(`/api/sessions?${qs}`, { cache: "no-store" });
  const data = await jsonOrThrow<SessionPage>(res);
  return { sessions: data.sessions ?? [], next_cursor: data.next_cursor ?? null,
           total: data.total ?? 0, running: data.running ?? 0 };
}

export async function getSession(id: string): Promise<SessionSummary> {
  const res = await fetch(`/api/sessions/${id}`, { cache: "no-store" });
  return jsonOrThrow<SessionSummary>(res);
}

export async function createSession(body: {
  agent_id?: string;
  title?: string;
  model?: string;
  system_mode?: string;
}): Promise<{ id: string; status: string; agent_id: string | null }> {
  const res = await fetch("/api/sessions", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return jsonOrThrow(res);
}

export async function deleteSession(id: string): Promise<void> {
  const res = await fetch(`/api/sessions/${id}`, { method: "DELETE" });
  await jsonOrThrow(res);
}

export async function sendMessage(id: string, text: string): Promise<void> {
  const res = await fetch(`/api/sessions/${id}/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ text }),
  });
  await jsonOrThrow(res);
}

export async function interruptSession(id: string): Promise<void> {
  const res = await fetch(`/api/sessions/${id}/interrupt`, { method: "POST" });
  await jsonOrThrow(res);
}

// Answer a pending AskUserQuestion. `answers` maps each question's text to the chosen
// option label (string), a list of labels (multi-select), or free text ("Other").
export async function answerSession(
  id: string,
  questionId: string,
  answers: Record<string, string | string[]>,
): Promise<void> {
  const res = await fetch(`/api/sessions/${id}/answer`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ question_id: questionId, answers }),
  });
  await jsonOrThrow(res);
}

// Switch a running session's model live (set_model). The next turn re-reads context
// uncached (input-cache penalty), but no restart — the conversation continues.
export async function reconfigureSession(id: string, model: string): Promise<void> {
  const res = await fetch(`/api/sessions/${id}/config`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model }),
  });
  await jsonOrThrow(res);
}

export type Decision = "allow" | "always" | "deny";

// Approve/deny a pending tool-permission request (an EV_PERMISSION event).
export async function decideSession(id: string, requestId: string, decision: Decision): Promise<void> {
  const res = await fetch(`/api/sessions/${id}/permission`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ request_id: requestId, decision }),
  });
  await jsonOrThrow(res);
}

export type RewindMode = "files" | "conversation" | "both";

export async function rewindSession(id: string, messageId: string, mode: RewindMode = "files"): Promise<void> {
  const res = await fetch(`/api/sessions/${id}/rewind`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message_id: messageId, mode }),
  });
  await jsonOrThrow(res);
}

export async function uploadFile(id: string, file: File, name?: string): Promise<{ name: string; size: number }> {
  const form = new FormData();
  form.append("file", file);
  if (name) form.append("name", name);
  const res = await fetch(`/api/sessions/${id}/files/upload`, { method: "POST", body: form });
  return jsonOrThrow<{ name: string; size: number }>(res);
}

/** Download one workspace file and hand it to the browser as a save.
 *
 *  Fetched (not a bare <a href>) so a failure surfaces as an error instead of navigating the
 *  console to an error page, and so the request carries the session cookie the proxy expects.
 *  The blob URL is revoked immediately after the click — a same-origin URL pointing at
 *  agent-authored bytes should not outlive the save it was created for. */
export async function downloadFile(id: string, name: string): Promise<void> {
  const res = await fetch(`/api/sessions/${id}/files/${encodeURIComponent(name)}`);
  if (!res.ok) {
    let detail = `${res.status}`;
    try { detail = ((await res.json()) as { detail?: string }).detail ?? detail; } catch { /* not JSON */ }
    if (res.status === 401) throw new UnauthorizedError();
    throw new Error(detail);
  }
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

// ── auth ─────────────────────────────────────────────────────────────────────
export type AuthStatus = { required: boolean; authed: boolean; reachable: boolean };

export async function getAuthStatus(): Promise<AuthStatus> {
  const res = await fetch("/api/auth/status", { cache: "no-store" });
  return res.json();
}

export async function login(token: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ token }),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok && data.ok, error: data.error };
}

export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", { method: "POST" });
}

// ── subscription credential (never returns the token itself) ───────────────────
export type CredStatus = {
  managed: boolean;
  present?: boolean;
  valid?: boolean;
  expires_in_s?: number | null;
  subscription_type?: string | null;
  last_refresh?: string | null;
  last_error?: string | null;
};

export async function getCredStatus(): Promise<CredStatus> {
  const res = await fetch("/api/credentials/status", { cache: "no-store" });
  return jsonOrThrow<CredStatus>(res);
}

export async function setCredentials(credentials: string): Promise<CredStatus> {
  const res = await fetch("/api/credentials", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ credentials }),
  });
  return jsonOrThrow<CredStatus>(res);
}

export async function clearCredentials(): Promise<void> {
  await jsonOrThrow(await fetch("/api/credentials", { method: "DELETE" }));
}

// ── templates ──────────────────────────────────────────────────────────────
export async function listTemplates(): Promise<Template[]> {
  const res = await fetch("/api/templates", { cache: "no-store" });
  return (await jsonOrThrow<{ templates: Template[] }>(res)).templates ?? [];
}

// ── model catalog ──────────────────────────────────────────────────────────
// The orchestrator owns the list (terracore/models.py); the console renders it.
export type ModelCatalog = { models: ModelInfo[]; default: string };

export async function listModels(): Promise<ModelCatalog> {
  const res = await fetch("/api/models", { cache: "no-store" });
  return jsonOrThrow<ModelCatalog>(res);
}

export async function listTools(): Promise<ToolCatalog> {
  const res = await fetch("/api/tools", { cache: "no-store" });
  return jsonOrThrow<ToolCatalog>(res);
}

// ── usage (fleet spend over a window, from the durable ledger) ───────────────
export async function getUsage(days: number): Promise<UsageWindow> {
  const res = await fetch(`/api/usage?days=${days}`, { cache: "no-store" });
  return jsonOrThrow<UsageWindow>(res);
}

// ── schedules (recurring agents) ─────────────────────────────────────────────
export type SchedulePayload = {
  name?: string;
  agent_id?: string;
  prompt?: string;
  cron?: string;
  enabled?: boolean;
  max_budget_usd?: number | null;
};

export async function listSchedules(): Promise<Schedule[]> {
  const res = await fetch("/api/schedules", { cache: "no-store" });
  return (await jsonOrThrow<{ schedules: Schedule[] }>(res)).schedules ?? [];
}

export async function createSchedule(body: SchedulePayload): Promise<Schedule> {
  const res = await fetch("/api/schedules", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return jsonOrThrow<Schedule>(res);
}

export async function updateSchedule(id: string, body: SchedulePayload): Promise<Schedule> {
  const res = await fetch(`/api/schedules/${id}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return jsonOrThrow<Schedule>(res);
}

export async function deleteSchedule(id: string): Promise<void> {
  await jsonOrThrow(await fetch(`/api/schedules/${id}`, { method: "DELETE" }));
}

export async function runSchedule(id: string): Promise<{ session_id: string }> {
  return jsonOrThrow(await fetch(`/api/schedules/${id}/run`, { method: "POST" }));
}

// ── scoped API tokens (admin) ────────────────────────────────────────────────
export async function listTokens(): Promise<Token[]> {
  const res = await fetch("/api/tokens", { cache: "no-store" });
  return (await jsonOrThrow<{ tokens: Token[] }>(res)).tokens ?? [];
}

export async function createToken(name: string, scopes: string[]): Promise<Token> {
  const res = await fetch("/api/tokens", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name, scopes }),
  });
  return jsonOrThrow<Token>(res);
}

export async function deleteToken(id: string): Promise<void> {
  await jsonOrThrow(await fetch(`/api/tokens/${id}`, { method: "DELETE" }));
}

// ── secrets (host-scoped header credentials, injected at the egress boundary) ──
// The value is write-only: never returned by GET. Omit `value` on edit to keep
// the stored one. POST upserts by name. 503 → store unavailable (no KEK).
export type SecretPayload = {
  name: string;
  scopes: string[];
  header: string;
  template: string;
  value?: string;
  enabled: boolean;
};

export async function listSecrets(): Promise<Secret[]> {
  const res = await fetch("/api/secrets", { cache: "no-store" });
  return (await jsonOrThrow<{ secrets: Secret[] }>(res)).secrets ?? [];
}

export async function upsertSecret(body: SecretPayload): Promise<Secret> {
  const res = await fetch("/api/secrets", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return jsonOrThrow<Secret>(res);
}

export async function deleteSecret(name: string): Promise<void> {
  await jsonOrThrow(await fetch(`/api/secrets/${encodeURIComponent(name)}`, { method: "DELETE" }));
}

// ── egress policy (the gateway's allow/deny list + audit) ────────────────────
export async function getEgressPolicy(): Promise<EgressPolicy> {
  return jsonOrThrow<EgressPolicy>(await fetch("/api/egress/policy", { cache: "no-store" }));
}

export async function setEgressPolicy(body: { mode?: string; rules?: EgressRule[]; hosts?: EgressHostOverride[]; kill?: boolean; allow_metadata?: boolean }): Promise<EgressPolicy> {
  return jsonOrThrow<EgressPolicy>(await fetch("/api/egress/policy", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }));
}

// ── egress profiles (named allow/deny/inspect bundles, assignable per agent) ──
export async function listEgressProfiles(): Promise<EgressProfile[]> {
  const res = await fetch("/api/egress/profiles", { cache: "no-store" });
  return (await jsonOrThrow<{ profiles: EgressProfile[] }>(res)).profiles ?? [];
}

// A built-in rule bundle, shipped in the canonical rules[] shape (the same one the editor
// and Warden read) so "create from preset" is a seed rather than a conversion.
export type EgressPreset = {
  key: string; name: string; description: string;
  mode: "enforce" | "monitor"; rules: EgressRule[];
};

export async function listEgressPresets(): Promise<EgressPreset[]> {
  const res = await fetch("/api/egress/presets", { cache: "no-store" });
  return (await jsonOrThrow<{ presets: EgressPreset[] }>(res)).presets ?? [];
}

export async function createEgressProfile(body: (Partial<EgressProfile> & { name: string }) | { preset: string; name?: string }): Promise<EgressProfile> {
  return jsonOrThrow<EgressProfile>(await fetch("/api/egress/profiles", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }));
}

export async function updateEgressProfile(id: string, body: Partial<EgressProfile>): Promise<EgressProfile> {
  return jsonOrThrow<EgressProfile>(await fetch(`/api/egress/profiles/${id}`, {
    method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }));
}

export async function deleteEgressProfile(id: string): Promise<void> {
  await jsonOrThrow(await fetch(`/api/egress/profiles/${id}`, { method: "DELETE" }));
}

// ── environments (named {secrets, egress profile} bundles, attachable per agent) ──
export async function listEnvironments(): Promise<Environment[]> {
  const res = await fetch("/api/environments", { cache: "no-store" });
  return (await jsonOrThrow<{ environments: Environment[] }>(res)).environments ?? [];
}

export async function createEnvironment(body: { name: string; description?: string; secrets?: string[]; egress_profile?: string | null }): Promise<Environment> {
  return jsonOrThrow<Environment>(await fetch("/api/environments", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }));
}

export async function updateEnvironment(id: string, body: Partial<Environment>): Promise<Environment> {
  return jsonOrThrow<Environment>(await fetch(`/api/environments/${id}`, {
    method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }));
}

export async function deleteEnvironment(id: string): Promise<void> {
  await jsonOrThrow(await fetch(`/api/environments/${id}`, { method: "DELETE" }));
}

export async function getEgressAudit(limit = 200): Promise<EgressDecision[]> {
  const res = await fetch(`/api/egress/audit?limit=${limit}`, { cache: "no-store" });
  return (await jsonOrThrow<{ decisions: EgressDecision[] }>(res)).decisions ?? [];
}

export type EgressVerification = {
  session_id: string;
  ok: boolean;
  checked: number;
  first_break_seq: number | null;
  gap_before_seq: number | null;
  reason: string;
};

export async function verifySessionEgress(id: string): Promise<EgressVerification> {
  return jsonOrThrow<EgressVerification>(
    await fetch(`/api/sessions/${encodeURIComponent(id)}/egress/verify`, { cache: "no-store" }),
  );
}

export async function getLogs(f: LogFilters = {}): Promise<LogsResponse> {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const res = await fetch(`/api/logs?${p.toString()}`, { cache: "no-store" });
  return jsonOrThrow<LogsResponse>(res);
}
