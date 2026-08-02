"use client";

import { useMemo, useState } from "react";
import { Clock, Pencil, Play, Trash2, Plus } from "lucide-react";
import type { AgentSpec, Schedule } from "@/lib/types";
import { createSchedule, deleteSchedule, runSchedule, updateSchedule } from "@/lib/api";
import { toast } from "@/components/ui/toast";
import { fmtTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input, Textarea, Field } from "@/components/ui/input";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { Switch, Tooltip } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ErrorBox, EmptyState, ListSkeleton, ResourceState } from "@/components/ui/feedback";
import { ListRow } from "@/components/ui/list-row";
import { PageContainer } from "@/components/ui/page";
import { SectionHeader } from "@/components/ui/section";

export function SchedulesView({ schedules, agents, loading, error, onChanged }: {
  schedules: Schedule[]; agents: AgentSpec[]; loading: boolean; error: string | null; onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Schedule | null>(null);
  const [confirmDel, setConfirmDel] = useState<Schedule | null>(null);
  const agentName = useMemo(() => new Map(agents.map((a) => [a.id, a.name])), [agents]);

  async function act(fn: () => Promise<unknown>) {
    try { await fn(); }
    catch (e) { toast.error(`Action failed: ${e instanceof Error ? e.message : String(e)}`); return; }
    onChanged();
  }

  return (
    <PageContainer>
      <SectionHeader hint="Cron-driven agents. Each run is an ordinary session.">
        <Tooltip label={agents.length === 0 ? "Create an agent first" : "Schedule a recurring run"}>
          <span>
            <Button onClick={() => setOpen(true)} disabled={agents.length === 0}><Plus /> New schedule</Button>
          </span>
        </Tooltip>
      </SectionHeader>

      <ResourceState
        loading={loading}
        error={error}
        isEmpty={schedules.length === 0}
        skeleton={<ListSkeleton rows={3} />}
        empty={
          <EmptyState
            icon={Clock}
            title="No schedules yet"
            description={agents.length === 0
              ? "Create an agent first. Schedules run a registered agent on a cron schedule."
              : "Schedule a registered agent to run on a recurring cron cadence."}
            action={agents.length > 0 ? <Button onClick={() => setOpen(true)}><Plus /> New schedule</Button> : undefined}
          />
        }
      >
        <div className="space-y-2">
          {schedules.map((s) => (
            <ListRow
              key={s.id}
              icon={Clock}
              title={s.name}
              badges={<>
                <Badge variant="secondary">{s.cron}</Badge>
                {!s.enabled && <span className="text-2xs text-faint">paused</span>}
              </>}
              subtitle={<>{agentName.get(s.agent_id) ?? s.agent_id}<span className="text-faint"> · {s.last_run ? `last run ${fmtTime(s.last_run)}` : "never run"}</span></>}
              actions={<>
                <Tooltip label={s.enabled ? "Enabled" : "Paused"}>
                  <span><Switch checked={s.enabled} onCheckedChange={(v) => act(() => updateSchedule(s.id, { enabled: v }))} aria-label="Toggle schedule" /></span>
                </Tooltip>
                <Tooltip label="Run now"><Button variant="ghost" size="icon-sm" aria-label="Run now" onClick={() => act(() => runSchedule(s.id))}><Play /></Button></Tooltip>
                <Tooltip label="Edit schedule"><Button variant="ghost" size="icon-sm" aria-label="Edit schedule" onClick={() => setEditing(s)}><Pencil /></Button></Tooltip>
                <Tooltip label="Delete schedule"><Button variant="ghost" size="icon-sm" aria-label="Delete schedule" onClick={() => setConfirmDel(s)}><Trash2 /></Button></Tooltip>
              </>}
            />
          ))}
        </div>
      </ResourceState>

      <ScheduleDialog
        key={editing?.id ?? (open ? "new" : "closed")}
        open={open || editing != null}
        onOpenChange={(value) => { setOpen(value && editing == null); if (!value) setEditing(null); }}
        agents={agents}
        schedule={editing}
        onSaved={() => { setOpen(false); setEditing(null); onChanged(); }}
      />
      <ConfirmDialog
        open={confirmDel != null}
        onOpenChange={(o) => { if (!o) setConfirmDel(null); }}
        title={`Delete schedule "${confirmDel?.name ?? ""}"?`}
        description="It will stop firing. This can't be undone."
        confirmLabel="Delete"
        destructive
        onConfirm={async () => { if (confirmDel) { await deleteSchedule(confirmDel.id); onChanged(); } }}
      />
    </PageContainer>
  );
}

function ScheduleDialog({ open, onOpenChange, agents, schedule, onSaved }: {
  open: boolean; onOpenChange: (o: boolean) => void; agents: AgentSpec[];
  schedule: Schedule | null; onSaved: () => void;
}) {
  const [name, setName] = useState(schedule?.name ?? "");
  const [agentId, setAgentId] = useState(schedule?.agent_id ?? "");
  const [prompt, setPrompt] = useState(schedule?.prompt ?? "");
  const [cron, setCron] = useState(schedule?.cron ?? "0 7 * * *");
  const [budget, setBudget] = useState(
    schedule?.max_budget_usd == null ? "" : String(schedule.max_budget_usd),
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (!name.trim() || !agentId || !prompt.trim() || !cron.trim()) { setErr("Name, agent, prompt and cron are required."); return; }
    setBusy(true);
    try {
      const body = { name: name.trim(), agent_id: agentId, prompt: prompt.trim(), cron: cron.trim(), max_budget_usd: budget.trim() ? Number(budget) : null };
      if (schedule) await updateSchedule(schedule.id, body);
      else await createSchedule(body);
      onOpenChange(false); onSaved();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{schedule ? "Edit schedule" : "New schedule"}</DialogTitle>
          <DialogDescription>Run a registered agent on a UTC cron schedule.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="nightly triage" autoFocus /></Field>
            <Field label="Agent"><Select value={agentId} onValueChange={setAgentId}><SelectTrigger><SelectValue placeholder="pick an agent" /></SelectTrigger><SelectContent>{agents.map((a) => <SelectItem key={a.id} value={a.id}>{a.name}</SelectItem>)}</SelectContent></Select></Field>
            <Field label="Cron" hint="UTC · min hour dom mon dow"><Input value={cron} onChange={(e) => setCron(e.target.value)} placeholder="0 7 * * *" className="font-mono" /></Field>
            <Field label="Max budget (USD)" hint="blank = agent default"><Input type="number" step="0.01" value={budget} onChange={(e) => setBudget(e.target.value)} placeholder="optional" /></Field>
          </div>
          <Field label="Prompt"><Textarea rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Run the nightly triage and summarize findings." /></Field>
          {err && <ErrorBox>{err}</ErrorBox>}
          <div className="flex justify-end gap-2 border-t border-border pt-4">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? "Saving…" : schedule ? "Save changes" : "Create schedule"}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
