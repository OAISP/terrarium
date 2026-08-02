"use client";

import { useState } from "react";
import { Globe, Layers, Plus, Trash2, SquarePen, Sparkles, ChevronDown, Activity, Box, Boxes, AlertTriangle } from "lucide-react";
import type { AgentSpec, Environment, EgressProfile, EgressDecision, EgressRule } from "@/lib/types";
import { destType, effectiveReach, decisionLabel } from "@/lib/egress";
import {
  setEgressPolicy,
  createEgressProfile,
  updateEgressProfile,
  deleteEgressProfile,
  type EgressPreset,
} from "@/lib/api";
import { fmtClock } from "@/lib/format";
import { qk, useAgents, useEnvironments, useEgressPolicy, useEgressProfiles, useEgressPresets, useEgressAudit, useInvalidate } from "@/lib/queries";
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { Input, Field } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/misc";
import { ErrorBox, EmptyState } from "@/components/ui/feedback";
import { PageContainer } from "@/components/ui/page";
import { ListRow } from "@/components/ui/list-row";
import { HelpNote } from "@/components/ui/help-note";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { EgressPolicyEditor, type EgressDraft } from "@/components/EgressPolicyEditor";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

// What's currently being edited in the dialog: the global default, an existing profile,
// or a brand-new profile (not persisted until Save — so Cancel leaves no orphan).
type Editing = { kind: "default" } | { kind: "profile"; profile: EgressProfile } | { kind: "new" } | null;

function RuleSummary({ mode, rules, allowMetadata }: { mode: "enforce" | "monitor"; rules: EgressRule[]; allowMetadata?: boolean }) {
  const reach = effectiveReach(rules, mode, !!allowMetadata);
  const tone = reach.tone === "danger" ? "var(--c-error)" : reach.tone === "warn" ? "var(--c-result)" : "var(--accent)";
  const dots: { n: number; color: string }[] = [
    { n: reach.counts.allow, color: "var(--accent)" },
    { n: reach.counts.deny, color: "var(--c-error)" },
    { n: reach.counts.inspect, color: "var(--c-result)" },
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
      <span className="rounded-md border px-1.5 py-0.5 font-medium capitalize" style={{
        borderColor: `color-mix(in oklch, ${tone} 30%,var(--border))`,
        background: `color-mix(in oklch, ${tone} 8%,transparent)`, color: tone,
      }}>{mode}</span>
      {dots.map((d, i) => (
        <span key={i} className="flex items-center gap-1 text-muted">
          <span className="size-1.5 rounded-full" style={{ background: d.color }} aria-hidden />
          <span className="tabular-nums">{d.n}</span> {["allow", "block", "inspect"][i]}
        </span>
      ))}
      <span className="text-muted" style={{ color: reach.tone === "safe" ? undefined : tone }}>· {reach.headline}</span>
    </div>
  );
}

// Who references a profile (or the default policy) — the reach an operator needs before
// editing/deleting. `envs` carry it; `agents` are those reached via an attached environment.
function RefLine({ agents, envs }: { agents: AgentSpec[]; envs: Environment[] }) {
  if (agents.length === 0 && envs.length === 0) {
    return <span className="inline-flex items-center gap-1 text-2xs text-faint">Not referenced yet</span>;
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-faint">
      <span>used by</span>
      {agents.map((a) => (
        <span key={a.id} className="inline-flex items-center gap-1 text-muted"><Box className="size-3" style={{ color: "var(--c-user)" }} />{a.name}</span>
      ))}
      {envs.map((e) => (
        <span key={e.id} className="inline-flex items-center gap-1 text-muted"><Boxes className="size-3" style={{ color: "var(--c-agent)" }} />{e.name}</span>
      ))}
    </span>
  );
}

