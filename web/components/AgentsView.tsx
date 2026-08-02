"use client";

import { useMemo, useState } from "react";
import { Box, Plus, MoreVertical, Play, SquarePen, Copy, Trash2 } from "lucide-react";
import type { AgentPayload, AgentSpec, Environment } from "@/lib/types";
import { createAgent, deleteAgent, updateAgent } from "@/lib/api";
import { useEgressProfiles, useEnvironments } from "@/lib/queries";
import { thinkingLabel, toolsLabel, personaLabel } from "@/lib/harness";
import { fmtTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/misc";
import { ErrorBox, EmptyState, ResourceState } from "@/components/ui/feedback";
import { PageContainer } from "@/components/ui/page";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { SectionHeader } from "@/components/ui/section";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { AgentForm } from "./AgentForm";

export function AgentsView({ agents, loading, error, onChanged, onNewSession }: {
  agents: AgentSpec[]; loading: boolean; error: string | null; onChanged: () => void; onNewSession: (id: string) => void;
}) {
  const [editing, setEditing] = useState<AgentSpec | "new" | null>(null);
  const [seed, setSeed] = useState<AgentSpec | undefined>(undefined);
  const [submitting, setSubmitting] = useState(false);
  const [formErr, setFormErr] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<AgentSpec | null>(null);
  const [purge, setPurge] = useState(true);

  // Resolve egress/environment ids → names so cards can show network posture at a glance.
  // Memoized `?? []` — see AgentForm: a new array each render defeats the memos below.
  const profilesQ = useEgressProfiles().data;
  const environmentsQ = useEnvironments().data;
  const profiles = useMemo(() => profilesQ ?? [], [profilesQ]);
  const environments = useMemo(() => environmentsQ ?? [], [environmentsQ]);
  const profileName = useMemo(() => new Map(profiles.map((p) => [p.id, p.name])), [profiles]);
  const envName = useMemo(() => new Map(environments.map((e) => [e.id, e.name])), [environments]);
  const envById = useMemo(() => new Map(environments.map((e) => [e.id, e])), [environments]);

  async function submit(payload: AgentPayload) {
    setSubmitting(true); setFormErr(null);
    try {
      if (editing && editing !== "new") await updateAgent(editing.id, payload);
      else await createAgent(payload);
      setEditing(null); setSeed(undefined); onChanged();
    } catch (e) { setFormErr(e instanceof Error ? e.message : String(e)); }
    finally { setSubmitting(false); }
  }
  const initial = editing === "new" ? seed : editing ?? undefined;

  return (
    <PageContainer>
      {/* Count only: the Hud one line above already says what agents are, and "harness" is an SDK
          word the UI never labels anything with — it was glossed inline every time it appeared. */}
      <SectionHeader hint={agents.length > 0 ? `${agents.length} agent${agents.length === 1 ? "" : "s"}` : undefined}>
        <Button onClick={() => { setSeed(undefined); setEditing("new"); }}><Plus /> New agent</Button>
      </SectionHeader>

      <ResourceState
        loading={loading}
        error={error}
        isEmpty={agents.length === 0}
        skeleton={<div className="grid gap-3.5 [grid-template-columns:repeat(auto-fill,minmax(340px,1fr))]">{Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-52 rounded-lg" />)}</div>}
        empty={<Empty onCreate={() => setEditing("new")} />}
      >
        <div className="grid gap-3.5 [grid-template-columns:repeat(auto-fill,minmax(340px,1fr))]">
          {agents.map((a) => (
            <AgentCard key={a.id} a={a} profileName={profileName} envName={envName} envById={envById} onLaunch={() => onNewSession(a.id)} onEdit={() => setEditing(a)}
              onDup={() => { setSeed({ ...a, name: `${a.name}-copy`, id: "" } as AgentSpec); setEditing("new"); }}
              onDelete={() => { setPurge(true); setConfirm(a); }} />
          ))}
        </div>
      </ResourceState>

      <Dialog open={editing != null} onOpenChange={(o) => { if (!o) { setEditing(null); setSeed(undefined); setFormErr(null); } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing && editing !== "new" ? <span className="flex items-center gap-2">Edit {editing.name} <span className="rounded-md bg-surface-2 px-1.5 py-0.5 font-mono text-2xs text-faint">v{editing.version} → v{editing.version + 1}</span></span> : "New agent"}</DialogTitle>
            <DialogDescription>Configure the model, persona, tools, reasoning, and limits.</DialogDescription>
          </DialogHeader>
          {formErr && <ErrorBox>{formErr}</ErrorBox>}
          <AgentForm initial={initial} submitting={submitting} onSubmit={submit} />
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirm != null}
        onOpenChange={(o) => { if (!o) setConfirm(null); }}
        title={`Delete ${confirm?.name ?? ""}?`}
        description="This removes the agent configuration and cannot be undone."
        confirmLabel="Delete"
        destructive
        extra={
          <label className="flex items-center gap-2 rounded-lg border border-border p-3 text-sm">
            <input type="checkbox" checked={purge} onChange={(e) => setPurge(e.target.checked)} className="accent-[var(--accent)]" /> Also purge its memory volume
          </label>
        }
        onConfirm={async () => { if (confirm) { await deleteAgent(confirm.id, purge); onChanged(); } }}
      />
    </PageContainer>
  );
}

function Facet({ color, label, value }: { color: string; label: string; value: string }) {
  return (
    <div className="flex items-center gap-2 overflow-hidden">
      <span className="h-2 w-2 flex-none rounded-full" style={{ background: color }} />
      <span className="flex-none text-xs text-faint">{label}</span>
      <span className="truncate font-mono text-xs text-muted">{value}</span>
    </div>
  );
}

const PERM_SHORT: Record<string, string> = { default: "default", acceptEdits: "acceptEdits", plan: "plan", bypassPermissions: "bypass" };

function AgentCard({ a, profileName, envName, envById, onLaunch, onEdit, onDup, onDelete }: { a: AgentSpec; profileName: Map<string, string>; envName: Map<string, string>; envById: Map<string, Environment>; onLaunch: () => void; onEdit: () => void; onDup: () => void; onDelete: () => void }) {
  const h = a.harness;
  const shared = !!a.memory_scope && a.memory_scope !== a.id;
  const caps = [h.max_turns != null ? `${h.max_turns}t` : null, h.max_budget_usd != null ? `$${h.max_budget_usd}` : null].filter(Boolean).join(" · ") || "uncapped";
  const envs = h.environments ?? [];
  // Egress now comes solely from the attached environments' profiles (no per-agent pin);
  // "global" when none carries egress. Show the profile name for a single source, else a count.
  const egressProfileIds = [...new Set(envs.map((id) => envById.get(id)?.egress_profile).filter(Boolean) as string[])];
  const egress = egressProfileIds.length === 0 ? "global"
    : egressProfileIds.length === 1 ? (profileName.get(egressProfileIds[0]) ?? egressProfileIds[0])
    : `${egressProfileIds.length} profiles`;
  const envLabel = envs.length === 0 ? "none" : envs.length === 1 ? (envName.get(envs[0]) ?? "1") : `${envs.length} attached`;
  return (
    <div className="group flex flex-col rounded-lg border border-border bg-surface p-4 shadow-soft transition-[transform,border-color] duration-200 [transition-timing-function:var(--ease)] hover:-translate-y-0.5 hover:border-border-2">
      <div className="flex items-start gap-3">
        <div className="grid h-[38px] w-[38px] flex-none place-items-center rounded-xl" style={{ background: "color-mix(in oklch,var(--accent) 14%,transparent)", color: "var(--accent)" }}><Box size={19} strokeWidth={1.8} /></div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2"><span className="truncate text-base font-semibold">{a.name}</span><span className="font-mono text-2xs text-faint">v{a.version}</span></div>
          <div className="text-xs text-muted">{personaLabel(h.system_mode)}</div>
        </div>
        <span className="rounded-md border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-2xs text-muted">{h.model}</span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild><Button variant="ghost" size="icon-sm" aria-label={`Actions for ${a.name}`} className="text-muted opacity-60 group-hover:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100"><MoreVertical /></Button></DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={onEdit}><SquarePen /> Edit</DropdownMenuItem>
            <DropdownMenuItem onClick={onDup}><Copy /> Duplicate</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem destructive onClick={onDelete}><Trash2 /> Delete</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-x-3 gap-y-2 border-t border-border pt-3.5">
        <Facet color="var(--c-think)" label="Think" value={thinkingLabel(h.thinking).replace(" thinking", "")} />
        <Facet color="var(--c-tool)" label="Perm" value={PERM_SHORT[h.permission_mode] ?? h.permission_mode} />
        <Facet color="var(--c-user)" label="Tools" value={toolsLabel(h)} />
        <Facet color="var(--c-agent)" label="Skills" value={
          Array.isArray(h.skills) ? (h.skills.length ? `${h.skills.length} picked` : "none")
            : h.skills === "all" ? "all" : h.skills ? "on" : "off"
        } />
        <Facet color="var(--c-result)" label="Caps" value={caps} />
        <Facet color="var(--accent)" label="Mem" value={shared ? (a.memory_scope as string) : "isolated"} />
        <Facet color="var(--c-agent)" label="Egress" value={egress} />
        <Facet color="var(--c-user)" label="Envs" value={envLabel} />
      </div>

      <div className="mt-4 flex items-center gap-2 border-t border-border pt-3">
        {/* Outline, not filled: four filled accent buttons rendered at once on this view (3x Launch +
          New agent), so accent marked every action and therefore none. Launch is also the most
          consequential button here — it spends money and opens a sandbox — and was the easiest
          to mis-click. Primary weight stays with "New agent". */}
      <Button size="sm" variant="outline" className="flex-1" onClick={onLaunch}><Play className="size-3.5" /> Launch session</Button>
        <span className="ml-auto text-2xs text-faint">{fmtTime(a.updated_at)}</span>
      </div>
    </div>
  );
}

function Empty({ onCreate }: { onCreate: () => void }) {
  return (
    <EmptyState
      icon={Box}
      title="No agents yet"
      description="Create your first agent to set its model, persona, tools, reasoning and limits. Then launch a session against it."
      action={<Button onClick={onCreate}><Plus /> New agent</Button>}
    />
  );
}
