"use client";

import { useEffect, useMemo, useState } from "react";
import { Boxes, Plus, Trash2, SquarePen, KeyRound, ShieldHalf, Box, Layers, CircleSlash, TriangleAlert } from "lucide-react";
import type { AgentSpec, Environment, EgressProfile, Secret } from "@/lib/types";
import { createEnvironment, updateEnvironment, deleteEnvironment } from "@/lib/api";
import { qk, useEnvironments, useEgressProfiles, useInvalidate } from "@/lib/queries";
import { Button } from "@/components/ui/button";
import { Input, Textarea, Field } from "@/components/ui/input";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { ToggleChip } from "@/components/ui/toggle-chip";
import { Tooltip } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ErrorBox, EmptyState, ListSkeleton, ResourceState } from "@/components/ui/feedback";
import { PageContainer } from "@/components/ui/page";
import { SectionHeader } from "@/components/ui/section";
import { HelpNote } from "@/components/ui/help-note";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

// Small labelled chip cluster used inside a card (secrets, agents…).
function ChipCluster({
  icon: Icon,
  label,
  empty,
  children,
}: {
  icon: React.ElementType;
  label: string;
  empty?: React.ReactNode;
  children?: React.ReactNode;
}) {
  const isEmpty = children == null || (Array.isArray(children) && children.length === 0);
  return (
    <div className="flex items-start gap-2">
      <span className="mt-0.5 flex w-24 flex-none items-center gap-1.5 text-2xs text-faint">
        <Icon className="size-3.5" /> {label}
      </span>
      <div className="min-w-0 flex-1">
        {isEmpty ? <span className="text-xs text-faint">{empty}</span> : <div className="flex flex-wrap gap-1.5">{children}</div>}
      </div>
    </div>
  );
}

function Pill({ tone = "var(--muted)", mono, title, children }: { tone?: string; mono?: boolean; title?: string; children: React.ReactNode }) {
  return (
    <span
      title={title}
      className={"inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs " + (mono ? "font-mono " : "")}
      style={{ borderColor: `color-mix(in oklch, ${tone} 30%, var(--border))`, background: `color-mix(in oklch, ${tone} 8%, transparent)`, color: tone }}
    >
      {children}
    </span>
  );
}

function EnvironmentCard({
  env,
  profiles,
  secrets,
  agents,
  onEdit,
  onDelete,
}: {
  env: Environment;
  profiles: EgressProfile[];
  secrets: Secret[];
  agents: AgentSpec[];
  onEdit: () => void;
  onDelete: () => void;
}) {
  const secretByName = useMemo(() => new Map(secrets.map((s) => [s.name, s])), [secrets]);
  const profile = env.egress_profile ? profiles.find((p) => p.id === env.egress_profile) : null;
  const attached = agents.filter((a) => (a.harness.environments ?? []).includes(env.id));

  return (
    <div className="rounded-lg border border-border bg-surface p-4 shadow-soft transition-[border-color] [transition-timing-function:var(--ease)] hover:border-border-2">
      <div className="flex items-start gap-3">
        <div className="grid size-9 flex-none place-items-center rounded-xl" style={{ background: "color-mix(in oklch,var(--accent) 14%,transparent)", color: "var(--accent)" }}>
          <Boxes size={18} strokeWidth={1.8} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-base font-semibold">{env.name}</span>
            <span className="font-mono text-2xs text-faint">{env.id}</span>
          </div>
          {env.description ? (
            <div className="text-xs text-muted">{env.description}</div>
          ) : (
            <div className="text-xs text-faint">No description</div>
          )}
        </div>
        <Button variant="outline" size="sm" onClick={onEdit}><SquarePen className="size-3.5" /> Edit</Button>
        <Tooltip label="Delete environment">
          <Button variant="ghost" size="icon-sm" aria-label={`Delete environment ${env.name}`} onClick={onDelete}><Trash2 /></Button>
        </Tooltip>
      </div>

      <div className="mt-3.5 space-y-2.5 border-t border-border pt-3.5">
        <ChipCluster icon={KeyRound} label="Secrets" empty="No secrets · attached agents get no injected credentials.">
          {env.secrets.map((name) => {
            const s = secretByName.get(name);
            if (!s) return <Pill key={name} tone="var(--c-error)" mono title="This secret no longer exists"><TriangleAlert className="size-3" /> {name}</Pill>;
            if (!s.enabled) return <Pill key={name} tone="var(--muted)" mono title="Secret is disabled · not injected"><CircleSlash className="size-3" /> {name}</Pill>;
            return <Pill key={name} tone="var(--accent)" mono>{name}</Pill>;
          })}
        </ChipCluster>

        <ChipCluster icon={ShieldHalf} label="Egress">
          {profile ? (
            <Pill tone="var(--c-agent)"><Layers className="size-3" /> {profile.name}</Pill>
          ) : env.egress_profile ? (
            <Pill tone="var(--c-error)" mono title="Referenced profile no longer exists"><TriangleAlert className="size-3" /> {env.egress_profile}</Pill>
          ) : (
            <Pill tone="var(--muted)">Global default policy</Pill>
          )}
        </ChipCluster>

        <ChipCluster icon={Box} label="Attached to" empty="Not attached to any agent yet.">
          {attached.map((a) => <Pill key={a.id} tone="var(--c-user)"><Box className="size-3" /> {a.name}</Pill>)}
        </ChipCluster>
      </div>
    </div>
  );
}

