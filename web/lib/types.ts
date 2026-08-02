// Types mirroring the orchestrator API surface.

export type SystemMode = "minimal" | "claude_code" | "assistant" | "custom";
export type PermissionMode =
  | "default"
  | "acceptEdits"
  | "plan"
  | "bypassPermissions";

export type Thinking =
  | { type: "adaptive" }
  | { type: "enabled"; budget_tokens: number }
  | { type: "disabled" }
  | null;

// Claude CLI "thinking level" (effort). Guides reasoning depth.
export type Effort = "low" | "medium" | "high" | "xhigh" | "max";

export type Harness = {
  model: string;
  system_mode: SystemMode;
  custom_prompt: string | null;
  permission_mode: PermissionMode;
  allowed_tools: string[] | null;
  // Availability allowlist. A list restricts the built-ins the agent HAS; the object form is
  // the SDK's preset shape ({type:"preset",preset:"claude_code"}), which the console renders
  // read-only rather than dropping — the Python side accepts it and it must round-trip.
  builtin_tools: string[] | Record<string, unknown> | null;
  thinking: Thinking;
  effort: Effort | null;
  // Model to retry with on overload/refusal.
  fallback_model: string | null;
  // Hard cap on thinking tokens per turn.
  max_thinking_tokens: number | null;
  // API beta flags to opt into.
  betas: string[] | null;
  max_turns: number | null;
  max_budget_usd: number | null;
  mcp_servers: Record<string, unknown> | null;
  // Programmatic subagents: name -> {description, prompt, ...} (Claude SDK AgentDefinition).
  agents?: Record<string, Record<string, unknown>> | null;
  // false=legacy default set; true/"all"=mount+discover; [names]=only these;
  // []=no skills at all (hides the CLI's built-in skills too).
  skills: boolean | string[] | "all";
  // How /memory is provided. volume=durable per-agent mount (costs ~11s of k8s volume attach per
  // launch); synced=snapshot in/out (fast, loses writes since the last turn on an abrupt kill);
  // none=container-local scratch.
  memory_mode?: "volume" | "synced" | "none";
  interactive: boolean;
  approval: "off" | "edits" | "all" | string[];
  setting_sources: string[] | null;
  env: Record<string, unknown> | null;
  extra_options: Record<string, unknown> | null;
  // Attached environments ({secrets, egress} bundles) — the sole per-agent egress + secret
  // scoping. null/[] = no operator secrets under the global egress policy; a non-empty
  // list grants ONLY those environments' secrets and merges egress from their profiles.
  environments?: string[] | null;
};

// Fleet spend over a window (GET /api/usage?days=N), from the DURABLE spend ledger — so
// unlike a fold over the session list, these totals don't shrink when a session is deleted.
export type SpendDay = { day: string; sessions: number; total_cost_usd: number };
export type SpendAgent = { agent_id: string | null; sessions: number; total_cost_usd: number };
export type UsageWindow = {
  window_days: number;
  since: string;
  daily: SpendDay[];
  by_agent: SpendAgent[];
  // Folded from the session logs (not the ledger), so — unlike cost — these do NOT
  // survive session deletion. Server-side because the console pages the session list
  // and folding a page would report page one as if it were the fleet.
  tokens: TokenTotals;
  tool_calls: number;
  by_model: { model: string; total_cost_usd: number }[];
  totals: { sessions: number; total_cost_usd: number };
  all_time: { sessions: number; total_cost_usd: number };
};

// The orchestrator's tool + skill catalog (GET /api/tools). Same reason as ModelInfo: the
// console had its own copy, and the CLI's tool set changes underneath it.
export type ToolCatalog = {
  groups: { label: string; tools: string[] }[];
  presets: Record<string, string[]>;
  defaults: string[];
  skills: string[];
};

// One entry in the orchestrator's model catalog (GET /api/models). The console keeps no
// model list of its own — every picker reads this, so the agent form, the new-session
// dialog and the live switcher cannot offer different sets.
export type ModelInfo = {
  id: string;
  label: string;
  // true = a CLI alias resolved at launch ("sonnet"), false = a pinned generation.
  alias: boolean;
  note?: string;
};

export type Environment = {
  id: string;
  name: string;
  description?: string;
  secrets: string[];
  egress_profile: string | null;
  created_at?: string;
  updated_at?: string;
};

// One firewall rule. `action` allow=opaque tunnel · deny=block (dest-only, every port) ·
// inspect=TLS-terminate + scan. `dest` is a domain, IP literal, or CIDR. `ports` (allow/inspect
// only) lift Warden's default 80/443 wall for that destination; null/[] = default 80/443.
export type EgressRule = {
  action: "allow" | "deny" | "inspect";
  dest: string;
  ports?: number[] | null;
  enabled?: boolean;
  note?: string;
};
// A static resolve override: Warden resolves `host` to `ip` instead of asking DNS — how an
// internal name (e.g. git.internal.example) reaches its private IP when the sandbox's resolver
// can't see your internal DNS server.
export type EgressHostOverride = { host: string; ip: string };
export type EgressProfile = {
  id: string;
  name: string;
  mode: "enforce" | "monitor";
  rules: EgressRule[];
  hosts?: EgressHostOverride[];
  created_at?: string;
  updated_at?: string;
};