// decision → tone + human label. The deny family is the one an operator scans for, so it keeps the
// error tone and everything routine stays quiet.
//
// The vocabulary itself lives in lib/egress (shared with the Logs "Type" column, which printed the
// same raw tokens); this only maps it to a tone.
function decisionTone(d: string): { color: string; label: string } {
  const label = decisionLabel(d);
  if (d.startsWith("deny") || d === "dlp-block") return { color: "var(--c-error)", label };
  if (d === "upstream-error" || d === "resolve-error" || d === "mitm-error" || d === "dlp-hit")
    return { color: "var(--c-result)", label };
  if (d === "allow" || d === "monitor-allow") return { color: "var(--accent)", label };
  return { color: "var(--muted)", label };
}

function RecentDecisions() {
  const auditQ = useEgressAudit(100);
  const decisions = auditQ.data ?? [];
  // newest first (the audit chain is appended in order)
  const rows = [...decisions].reverse().slice(0, 50);
  const denied = decisions.filter((d) => d.decision.startsWith("deny")).length;

  return (
    <div className="rounded-lg border border-border bg-surface shadow-soft">
      <div className="flex items-center gap-2 border-b border-border px-3.5 py-2.5 text-sm">
        <Activity className="size-4 text-muted" />
        <span className="font-medium">Recent egress decisions</span>
        {denied > 0 && (
          <span className="rounded-md px-1.5 py-0.5 text-2xs font-medium tabular-nums"
            style={{ color: "var(--c-error)", background: "color-mix(in oklch,var(--c-error) 12%,transparent)" }}>
            {denied} denied
          </span>
        )}
        <span className="ml-auto text-2xs text-faint">live · last {rows.length}</span>
      </div>
      {auditQ.isError ? (
        <div className="px-3.5 py-3 text-xs text-muted">Couldn&apos;t load egress decisions.</div>
      ) : rows.length === 0 ? (
        <div className="px-3.5 py-3 text-xs text-faint">No egress yet · decisions appear here as sessions make requests.</div>
      ) : (
        <ul className="max-h-72 divide-y divide-border overflow-auto">
          {rows.map((d: EgressDecision, i) => {
            const t = decisionTone(d.decision);
            return (
              <li key={`${d.ts}-${i}`} className="flex items-center gap-2.5 px-3.5 py-1.5 text-xs">
                <span className="size-1.5 flex-none rounded-full" style={{ background: t.color }} aria-hidden />
                <span className="font-medium tabular-nums" style={{ color: t.color }}>{t.label}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-muted">
                  {d.host ?? "—"}{d.port ? `:${d.port}` : ""}
                  {d.reason ? <span className="text-faint"> · {d.reason}</span> : null}
                </span>
                {d.session_id && (
                  <span className="flex-none font-mono text-2xs text-faint" title={d.session_id}>
                    {d.session_id.slice(-12)}
                  </span>
                )}
                <span className="flex-none tabular-nums text-faint">{fmtClock(d.ts)}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function EgressView() {
  const policyQ = useEgressPolicy();
  const profilesQ = useEgressProfiles();
  const agents = useAgents().data ?? [];
  const environments = useEnvironments().data ?? [];
  const invalidate = useInvalidate();
  const policy = policyQ.data ?? null;
  const profiles = profilesQ.data ?? [];

  // A profile reaches an agent only via an ENVIRONMENT that references it (no per-agent pin).
  const envsForProfile = (id: string) => environments.filter((e) => e.egress_profile === id);
  const refsFor = (id: string) => {
    const envIds = new Set(envsForProfile(id).map((e) => e.id));
    return {
      agents: agents.filter((a) => (a.harness.environments ?? []).some((eid) => envIds.has(eid))),
      envs: environments.filter((e) => e.egress_profile === id),
    };
  };
  // The default policy applies to any agent none of whose attached environments carries egress.
  const envCarriesEgress = new Map(environments.map((e) => [e.id, !!e.egress_profile]));
  const defaultAgents = agents.filter((a) => !(a.harness.environments ?? []).some((eid) => envCarriesEgress.get(eid)));
  const loading = policyQ.isLoading || profilesQ.isLoading;
  const loadErr = policyQ.error ? errMsg(policyQ.error) : profilesQ.error ? errMsg(profilesQ.error) : null;
  const [err, setErr] = useState<string | null>(null); // mutation errors (reads use loadErr)

  // create-profile affordance — name is only committed on Save (see openNewProfile/saveDraft)
  const [newName, setNewName] = useState("");
  const presets = useEgressPresets().data ?? [];

  // editor dialog
  const [editing, setEditing] = useState<Editing>(null);
  const [draft, setDraft] = useState<EgressDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [dialogErr, setDialogErr] = useState<string | null>(null);

  // confirm dialogs
  const [confirmDel, setConfirmDel] = useState<EgressProfile | null>(null);


  function openDefault() {
    if (!policy) return;
    setDraft({ mode: policy.mode, rules: policy.rules, hosts: policy.hosts ?? [], allow_metadata: policy.allow_metadata });
    setDialogErr(null);
    setEditing({ kind: "default" });
  }
  function openProfile(p: EgressProfile) {
    setDraft({ mode: p.mode, rules: p.rules, hosts: p.hosts ?? [] });
    setDialogErr(null);
    setEditing({ kind: "profile", profile: p });
  }

  // Open the create dialog seeded either blank or from a preset. NOTHING is persisted here —
  // the profile is only created on Save, so Cancel/close never leaves an orphan.
  function openNewProfile(seed?: EgressDraft, name?: string) {
    setDraft(seed ?? { mode: "enforce", rules: [] });
    setNewName(name ?? newName.trim());
    setDialogErr(null);
    setEditing({ kind: "new" });
  }
  function createFromPreset(preset: EgressPreset) {
    // Presets ship as rules[] — the same model the editor and Warden use — so this is a
    // seed, not a conversion.
    openNewProfile({ mode: preset.mode, rules: preset.rules }, newName.trim() || preset.name);
  }

  async function removeProfile(p: EgressProfile) {
    // A failed delete must say so. Silently closing the confirm and leaving the row leaves
    // the operator unsure whether a firewall profile still applies.
    setErr(null);
    try {
      await deleteEgressProfile(p.id);
    } catch (e) {
      setErr(`Couldn't delete “${p.name}”: ${errMsg(e)}`);
    }
    invalidate(qk.egressProfiles);
  }

  async function saveDraft() {
    if (!editing || !draft) return;
    if (editing.kind === "new" && !newName.trim()) { setDialogErr("Name is required."); return; }
    // Guardrail: confirm the two highest-impact states before persisting.
    const openInternet = draft.rules.some((r) => r.enabled !== false && r.action === "allow" && destType(r.dest).entireInternet);
    if (openInternet && !window.confirm("This profile allows 0.0.0.0/0. Every public host becomes reachable, defeating the allow-list. Continue?")) return;
    if (editing.kind === "default" && draft.allow_metadata && !policy?.allow_metadata
        && !window.confirm("Enable cloud-metadata access? Agents will be able to reach 169.254.169.254 (a credential-pivot risk) if an Allow rule also covers it. Continue?")) return;
    setSaving(true); setDialogErr(null);
    try {
      if (editing.kind === "default") {
        await setEgressPolicy({ mode: draft.mode, rules: draft.rules, hosts: draft.hosts ?? [], allow_metadata: !!draft.allow_metadata });
        invalidate(qk.egressPolicy);
      } else if (editing.kind === "new") {
        await createEgressProfile({ name: newName.trim(), mode: draft.mode, rules: draft.rules, hosts: draft.hosts ?? [] });
        invalidate(qk.egressProfiles);
        setNewName("");
      } else {
        await updateEgressProfile(editing.profile.id, { mode: draft.mode, rules: draft.rules, hosts: draft.hosts ?? [] });
        invalidate(qk.egressProfiles);
      }
      setEditing(null); setDraft(null);
    } catch (e) { setDialogErr(errMsg(e)); }
    finally { setSaving(false); }
  }

  if (loading) {
    return (
      <PageContainer width="narrow">
        <Skeleton className="h-14 rounded-lg" />
        <Skeleton className="h-12 rounded-lg" />
        <Skeleton className="h-20 rounded-lg" />
        <Skeleton className="h-20 rounded-lg" />
      </PageContainer>
    );
  }

  return (
    <PageContainer width="narrow">
        {policy?.kill && (
          // Frozen is loud and stays on the page — it changes what every agent can do right now, so
          // it is status, not chrome. The CONTROL moved to the global header (see KillSwitch): a
          // panic button you have to navigate to isn't one.
          <div className="flex items-center gap-2 rounded-lg border p-3 text-sm"
            style={{ borderColor: "var(--c-error)", background: "color-mix(in oklch,var(--c-error) 12%,transparent)" }}>
            <span className="size-2 flex-none rounded-full bg-error motion-safe:animate-[terra-breathe_1.4s_ease-in-out_infinite]" aria-hidden />
            <span className="truncate font-medium text-error">EGRESS FROZEN · all traffic blocked, even Anthropic.</span>
          </div>
        )}

        {/* Warden's guarantee is true on every page and changes nothing you do here, so it's a
            footnote you can open — not a standing banner re-teaching the model on every visit. */}
        <HelpNote label="How egress is enforced">
          Warden mediates every session&apos;s egress · allow-listed and audited. It injects the credential
          at the boundary, so the credential never enters the sandbox. Rules below decide what each session may reach.
        </HelpNote>

        {(err ?? loadErr) && <ErrorBox>{err ?? loadErr}</ErrorBox>}

        {/* ── live Warden decisions (what's actually being allowed/denied right now) ── */}
        <RecentDecisions />

        {/* ── default policy (pinned) ── */}
        {policy && (
          <div className="rounded-lg border p-4 shadow-soft" style={{ borderColor: "color-mix(in oklch,var(--accent) 28%,var(--border))", background: "var(--surface)" }}>
            <div className="flex items-start gap-3">
              <div className="grid size-9 flex-none place-items-center rounded-xl" style={{ background: "color-mix(in oklch,var(--accent) 14%,transparent)", color: "var(--accent)" }}>
                <Globe size={18} strokeWidth={1.8} />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-base font-semibold">Default policy</span>
                  <span className="rounded-md border border-border bg-surface-2 px-1.5 py-0.5 text-2xs text-faint">global</span>
                  {policy.allow_metadata && (
                    <span className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-2xs font-medium"
                      style={{ color: "var(--c-error)", background: "color-mix(in oklch,var(--c-error) 12%,transparent)", border: "1px solid color-mix(in oklch,var(--c-error) 28%,transparent)" }}
                      title="Agents can reach the cloud-metadata IP (169.254.169.254)">
                      <AlertTriangle className="size-3" /> metadata allowed
                    </span>
                  )}
                </div>
                <div className="text-xs text-muted">
                  Applied to every agent whose environments carry no egress
                  {defaultAgents.length > 0 && <span className="text-faint"> ({defaultAgents.length} right now)</span>}.
                </div>
              </div>
              <Button variant="outline" size="sm" onClick={openDefault}><SquarePen className="size-3.5" /> Edit</Button>
            </div>
            <div className="mt-3 border-t border-border pt-3">
              <RuleSummary mode={policy.mode} rules={policy.rules} allowMetadata={policy.allow_metadata} />
            </div>
          </div>
        )}

        {/* ── named profiles ── */}
        <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
          <div className="flex items-center gap-2 text-sm font-medium text-text">
            <Layers className="size-4 text-muted" /> Egress profiles
            {/* There is no per-agent pin — a profile reaches an agent only via an Environment that
                references it. The old copy ("assignable per agent") sent operators hunting the agent
                form for a picker that doesn't exist, and contradicted this view's own empty state. */}
            <span className="font-normal text-faint">Named rule bundles. An agent gets one by attaching an Environment.</span>
          </div>
          <div className="flex items-center gap-2">
            {presets.length > 0 && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="outline" size="sm"><Sparkles className="size-4" /> From preset <ChevronDown className="size-3.5" /></Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="max-w-[320px]">
                  {presets.map((p) => (
                    <DropdownMenuItem key={p.key} onSelect={() => createFromPreset(p)} className="flex flex-col items-start gap-0.5 py-2">
                      <span className="text-sm font-medium">{p.name}</span>
                      <span className="text-2xs leading-snug text-muted">{p.description}</span>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            <Button size="sm" onClick={() => openNewProfile()}><Plus className="size-4" /> New profile</Button>
          </div>
        </div>

        {profiles.length === 0 ? (
          <EmptyState icon={Layers} title="No profiles yet" description="Create one with “New profile” above, then attach it to an agent by adding it to an Environment." />
        ) : (
          <div className="space-y-2.5">
            {profiles.map((p) => (
              <ListRow
                key={p.id}
                icon={Layers}
                title={p.name}
                badges={<span className="font-mono text-2xs text-faint">{p.id}</span>}
                subtitle={
                  <div className="space-y-1.5">
                    <RuleSummary mode={p.mode} rules={p.rules} />
                    <RefLine {...refsFor(p.id)} />
                  </div>
                }
                actions={<>
                  <Button variant="outline" size="sm" onClick={() => openProfile(p)}><SquarePen className="size-3.5" /> Edit</Button>
                  <Button variant="ghost" size="icon-sm" aria-label={`Delete profile ${p.name}`} onClick={() => setConfirmDel(p)}><Trash2 /></Button>
                </>}
              />
            ))}
          </div>
        )}

      {/* ── shared editor dialog (default policy · existing profile · new profile) ── */}
      <Dialog open={editing != null} onOpenChange={(o) => { if (!o) { setEditing(null); setDraft(null); setDialogErr(null); } }}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>
              {editing?.kind === "default" ? "Edit default policy" : editing?.kind === "profile" ? `Edit profile · ${editing.profile.name}` : "New egress profile"}
            </DialogTitle>
            <DialogDescription>
              {editing?.kind === "default"
                ? "The global firewall rules an agent inherits when none of its environments carries egress."
                : "A named set of firewall rules. Apply it to an agent by adding it to an Environment the agent attaches to."}
            </DialogDescription>
          </DialogHeader>
          {dialogErr && <ErrorBox>{dialogErr}</ErrorBox>}
          {editing?.kind === "new" && (
            <Field label="Name" hint="Unique identifier, e.g. github+pypi.">
              <Input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="github+pypi" autoFocus />
            </Field>
          )}
          {draft && (
            <EgressPolicyEditor
              draft={draft}
              onChange={setDraft}
              alwaysAllow={editing?.kind === "default" ? policy?.always_allow : undefined}
              showMetadata={editing?.kind === "default"}
            />
          )}
          <div className="flex justify-end gap-2 border-t border-border pt-4">
            <Button variant="outline" onClick={() => { setEditing(null); setDraft(null); setDialogErr(null); }}>Cancel</Button>
            <Button onClick={saveDraft} disabled={saving}>{saving ? "Saving…" : editing?.kind === "default" ? "Save policy" : editing?.kind === "new" ? "Create profile" : "Save profile"}</Button>
          </div>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirmDel != null}
        onOpenChange={(o) => { if (!o) setConfirmDel(null); }}
        title={`Delete profile "${confirmDel?.name ?? ""}"?`}
        description={(() => {
          if (!confirmDel) return "";
          const r = refsFor(confirmDel.id);
          const bits: string[] = [];
          if (r.agents.length) bits.push(`${r.agents.length} agent${r.agents.length === 1 ? "" : "s"}`);
          if (r.envs.length) bits.push(`${r.envs.length} environment${r.envs.length === 1 ? "" : "s"}`);
          if (bits.length) return `${bits.join(" and ")} reference it. Detach or replace it before deleting this profile.`;
          return "This unused profile will be removed.";
        })()}
        confirmLabel="Delete"
        destructive
        onConfirm={async () => { if (confirmDel) await removeProfile(confirmDel); }}
      />
    </PageContainer>
  );
}