export function EnvironmentsView({ agents, secrets }: { agents: AgentSpec[]; secrets: Secret[] }) {
  const envQ = useEnvironments();
  const profilesQ = useEgressProfiles();
  const invalidate = useInvalidate();
  const environments = useMemo(() => envQ.data ?? [], [envQ.data]);
  const profiles = useMemo(() => profilesQ.data ?? [], [profilesQ.data]);

  const [editing, setEditing] = useState<Environment | "new" | null>(null);
  const [confirmDel, setConfirmDel] = useState<Environment | null>(null);

  const onChanged = () => invalidate(qk.environments, qk.agents);

  return (
    <PageContainer width="narrow">
      <SectionHeader hint={environments.length > 0 ? `${environments.length} environment${environments.length === 1 ? "" : "s"}. Each bundles secrets with a network profile.` : undefined}>
        <Button onClick={() => setEditing("new")}><Plus /> New environment</Button>
      </SectionHeader>

      {/* Folded, not deleted. The secrets/egress asymmetry is genuinely counterintuitive and worth
          teaching — but it's the same paragraph every visit, above the data, for an operator who
          learned it the first time. One quiet line closed; the whole model one click away. */}
      <HelpNote label="How attaching an environment changes access">
        An environment bundles <span className="text-text">secrets</span> with an <span className="text-text">egress profile</span>. The two behave
        differently. Secrets are <span className="text-text">scoped</span>: an attached agent gets only these, not every
        enabled secret. Egress <span className="text-text">merges</span> (enforce wins; allowed hosts union), so attaching can add
        reachable hosts, never remove them.
      </HelpNote>

      <ResourceState
        loading={envQ.isLoading}
        error={envQ.error ? errMsg(envQ.error) : null}
        isEmpty={environments.length === 0}
        skeleton={<ListSkeleton rows={3} />}
        empty={
          <EmptyState
            icon={Boxes}
            title="No environments yet"
            description="Scope which secrets an agent receives and which hosts it may reach. Least privilege per agent, without touching the global secret list."
            action={<Button onClick={() => setEditing("new")}><Plus /> New environment</Button>}
          />
        }
      >
        <div className="space-y-2.5">
          {environments.map((env) => (
            <EnvironmentCard
              key={env.id}
              env={env}
              profiles={profiles}
              secrets={secrets}
              agents={agents}
              onEdit={() => setEditing(env)}
              onDelete={() => setConfirmDel(env)}
            />
          ))}
        </div>
      </ResourceState>

      <EnvironmentDialog
        key={editing === "new" ? "new" : editing?.id ?? "closed"}
        env={editing === "new" ? null : editing}
        secrets={secrets}
        profiles={profiles}
        open={editing != null}
        onOpenChange={(o) => { if (!o) setEditing(null); }}
        onSaved={() => { setEditing(null); onChanged(); }}
      />

      <ConfirmDialog
        open={confirmDel != null}
        onOpenChange={(o) => { if (!o) setConfirmDel(null); }}
        title={`Delete environment "${confirmDel?.name ?? ""}"?`}
        description={(() => {
          if (!confirmDel) return "";
          const attached = agents.filter((a) => (a.harness.environments ?? []).includes(confirmDel.id));
          if (attached.length === 0) return "This bundle is removed. Its secrets and egress profile are unaffected.";
          const names = attached.map((a) => a.name).join(", ");
          return `${names} still reference${attached.length === 1 ? "s" : ""} this environment. Detach it from those agents before deleting it.`;
        })()}
        confirmLabel="Delete"
        destructive
        onConfirm={async () => { if (confirmDel) { await deleteEnvironment(confirmDel.id); onChanged(); } }}
      />
    </PageContainer>
  );
}