export type AgentSpec = {
  id: string;
  name: string;
  harness: Harness;
  memory_scope: string | null;
  memory_volume: string | null;
  version: number;
  created_at: string;
  updated_at: string;
};

export type TokenTotals = {
  input: number;
  output: number;
  cacheRead: number;
  cacheCreate: number;
  subagent: number;  // sub-agent / workflow tokens (main usage is per-turn main-agent only)
  total: number;
};

export type SessionSummary = {
  id: string;
  status: string;
  agent_id: string | null;
  memory_volume: string | null;
  memory_isolated?: boolean;
  model: string | null;
  system_mode: string | null;
  title: string | null;
  // ISO-8601 UTC. The API returns the list newest-first on this key, so views render in
  // API order rather than re-sorting.
  created_ts: string | null;
  user_turns: number;
  tool_calls: number;
  event_count: number;
  total_cost_usd: number;
  tokens: TokenTotals;
  // How the run ended, when it didn't end by the agent finishing: a budget hard-stop or a
  // dead sandbox. null for a clean finish. Both used to look like any other "terminated"
  // row, which hid the two outcomes an operator scans a fleet list to find.
  terminal?: "budget" | "lost" | null;
  // Latest context-window usage. The orchestrator has always returned this ("for
  // supervisors") and the console dropped it on the floor.
  context?: { percentage: number; total_tokens: number; max_tokens: number;
              auto_compact: boolean; compact_threshold: number } | null;
};

/** A page of the session list. `total`/`running` are fleet-wide, not page-wide. */
export type SessionPage = {
  sessions: SessionSummary[];
  next_cursor: string | null;
  total: number;
  running: number;
};

export type LogEvent = {
  seq: number;
  ts: string;
  type: string;
  [key: string]: unknown;
};

export type Schedule = {
  id: string;
  name: string;
  agent_id: string;
  prompt: string;
  cron: string;
  enabled: boolean;
  max_budget_usd: number | null;
  last_run: string | null;
  last_session_id: string | null;
  created_at: string;
};

export type Token = {
  id: string;
  name: string;
  scopes: string[];
  created_at: string;
  token?: string; // present only once, in the create response
};

// A host-scoped header credential. Warden injects `header: <template-with-value>`
// into requests to any host in `scopes`, at the egress boundary — the value never
// enters the sandbox and is NEVER returned by the API (`has_value` reflects whether
// one is stored).
export type Secret = {
  name: string;
  scopes: string[];
  header: string;
  template: string;
  enabled: boolean;
  has_value: boolean;
  created_at?: string;
  updated_at?: string;
};

export type Template = {
  id: string;
  name: string;
  description: string;
  harness: Record<string, unknown>;
};

export type EgressPolicy = {
  mode: "enforce" | "monitor";
  rules: EgressRule[];
  hosts?: EgressHostOverride[];
  kill: boolean;
  allow_metadata: boolean;   // reach the cloud-metadata IP (169.254.169.254) — off by default
  always_allow: string[];
  warden_port: number;
};

export type EgressDecision = {
  ts: string;
  decision: string; // allow | deny | monitor-allow | deny-method | upstream-error | listening
  session_id?: string | null;
  agent_id?: string | null;
  host?: string;
  port?: number;
  reason?: string;
  [k: string]: unknown;
};

export type LogEntry = {
  ts: string;
  source: "event" | "egress";
  session_id: string | null;
  agent_id: string | null;
  type: string | null;
  detail: string;
  host?: string;
  port?: number;
  reason?: string | null;
};

export type LogsResponse = {
  logs: LogEntry[];
  // Where this view stopped looking. `sessions` = matching sessions never opened,
  // `rows` = at least one session had more history than the per-session window,
  // `limit` = the merged result was longer than the requested page.
  truncated?: { sessions: number; rows: boolean; limit: boolean; scan_limit: number };
  facets: {
    agents: string[];
    sessions: { id: string; title: string | null; agent_id: string | null; status: string }[];
    types: string[];
  };
};

export type LogFilters = {
  agent_id?: string; session_id?: string; source?: string; type?: string; q?: string; since?: string; limit?: number;
};

export type Health = {
  ok: boolean;
  runner?: string;
  image?: string;
};

// Payload accepted by the agent create/edit form: the harness, all optional (the API's
// agent PATCH is likewise every harness field optional), plus the two identity fields that
// live on the AgentSpec rather than the harness. Derived from Harness, not re-listed — this
// was a third hand-kept copy of the same 24 fields.
export type AgentPayload = Partial<Harness> & {
  name?: string;
  memory_scope?: string | null;
};
