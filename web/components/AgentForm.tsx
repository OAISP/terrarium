"use client";

import { useMemo, useState } from "react";
import type { AgentPayload, AgentSpec, Effort, Environment, Harness, PermissionMode, SystemMode, Thinking } from "@/lib/types";
import { useEgressPolicy, useEgressProfiles, useEnvironments, useHealth, useModels, useTemplates, useTools } from "@/lib/queries";
import { Button } from "@/components/ui/button";
import { Input, Textarea, Field } from "@/components/ui/input";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/misc";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { ErrorBox } from "@/components/ui/feedback";
import { ToggleChip } from "@/components/ui/toggle-chip";
import { FieldLabel } from "@/components/ui/stat";

// Models come from the orchestrator (useModels), and so do the tool groups, presets, the
// default auto-approve set and the known skills (useTools → terracore/toolset.py). None of
// it is listed here: a second copy in TypeScript has nothing to keep it in step with the
// worker or with a CLI upgrade.
//
// NOTE on skills: they execute only when the value is "all" or a name list — the bare bool
// `true` mounts skills but does NOT enable the Skill tool, so built-ins stay
// listed-but-unrunnable. Requires the Skill tool (Tools, below).
const EFFORTS: Effort[] = ["low", "medium", "high", "xhigh", "max"];
const SYSTEM_MODES: SystemMode[] = ["minimal", "claude_code", "assistant", "custom"];
const PERMISSION_MODES: PermissionMode[] = ["default", "acceptEdits", "plan", "bypassPermissions"];

type MemoryMode = "volume" | "synced" | "none";
// Say the cost, not the mechanism: the operator is choosing durability vs launch speed.
//
// The trade-off is real only on Kubernetes, where mounting the per-agent RWO PVC costs ~11s
// of volume attach per launch. Docker attaches a local volume in ~0ms, so DockerRunner keeps
// the mount for "synced" too and the two modes are indistinguishable there — quoting an 11s
// saving to a Docker operator describes a different deployment's problem.
const MEMORY_HINT: Record<MemoryMode, Record<"k8s" | "other", string>> = {
  volume: {
    k8s: "Survives anything, but costs ~11s of volume attach on every launch.",
    other: "Survives anything. On Docker the volume attaches instantly, so it costs nothing.",
  },
  synced: {
    k8s: "Restored at launch, saved after each turn, so launches are ~11s faster. Writes since the last turn are lost if the sandbox dies abruptly.",
    other: "Identical to durable on this runner — a local volume attaches instantly, so there is nothing to trade away and no snapshot window.",
  },
  none: {
    k8s: "No memory kept. Fastest launch; correct for agents that never take notes.",
    other: "No memory kept. Correct for agents that never take notes.",
  },
};

type ThinkingKind = "none" | "adaptive" | "enabled" | "disabled";
const thinkingToKind = (t: Thinking): ThinkingKind => (!t ? "none" : t.type === "adaptive" ? "adaptive" : t.type === "disabled" ? "disabled" : "enabled");

function jsonOrNull(raw: string): { ok: true; value: Record<string, unknown> | null } | { ok: false } {
  const t = raw.trim();
  if (!t) return { ok: true, value: null };
  try { const v = JSON.parse(t); if (v === null) return { ok: true, value: null }; if (typeof v !== "object" || Array.isArray(v)) return { ok: false }; return { ok: true, value: v as Record<string, unknown> }; }
  catch { return { ok: false }; }
}
const prettyJson = (v: unknown) => (v == null ? "" : JSON.stringify(v, null, 2));

// Teaches the two consequences of attaching environments: SECRETS get scoped (least
// privilege — only these), while EGRESS profiles MERGE (union of allowed hosts) — the
// second half is the trap the copy must surface, since attaching can only widen reach.
function EnvScopeHint({ attached }: { attached: Environment[] }) {
  const scoped = attached.length > 0;
  const secretCount = new Set(attached.flatMap((e) => e.secrets)).size;
  const withEgress = attached.filter((e) => e.egress_profile).length;
  // Unattached is secret-safe (zero grants) while still inheriting global egress.
  const tone = "var(--accent)";
  return (
    <div className="flex items-start gap-2 rounded-lg border px-2.5 py-2 text-2xs leading-relaxed"
      style={{ borderColor: `color-mix(in oklch, ${tone} 30%, var(--border))`, background: `color-mix(in oklch, ${tone} 8%, transparent)`, color: "var(--muted)" }}>
      <span className="mt-1 size-1.5 flex-none rounded-full" style={{ background: tone }} aria-hidden />
      {scoped ? (
        <span>
          <span className="font-medium text-text">Secrets scoped.</span> Warden injects only the{" "}
          {secretCount} secret{secretCount === 1 ? "" : "s"} from {attached.length} attached environment{attached.length === 1 ? "" : "s"}. Nothing else, even if more secrets are enabled globally.
          {withEgress > 0 && <> Their egress profile{withEgress === 1 ? "" : "s"} <span className="text-text">set the egress</span>, merged into the reachable-host set below.</>}
        </span>
      ) : (
        <span>
          {/* Lead with what the operator holds right now, not how the codebase got here. "Legacy:"
              is migration vocabulary and told them nothing actionable — while fronting the widest
              privilege state in the product. */}
          <span className="font-medium text-text">No operator secrets.</span> With no environment attached, this agent receives no injected secret and runs under the global egress policy. Attach an environment to grant only what it names.
        </span>
      )}
    </div>
  );
}