function EnvironmentDialog({ env, secrets, profiles, open, onOpenChange, onSaved }: {
  env: Environment | null; secrets: Secret[]; profiles: EgressProfile[];
  open: boolean; onOpenChange: (o: boolean) => void; onSaved: () => void;
}) {
  const isEdit = env != null;
  const [name, setName] = useState(env?.name ?? "");
  const [description, setDescription] = useState(env?.description ?? "");
  const [picked, setPicked] = useState<string[]>(env?.secrets ?? []);
  const [egressProfile, setEgressProfile] = useState(env?.egress_profile ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Form reset when the dialog is reopened for a DIFFERENT environment. This state mirrors a
  // prop by design, and there is no render-phase equivalent for clearing a user-edited form.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setName(env?.name ?? ""); setDescription(env?.description ?? "");
    setPicked(env?.secrets ?? []); setEgressProfile(env?.egress_profile ?? ""); setErr(null);
  }, [env]);

  const toggle = (n: string) => setPicked((cur) => (cur.includes(n) ? cur.filter((x) => x !== n) : [...cur, n]));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (!name.trim()) { setErr("Name is required."); return; }
    setBusy(true);
    try {
      const body = { name: name.trim(), description: description.trim(), secrets: picked, egress_profile: egressProfile || null };
      if (isEdit) await updateEnvironment(env.id, body);
      else await createEnvironment(body);
      onSaved();
    } catch (e) { setErr(errMsg(e)); }
    finally { setBusy(false); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit environment · ${env.name}` : "New environment"}</DialogTitle>
          <DialogDescription>A named bundle of secrets + an egress profile. Agents attach it to run under least privilege.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="ci-github" autoFocus={!isEdit} /></Field>
          <Field label="Description" hint="Optional · what this bundle is for.">
            <Textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Read-only GitHub + npm access for CI runs." />
          </Field>

          <Field label="Secrets" hint="Attached agents receive ONLY these credentials. Leave empty for an egress-only environment.">
            {secrets.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border bg-surface-2 px-3 py-2.5 text-xs text-muted">
                No secrets defined yet. Create them under <span className="text-text">Secrets</span> first, then scope them here.
              </div>
            ) : (
              <div className="flex flex-wrap gap-1.5 rounded-lg border border-border bg-surface-2 p-3">
                {secrets.map((s) => (
                  <ToggleChip key={s.name} selected={picked.includes(s.name)} tone="var(--accent)" onClick={() => toggle(s.name)} className="font-mono">
                    {s.name}{!s.enabled && <span className="ml-1 text-faint">· off</span>}
                  </ToggleChip>
                ))}
              </div>
            )}
          </Field>

          <Field label="Egress profile" hint="Hosts attached agents may reach. An agent's environments merge (enforce wins; allowed hosts union) — attaching one can widen reach, never narrow it.">
            <Select value={egressProfile || "__global"} onValueChange={(v) => setEgressProfile(v === "__global" ? "" : v)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__global">Global policy (default)</SelectItem>
                {profiles.map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}
              </SelectContent>
            </Select>
          </Field>

          {err && <ErrorBox>{err}</ErrorBox>}
          <div className="flex justify-end gap-2 border-t border-border pt-4">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? "Saving…" : isEdit ? "Save environment" : "Create environment"}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