// The agent's real reachable-host posture, merging the pinned profile + attached environments.
function EffectiveEgress({ allowRules, denyRules, mode }: {
  allowRules: number; denyRules: number; mode: "enforce" | "monitor";
}) {
  const lockdown = mode === "enforce" && allowRules === 0;
  const monitor = mode === "monitor";
  const tone = lockdown ? "var(--accent)" : monitor ? "var(--c-result)" : "var(--c-agent)";
  const text = lockdown
    ? "Anthropic only (lockdown) · every other host is blocked."
    : monitor
      ? `Monitor mode · all destinations are reachable; ${allowRules} allow/inspect rule${allowRules === 1 ? "" : "s"} and ${denyRules} deny rule${denyRules === 1 ? "" : "s"} are logged.`
      : `${allowRules} allow/inspect rule${allowRules === 1 ? "" : "s"} · ${denyRules} deny rule${denyRules === 1 ? "" : "s"} · enforce.`;
  return (
    <div className="flex items-start gap-2 rounded-lg border px-2.5 py-2 text-2xs leading-relaxed"
      style={{ borderColor: `color-mix(in oklch, ${tone} 32%, var(--border))`, background: `color-mix(in oklch, ${tone} 8%, transparent)`, color: "var(--muted)" }}>
      <span className="mt-1 size-1.5 flex-none rounded-full" style={{ background: tone }} aria-hidden />
      <span>
        <span className="font-medium text-text">Effective egress:</span> {text}{" "}
        <span className="text-faint">Merged from the attached environments&apos; egress profiles, else the global policy. Enforce wins; allowed hosts are unioned.</span>
      </span>
    </div>
  );
}

export function AgentForm({ initial, submitting, onSubmit }: { initial?: AgentSpec; submitting?: boolean; onSubmit: (p: AgentPayload) => void }) {
  const h = initial?.harness;
  const [name, setName] = useState(initial?.name ?? "");
  // The orchestrator owns the catalog AND the default (which honours TERRA_MODEL) — a
  // hardcoded fallback here would disagree with what a session actually launches on.
  const catalog = useModels().data;
  // Memoized: `?? []` mints a new array each render, which would change modelOptions' deps
  // every time and defeat its memo.
  const catalogModels = useMemo(() => catalog?.models ?? [], [catalog]);
  const defaultModel = catalog?.default ?? "";
  const [model, setModel] = useState(h?.model ?? "");
  const [systemMode, setSystemMode] = useState<SystemMode>(h?.system_mode ?? "claude_code");
  const [customPrompt, setCustomPrompt] = useState(h?.custom_prompt ?? "");
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(h?.permission_mode ?? "default");
  const [memoryMode, setMemoryMode] = useState<MemoryMode>(h?.memory_mode ?? "synced");
  const [allDefault, setAllDefault] = useState(h ? h.allowed_tools === null : true);
  const [tools, setTools] = useState<Set<string>>(new Set(h?.allowed_tools ?? []));
  const [extraTools, setExtraTools] = useState("");
  // Tool availability (builtin_tools): the base set of built-in tools the agent may use. A
  // whitelist — `available` IS the value (null = all defaults), no complement bookkeeping.
  const [allTools, setAllTools] = useState(h ? h.builtin_tools == null : true);
  // Only the STORED selection seeds this; "all tools" is represented by the toggle, not by
  // an enumerated set. Seeding from ALL_TOOLS here would race the catalog fetch and leave a
  // new agent with an empty availability list the moment the toggle was turned off.
  const [available, setAvailable] = useState<Set<string>>(
    new Set(Array.isArray(h?.builtin_tools) ? (h!.builtin_tools as string[]) : []),
  );
  // New agents default to adaptive (the recommended mode); editing reflects the stored value.
  const [thinkingKind, setThinkingKind] = useState<ThinkingKind>(h ? thinkingToKind(h.thinking) : "adaptive");
  const [thinkingBudget, setThinkingBudget] = useState(h?.thinking && h.thinking.type === "enabled" ? String(h.thinking.budget_tokens) : "20000");
  const [effort, setEffort] = useState<"default" | Effort>(h?.effort ?? "default");
  const [fallbackModel, setFallbackModel] = useState(h?.fallback_model ?? "");
  const [maxThinkingTokens, setMaxThinkingTokens] = useState(h?.max_thinking_tokens != null ? String(h.max_thinking_tokens) : "");
  const [betas, setBetas] = useState((h?.betas ?? []).join(", "));
  const [maxTurns, setMaxTurns] = useState(h?.max_turns != null ? String(h.max_turns) : "");
  const [maxBudget, setMaxBudget] = useState(h?.max_budget_usd != null ? String(h.max_budget_usd) : "");
  // Skills mirror the Tools-availability control: a master switch, then all-vs-specific with a
  // picker. Enabling emits "all" (or a name list) — never the bare bool — so the Skill tool is
  // actually turned on. off = false (default). See KNOWN_SKILLS for why the bool doesn't work.
  const skillsInit = h?.skills ?? "all";  // new agents default to all skills enabled
  const [skillsEnabled, setSkillsEnabled] = useState(
    skillsInit === true || skillsInit === "all" || (Array.isArray(skillsInit) && skillsInit.length > 0));
  const [allSkills, setAllSkills] = useState(!(Array.isArray(skillsInit) && skillsInit.length > 0));
  const [skillSet, setSkillSet] = useState<Set<string>>(new Set(Array.isArray(skillsInit) ? skillsInit : []));
  const [extraSkills, setExtraSkills] = useState(
    Array.isArray(skillsInit) ? skillsInit.filter((s) => !KNOWN_SKILLS.includes(s)).join(", ") : "");
  const [interactive, setInteractive] = useState(h?.interactive ?? false);
  const [approval, setApproval] = useState<"off" | "edits" | "all">(
    h?.approval === "edits" || h?.approval === "all" ? h.approval : "off");
  // Shared, cached, dedup'd via TanStack Query (was 4 ad-hoc useEffect fetches that
  // bypassed the cache and silently swallowed load errors).
  // Memoized `?? []`: a fresh array each render would change the deps of every useMemo below,
  // so those memos would recompute on every render instead of only when the data changes.
  const profilesQ = useEgressProfiles().data;
  const profiles = useMemo(() => profilesQ ?? [], [profilesQ]);
  const globalPolicy = useEgressPolicy().data ?? null;
  const [environments, setEnvironments] = useState<string[]>(h?.environments ?? []);
  const allEnvsQ = useEnvironments().data;
  const allEnvs = useMemo(() => allEnvsQ ?? [], [allEnvsQ]);
  const attachedEnvs = useMemo(() => allEnvs.filter((e) => environments.includes(e.id)), [allEnvs, environments]);
  // Effective egress = the merged egress profiles of the attached environments (the sole
  // per-agent egress mechanism), else the GLOBAL default. Hosts UNION; mode: enforce wins.
  // This is the agent's real reachable-host posture — attaching an environment can only ADD
  // hosts, never remove them.
  const effectiveEgress = useMemo(() => {
    const parts: { mode: string; rules: { action: string; dest: string; enabled?: boolean }[] }[] = [];
    for (const e of attachedEnvs) {
      if (!e.egress_profile) continue;
      const p = profiles.find((x) => x.id === e.egress_profile);
      if (p) parts.push({ mode: p.mode, rules: p.rules });
    }
    // No environment egress resolved → the agent runs under the global policy.
    if (parts.length === 0 && globalPolicy) parts.push({ mode: globalPolicy.mode, rules: globalPolicy.rules });
    let allowRules = 0;
    let denyRules = 0;
    let enforce = false;
    for (const p of parts) {
      p.rules.forEach((r) => {
        if (r.enabled === false) return;
        if (r.action === "allow" || r.action === "inspect") allowRules += 1;
        if (r.action === "deny") denyRules += 1;
      });
      if (p.mode === "enforce") enforce = true;
    }
    return {
      allowRules, denyRules,
      mode: (enforce ? "enforce" : "monitor") as "enforce" | "monitor",
      ready: parts.length > 0,
    };
  }, [attachedEnvs, profiles, globalPolicy]);
  const toggleEnv = (id: string) =>
    setEnvironments((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));
  const [memoryScope, setMemoryScope] = useState(initial?.memory_scope ?? "");
  const [settingSources, setSettingSources] = useState((h?.setting_sources ?? []).join(", "));
  const [mcpServers, setMcpServers] = useState(prettyJson(h?.mcp_servers));
  const [env, setEnv] = useState(prettyJson(h?.env));
  const [extraOptions, setExtraOptions] = useState(prettyJson(h?.extra_options));
  const [error, setError] = useState<string | null>(null);
  // Empty until the catalog loads; every consumer below renders an empty list fine. Plain
  // derivations, not useMemo — the React Compiler memoizes these, and hand-written memos
  // here defeat it (it refuses to optimize a component whose manual memoization it can't
  // preserve, which costs more than the memo saves).
  const runner = useHealth().data?.runner;
  const toolCatalog = useTools().data;
  const TOOL_GROUPS = toolCatalog?.groups ?? [];
  const ALL_TOOLS = TOOL_GROUPS.flatMap((g) => g.tools);
  const TOOL_PRESETS = toolCatalog?.presets ?? {};
  const COMMON_TOOLS = toolCatalog?.defaults ?? [];
  const KNOWN_SKILLS = toolCatalog?.skills ?? [];
  const isEdit = !!initial?.id;

  // One-click presets (create only) — pre-fill the form from a built-in template.
  const templates = useTemplates(!isEdit).data ?? [];
  function applyTemplate(id: string) {
    const t = templates.find((x) => x.id === id);
    if (!t) return;
    const har = t.harness as Record<string, unknown>;
    if (typeof har.model === "string") setModel(har.model);
    if (typeof har.system_mode === "string") setSystemMode(har.system_mode as SystemMode);
    if (typeof har.permission_mode === "string") setPermissionMode(har.permission_mode as PermissionMode);
    if (typeof har.memory_mode === "string") setMemoryMode(har.memory_mode as MemoryMode);
    if (Array.isArray(har.allowed_tools)) { setAllDefault(false); setTools(new Set(har.allowed_tools as string[])); } else { setAllDefault(true); }
    if (Array.isArray(har.builtin_tools)) { setAllTools(false); setAvailable(new Set(har.builtin_tools as string[])); } else { setAllTools(true); }
    const th = har.thinking as { type?: string } | null;
    setThinkingKind(!th ? "none" : th.type === "adaptive" ? "adaptive" : th.type === "disabled" ? "disabled" : "enabled");
    setEffort((typeof har.effort === "string" ? har.effort : "default") as "default" | Effort);
    const sk = har.skills;
    const skArr = Array.isArray(sk) ? (sk as string[]) : null;
    setSkillsEnabled(sk === true || sk === "all" || (skArr != null && skArr.length > 0));
    setAllSkills(!(skArr != null && skArr.length > 0));
    setSkillSet(new Set(skArr ?? []));
    setExtraSkills(skArr ? skArr.filter((s) => !KNOWN_SKILLS.includes(s)).join(", ") : "");
    setInteractive(har.interactive === true);
    setApproval(har.approval === "edits" || har.approval === "all" ? har.approval : "off");
    setFallbackModel(typeof har.fallback_model === "string" ? har.fallback_model : "");
    setMaxThinkingTokens(typeof har.max_thinking_tokens === "number" ? String(har.max_thinking_tokens) : "");
    setBetas(Array.isArray(har.betas) ? (har.betas as string[]).join(", ") : "");
  }

  // Keep a legacy/custom model id selectable when editing an older agent, so its value isn't
  // silently dropped by a list that no longer contains it.
  const modelOptions = useMemo(
    () => (model && !catalogModels.some((m) => m.id === model)
      ? [{ id: model, label: model, alias: false }, ...catalogModels]
      : catalogModels),
    [model, catalogModels],
  );

  const toggleTool = (t: string) => setTools((p) => { const n = new Set(p); if (n.has(t)) n.delete(t); else n.add(t); return n; });
  const toggleAvail = (t: string) => setAvailable((p) => { const n = new Set(p); if (n.has(t)) n.delete(t); else n.add(t); return n; });
  // builtin_tools = the enabled set itself (null when "all tools" is on) — a whitelist.
  const builtinToolsValue: string[] | null = allTools ? null : ALL_TOOLS.filter((t) => available.has(t));
  // Turning per-tool control ON starts from everything enabled — the state the toggle was
  // just showing — so it's a narrowing step, not a surprise reset to nothing.
  function setPerToolControl(all: boolean) {
    if (!all && available.size === 0) setAvailable(new Set(ALL_TOOLS));
    setAllTools(all);
  }
  const allowedToolsValue = useMemo<string[] | null>(() => {
    if (allDefault) return null;
    const extra = extraTools.split(",").map((s) => s.trim()).filter(Boolean);
    return Array.from(new Set([...tools, ...extra]));
  }, [allDefault, tools, extraTools]);
  const toggleSkill = (s: string) => setSkillSet((p) => { const n = new Set(p); if (n.has(s)) n.delete(s); else n.add(s); return n; });
  // false (off) | "all" | [names]. A specific-but-empty selection is [] — the CLI reads that as
  // "no skills at all" (hides even built-ins), which the UI surfaces via the 0-count + hint.
  // Plain derivations (not useMemo) for anything reading the async tool catalog — see the
  // note at the catalog reads above.
  const skillsValue: Harness["skills"] = !skillsEnabled ? false : allSkills ? "all"
    : Array.from(new Set([
        ...KNOWN_SKILLS.filter((s) => skillSet.has(s)),
        ...extraSkills.split(",").map((s) => s.trim()).filter(Boolean),
      ]));
  // Skills only RUN if the Skill tool is available (builtin_tools). With per-tool control on, the
  // Coding/Read-only/None presets all drop Skill — which silently yields an agent that LISTS skills
  // and can run none (the `Execute skill: …` is_error failure). We surface it and block the save
  // rather than auto-adding Skill: silently widening an agent's tool set would undercut the
  // least-privilege model this product exists to enforce. The operator picks the fix.
  const skillToolMissing = skillsEnabled && !allTools && !available.has("Skill");

  function buildThinking(): Thinking {
    switch (thinkingKind) { case "none": return null; case "adaptive": return { type: "adaptive" }; case "disabled": return { type: "disabled" }; case "enabled": return { type: "enabled", budget_tokens: Number(thinkingBudget) || 0 }; }
  }
  function handleSubmit(e: React.FormEvent) {
    e.preventDefault(); setError(null);
    if (!isEdit && !name.trim()) return setError("Name is required.");
    if (skillToolMissing)
      return setError('Skills are on but the "Skill" tool is off under Tools, so the agent would list skills it cannot run. Enable the Skill tool, or turn Skills off.');
    const mcp = jsonOrNull(mcpServers), envP = jsonOrNull(env), extra = jsonOrNull(extraOptions);
    if (!mcp.ok) return setError("mcp_servers is not a valid JSON object.");
    if (!envP.ok) return setError("env is not a valid JSON object.");
    if (!extra.ok) return setError("extra_options is not a valid JSON object.");
    const sources = settingSources.split(",").map((s) => s.trim()).filter(Boolean);
    const betaList = betas.split(",").map((s) => s.trim()).filter(Boolean);
    onSubmit({
      name: name.trim() || undefined, model: model.trim() || defaultModel, system_mode: systemMode,
      custom_prompt: systemMode === "custom" ? customPrompt : null, permission_mode: permissionMode,
      allowed_tools: allowedToolsValue, builtin_tools: builtinToolsValue, thinking: buildThinking(), effort: effort === "default" ? null : effort,
      fallback_model: fallbackModel.trim() || null,
      max_thinking_tokens: maxThinkingTokens.trim() ? Number(maxThinkingTokens) : null,
      betas: betaList.length ? betaList : null,
      max_turns: maxTurns.trim() ? Number(maxTurns) : null, max_budget_usd: maxBudget.trim() ? Number(maxBudget) : null,
      skills: skillsValue, memory_mode: memoryMode, interactive, approval: interactive ? approval : "off",
      setting_sources: sources.length ? sources : null, memory_scope: memoryScope.trim() || null,
      mcp_servers: mcp.value, env: envP.value, extra_options: extra.value,
      environments: environments.length ? environments : null,
    });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      {/* Two tabs, not four. The old taxonomy actively hid security: permission_mode — including
          bypassPermissions, the field deciding whether Bash runs unprompted — sat under a tab
          called "Reasoning & limits", while Environments and the auto-approve list sat under
          "Advanced", so the default config path never met a single access decision. Behavior =
          what it is and how hard it thinks; Access = what it can reach and what it may do
          unasked, ordered the way the runtime applies them. */}
      <Tabs defaultValue="behavior">
        <TabsList className="sticky top-0 z-10 w-full justify-start overflow-x-auto">
          <TabsTrigger value="behavior">Behavior</TabsTrigger>
          <TabsTrigger value="access">Access</TabsTrigger>
        </TabsList>

        <TabsContent value="behavior" className="mt-4 space-y-4 outline-none">
          <div className="grid grid-cols-2 gap-4">
            {!isEdit && templates.length > 0 && (
              <Field label="Start from template" className="col-span-2" hint="Pre-fills the fields below. Tweak anything afterwards.">
                <Select onValueChange={applyTemplate}>
                  <SelectTrigger><SelectValue placeholder="(blank agent)" /></SelectTrigger>
                  <SelectContent>{templates.map((t) => <SelectItem key={t.id} value={t.id}>{t.name} — {t.description}</SelectItem>)}</SelectContent>
                </Select>
              </Field>
            )}
            <Field label="Name" className="col-span-2"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="researcher" disabled={isEdit} /></Field>
            <Field label="Model"><Select value={model || defaultModel} onValueChange={setModel}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent className="max-h-[50vh]">{modelOptions.map((m) => (
              <SelectItem key={m.id} value={m.id}>
                {m.label}{m.alias ? <span className="ml-1.5 text-2xs text-faint">alias</span> : null}
              </SelectItem>
            ))}</SelectContent></Select></Field>
            <Field label="Fallback model" hint="Retried with when the primary is overloaded or refuses. Blank = no fallback.">
              <Select value={fallbackModel || "__none"} onValueChange={(v) => setFallbackModel(v === "__none" ? "" : v)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent className="max-h-[50vh]">
                  <SelectItem value="__none">none</SelectItem>
                  {modelOptions.map((m) => <SelectItem key={m.id} value={m.id}>{m.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </Field>
            <Field label="Persona (system mode)"><Select value={systemMode} onValueChange={(v) => setSystemMode(v as SystemMode)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{SYSTEM_MODES.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}</SelectContent></Select></Field>
          </div>
          {systemMode === "custom" && <Field label="Custom system prompt"><Textarea rows={5} value={customPrompt} onChange={(e) => setCustomPrompt(e.target.value)} placeholder="You are a focused research assistant…" /></Field>}
          <Field label="Memory scope" hint="Empty = isolated to this agent. Set a shared name to pool memory across agents."><Input value={memoryScope} onChange={(e) => setMemoryScope(e.target.value)} placeholder="optional, e.g. shared-research" /></Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Thinking" hint="adaptive recommended. enabled + budget works only on older models (Opus 4.8 / Fable reject it)."><Select value={thinkingKind} onValueChange={(v) => setThinkingKind(v as ThinkingKind)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="none">none (omit)</SelectItem><SelectItem value="adaptive">adaptive</SelectItem><SelectItem value="enabled">enabled + budget</SelectItem><SelectItem value="disabled">disabled</SelectItem></SelectContent></Select></Field>
            <Field label="Thinking level" hint="Guides reasoning depth. xhigh needs Opus 4.7+; Haiku ignores it."><Select value={effort} onValueChange={(v) => setEffort(v as "default" | Effort)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="default">default (model default)</SelectItem>{EFFORTS.map((e) => <SelectItem key={e} value={e}>{e}</SelectItem>)}</SelectContent></Select></Field>
            {thinkingKind === "enabled" && <Field label="Thinking budget (tokens)"><Input type="number" min={1024} value={thinkingBudget} onChange={(e) => setThinkingBudget(e.target.value)} /></Field>}
            <Field label="Max thinking tokens" hint="Hard per-turn ceiling on reasoning. blank = model default"><Input type="number" min={1024} value={maxThinkingTokens} onChange={(e) => setMaxThinkingTokens(e.target.value)} placeholder="model default" /></Field>
            <Field label="Max turns" hint="blank = unlimited"><Input type="number" min={1} value={maxTurns} onChange={(e) => setMaxTurns(e.target.value)} placeholder="unlimited" /></Field>
            <Field label="Max budget (USD)" hint="blank = unlimited · applies live to running sessions"><Input type="number" step="0.01" min={0} value={maxBudget} onChange={(e) => setMaxBudget(e.target.value)} placeholder="unlimited" /></Field>
          </div>
        </TabsContent>

        <TabsContent value="access" className="mt-4 space-y-4 outline-none">
          {/* In the order the runtime applies them: which tools exist at all → which skills may
              run → whether anything pauses for you → what runs unasked → what it carries and
              where it may reach. */}
          <div className="rounded-lg border border-border p-3">
            <div className="flex items-center justify-between">
              <div><div className="text-sm font-medium">Tools it can use</div><div className="text-xs text-muted">Anything off is removed from the agent&apos;s context entirely. Turn off “all tools” for per-tool control.</div></div>
              <label className="flex items-center gap-2 text-xs text-muted">all tools <Switch checked={allTools} onCheckedChange={setPerToolControl} /></label>
            </div>
            {!allTools && (
              <div className="mt-3 space-y-3 border-t border-border pt-3">
                <div className="flex flex-wrap items-center gap-1.5">
                  {Object.keys(TOOL_PRESETS).map((p) => (
                    <button key={p} type="button" onClick={() => setAvailable(new Set(TOOL_PRESETS[p]))} className="rounded-md border border-border px-2 py-1 text-2xs text-muted transition-colors hover:border-accent hover:text-accent">{p}</button>
                  ))}
                  <span className="ml-auto text-2xs tabular-nums text-faint">{available.size}/{ALL_TOOLS.length} enabled</span>
                </div>
                {TOOL_GROUPS.map((g) => (
                  <div key={g.label}>
                    <FieldLabel className="mb-1">{g.label}</FieldLabel>
                    <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
                      {g.tools.map((t) => (
                        <ToggleChip key={t} selected={available.has(t)} tone="var(--c-tool)" onClick={() => toggleAvail(t)} className="truncate text-left font-mono">{t}</ToggleChip>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div className="rounded-lg border border-border p-3">
            <div className="flex items-center justify-between">
              <div><div className="text-sm font-medium">Skills</div><div className="text-xs text-muted">Enable Claude Code skills (deep-research, code-review, …). Needs the <span className="font-mono">Skill</span> tool enabled above; off hides them entirely.</div></div>
              <label className="flex items-center gap-2 text-xs text-muted">enabled <Switch checked={skillsEnabled} onCheckedChange={setSkillsEnabled} /></label>
            </div>
            {skillToolMissing && (
              <div className="mt-3 flex flex-wrap items-center gap-2 rounded-md border px-2.5 py-2 text-xs"
                   style={{ borderColor: "color-mix(in oklch, var(--c-error) 40%, var(--border))",
                            background: "color-mix(in oklch, var(--c-error) 8%, transparent)" }}>
                <span style={{ color: "var(--c-error)" }}>
                  The <span className="font-mono">Skill</span> tool is off under Tools, so these would be listed but could not run.
                </span>
                <button type="button" onClick={() => setAvailable((p) => new Set(p).add("Skill"))}
                        className="ml-auto rounded-md border border-border px-2 py-1 text-2xs text-muted transition-colors hover:border-accent hover:text-accent">
                  Enable the Skill tool
                </button>
                <button type="button" onClick={() => setSkillsEnabled(false)}
                        className="rounded-md border border-border px-2 py-1 text-2xs text-muted transition-colors hover:border-accent hover:text-accent">
                  Turn Skills off
                </button>
              </div>
            )}
            {skillsEnabled && (
              <div className="mt-3 space-y-3 border-t border-border pt-3">
                <label className="flex items-center justify-between gap-2 text-xs text-muted">
                  <span>All skills <span className="text-faint">(built-in and mounted)</span></span>
                  <Switch checked={allSkills} onCheckedChange={setAllSkills} />
                </label>
                {!allSkills && (
                  <>
                    <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
                      {KNOWN_SKILLS.map((s) => (
                        <ToggleChip key={s} selected={skillSet.has(s)} tone="var(--c-tool)" onClick={() => toggleSkill(s)} className="truncate text-left font-mono">{s}</ToggleChip>
                      ))}
                    </div>
                    <Field label="Other skills (comma-separated)" hint="Project or plugin skills not listed above."><Input value={extraSkills} onChange={(e) => setExtraSkills(e.target.value)} placeholder="my-project-skill, plugin:skill" /></Field>
                    <div className="text-2xs tabular-nums text-faint">
                      {(Array.isArray(skillsValue) ? skillsValue.length : 0)} skill{(Array.isArray(skillsValue) ? skillsValue.length : 0) === 1 ? "" : "s"} enabled
                      {Array.isArray(skillsValue) && skillsValue.length === 0 && (
                        <span style={{ color: "var(--c-error)" }}>. None selected: no skills at all, including built-ins.</span>
                      )}
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
          <div className="rounded-lg border border-border p-3">
            <div className="flex items-center justify-between">
              <div><div className="text-sm font-medium">Human in the loop</div><div className="text-xs text-muted">Let the agent ask you questions, and optionally require approval before tools run. Leave off for unattended or scheduled agents.</div></div>
              <Switch checked={interactive} onCheckedChange={setInteractive} />
            </div>
            {interactive && (
              <div className="mt-3 border-t border-border pt-3">
                <Field label="Require approval for" hint="Which tool uses pause for your Allow/Deny. Only applies to this attended session.">
                  <Select value={approval} onValueChange={(v) => setApproval(v as "off" | "edits" | "all")}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="off">Nothing (auto-approve all tools)</SelectItem>
                      <SelectItem value="edits">File writes &amp; edits</SelectItem>
                      <SelectItem value="all">Every tool</SelectItem>
                    </SelectContent>
                  </Select>
                </Field>
              </div>
            )}
          </div>
          <div className="grid grid-cols-2 gap-4">
            {/* Switching modes does NOT migrate what's already stored: "volume" memory lives in a
                per-agent PVC, "synced" memory in an orchestrator-side snapshot. Neither reads the
                other, so without this warning the agent would simply look like it forgot everything. */}
            {isEdit && h?.memory_mode && memoryMode !== h.memory_mode && (
              <div className="col-span-2 rounded-md border px-2.5 py-2 text-2xs leading-relaxed"
                style={{ borderColor: "color-mix(in oklch, var(--c-result) 40%, var(--border))",
                         background: "color-mix(in oklch, var(--c-result) 8%, transparent)", color: "var(--muted)" }}>
                <span className="font-medium" style={{ color: "var(--c-result)" }}>Existing memory won&apos;t carry over.</span>{" "}
                {h.memory_mode} and {memoryMode} store memory in different places. What this agent
                already remembers stays put, and its next session starts with an empty <span className="font-mono">/memory</span>.
              </div>
            )}
            <Field label="Memory" hint={MEMORY_HINT[memoryMode][runner === "k8s" ? "k8s" : "other"]}>
              <Select value={memoryMode} onValueChange={(v) => setMemoryMode(v as MemoryMode)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="volume">durable (persistent volume)</SelectItem>
                  <SelectItem value="synced">synced snapshot{runner === "k8s" ? " (faster launch)" : ""}</SelectItem>
                  <SelectItem value="none">none (ephemeral scratch)</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <Field label="Permission mode" hint="bypassPermissions = nothing ever pauses for you; the auto-approve list below then has no effect."><Select value={permissionMode} onValueChange={(v) => setPermissionMode(v as PermissionMode)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{PERMISSION_MODES.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}</SelectContent></Select></Field>
          </div>
          <div className="rounded-lg border border-border p-3">
            <div className="flex items-center justify-between">
              {/* Availability and auto-approval sit on the same tab, in runtime order, so the
                  labels carry the distinction between them — no cross-reference to a control
                  the reader can't see. */}
              <div><div className="text-sm font-medium">Runs without asking</div><div className="text-xs text-muted">Of the tools it can use, these skip the Allow/Deny prompt. Everything else pauses for you. No effect under <span className="font-mono">bypassPermissions</span>, where nothing pauses at all.</div></div>
              <label className="flex items-center gap-2 text-xs text-muted">default set <Switch checked={allDefault} onCheckedChange={setAllDefault} /></label>
            </div>
            {!allDefault && (
              <div className="mt-3 space-y-3 border-t border-border pt-3">
                <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
                  {COMMON_TOOLS.map((t) => (
                    <ToggleChip key={t} selected={tools.has(t)} tone="var(--c-tool)" onClick={() => toggleTool(t)} className="truncate text-left font-mono">{t}</ToggleChip>
                  ))}
                </div>
                <Field label="Extra tools (comma-separated)"><Input value={extraTools} onChange={(e) => setExtraTools(e.target.value)} placeholder="mcp__server__tool, …" /></Field>
              </div>
            )}
          </div>
          {/* Was 71 words of standing theory (Warden, least-privilege, egress merge semantics,
              "legacy" fallback) rendered as a grey wall above the control, leaking a literal type
              signature — {secrets, egress} — into user-facing copy, with the one actionable fact
              buried last. The live consequence is already computed and shown by EnvScopeHint +
              EffectiveEgress directly below, so the hint only has to say what the control does. */}
          <Field label="Environments" hint="A named bundle of secrets and a network profile. Attach one to grant only its named secrets; attach none to grant no operator secrets. Applies to running sessions.">
            {allEnvs.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border bg-surface-2 px-3 py-2.5 text-xs text-muted">
                No environments defined yet. Create them under <span className="text-text">Environments</span> to scope an agent&apos;s secrets and egress. For egress alone, make one with an egress profile and no secrets.
              </div>
            ) : (
              <div className="space-y-2">
                <div className="flex flex-wrap gap-1.5">
                  {allEnvs.map((e) => (
                    <span key={e.id} title={`${e.secrets.length} secret${e.secrets.length === 1 ? "" : "s"}${e.egress_profile ? " + egress profile" : ""}`}>
                      <ToggleChip selected={environments.includes(e.id)} tone="var(--accent)"
                        onClick={() => toggleEnv(e.id)} className="text-left">
                        {e.name}
                        <span className="ml-1.5 tabular-nums opacity-70">{e.secrets.length}s{e.egress_profile ? "·net" : ""}</span>
                      </ToggleChip>
                    </span>
                  ))}
                </div>
                <EnvScopeHint attached={allEnvs.filter((e) => environments.includes(e.id))} />
              </div>
            )}
          </Field>
          {effectiveEgress.ready && <EffectiveEgress allowRules={effectiveEgress.allowRules} denyRules={effectiveEgress.denyRules} mode={effectiveEgress.mode} />}
          <Field label="API betas (comma-separated)" hint="Anthropic beta flags this agent opts into, e.g. a server-side fallback header."><Input value={betas} onChange={(e) => setBetas(e.target.value)} placeholder="optional" /></Field>
          <Field label="Setting sources (comma-separated)"><Input value={settingSources} onChange={(e) => setSettingSources(e.target.value)} placeholder="optional" /></Field>
          <details className="rounded-lg border border-border bg-surface-2 p-3">
            <summary className="cursor-pointer text-sm font-medium text-text">Raw SDK configuration</summary>
            <p className="mt-2 text-2xs text-muted">Advanced escape hatches. Environment values enter the sandbox and are stored with the agent; use boundary secrets for credentials. Terrarium-managed SDK options cannot be overridden.</p>
            <div className="mt-3 space-y-3">
              <Field label="mcp_servers (JSON object)"><Textarea rows={3} className="font-mono text-xs" value={mcpServers} onChange={(e) => setMcpServers(e.target.value)} placeholder="{}" /></Field>
              <Field label="env (JSON object)" hint="Plaintext values are visible to the agent. Do not put credentials here."><Textarea rows={3} className="font-mono text-xs" value={env} onChange={(e) => setEnv(e.target.value)} placeholder="{}" /></Field>
              <Field label="extra_options (JSON object)" hint="Only SDK fields Terrarium does not manage."><Textarea rows={3} className="font-mono text-xs" value={extraOptions} onChange={(e) => setExtraOptions(e.target.value)} placeholder="{}" /></Field>
            </div>
          </details>
        </TabsContent>
      </Tabs>

      {error && <ErrorBox>{error}</ErrorBox>}
      <div className="flex justify-end gap-2 border-t border-border pt-4"><Button type="submit" disabled={submitting}>{submitting ? "Saving…" : isEdit ? "Save changes" : "Create agent"}</Button></div>
    </form>
  );
}
