"use client";

import { useState } from "react";
import { Key, Trash2, Plus, Copy, Check, ShieldCheck } from "lucide-react";
import type { Token } from "@/lib/types";
import { createToken, deleteToken } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Input, Field } from "@/components/ui/input";
import { Tooltip } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ErrorBox, EmptyState, ListSkeleton, ResourceState } from "@/components/ui/feedback";
import { ListRow } from "@/components/ui/list-row";
import { PageContainer } from "@/components/ui/page";
import { ToggleChip } from "@/components/ui/toggle-chip";
import { SectionHeader } from "@/components/ui/section";

const SCOPES = ["read", "run", "admin"] as const;
const SCOPE_HINT: Record<string, string> = { read: "read-only", run: "drive agents", admin: "everything, including credentials" };

export function TokensView({ tokens, loading, error, onChanged }: {
  tokens: Token[]; loading: boolean; error: string | null; onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [created, setCreated] = useState<string | null>(null); // raw token shown once
  const [copied, setCopied] = useState(false);
  const [confirmDel, setConfirmDel] = useState<Token | null>(null);

  function copy() { if (created) { navigator.clipboard.writeText(created); setCopied(true); setTimeout(() => setCopied(false), 1500); } }

  return (
    <PageContainer width="narrow">
      <SectionHeader hint={<span className="text-faint">{SCOPES.map((s) => `${s} = ${SCOPE_HINT[s]}`).join(" · ")}</span>}>
        <Button onClick={() => { setOpen(true); setCreated(null); }}><Plus /> New token</Button>
      </SectionHeader>

      {created && (
        <div className="space-y-2 rounded-lg border p-4" style={{ background: "color-mix(in oklch,var(--c-agent) 8%,transparent)", borderColor: "color-mix(in oklch,var(--c-agent) 30%,var(--border))" }}>
          <div className="flex items-center gap-2 text-sm font-medium">
            <ShieldCheck className="size-4 text-agent" /> Copy this token now. It won&apos;t be shown again.
          </div>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-md bg-surface px-2.5 py-1.5 font-mono text-xs">{created}</code>
            <Button variant="outline" size="sm" onClick={copy} aria-label="Copy token">{copied ? <Check className="size-4 text-agent" /> : <Copy className="size-4" />}</Button>
          </div>
        </div>
      )}

      <ResourceState
        loading={loading}
        error={error}
        isEmpty={tokens.length === 0}
        skeleton={<ListSkeleton rows={3} />}
        empty={
          <EmptyState
            icon={Key}
            title="No scoped tokens"
            description="TERRA_TOKEN already has admin access. Mint scoped tokens so CI and cron callers get only what they need."
            action={<Button onClick={() => { setOpen(true); setCreated(null); }}><Plus /> New token</Button>}
          />
        }
      >
        <div className="space-y-2">
          {tokens.map((t) => (
            <ListRow
              key={t.id}
              icon={Key}
              title={t.name}
              subtitle={<>{t.scopes.join(" · ")}<span className="text-faint"> · {fmtTime(t.created_at)}</span></>}
              actions={<Tooltip label="Revoke token"><Button variant="ghost" size="icon-sm" aria-label="Revoke token" onClick={() => setConfirmDel(t)}><Trash2 /></Button></Tooltip>}
            />
          ))}
        </div>
      </ResourceState>

      <NewTokenDialog open={open} onOpenChange={setOpen} onCreated={(raw) => { setCreated(raw); onChanged(); }} />
      <ConfirmDialog
        open={confirmDel != null}
        onOpenChange={(o) => { if (!o) setConfirmDel(null); }}
        title={`Revoke token "${confirmDel?.name ?? ""}"?`}
        description="Any CI/cron caller using it stops working immediately."
        confirmLabel="Revoke"
        destructive
        onConfirm={async () => { if (confirmDel) { await deleteToken(confirmDel.id); onChanged(); } }}
      />
    </PageContainer>
  );
}

function NewTokenDialog({ open, onOpenChange, onCreated }: {
  open: boolean; onOpenChange: (o: boolean) => void; onCreated: (raw: string | null) => void;
}) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<Set<string>>(new Set(["run"]));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function toggleScope(s: string) { setScopes((p) => { const n = new Set(p); if (n.has(s)) n.delete(s); else n.add(s); return n; }); }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (!name.trim()) { setErr("Name is required."); return; }
    setBusy(true);
    try {
      const t = await createToken(name.trim(), Array.from(scopes.size ? scopes : new Set(["read"])));
      setName(""); setScopes(new Set(["run"]));
      onOpenChange(false); onCreated(t.token ?? null);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>New token</DialogTitle>
          <DialogDescription>Mint a scoped bearer token for a CI or cron caller.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="github-action" autoFocus /></Field>
          <div className="space-y-1.5">
            <div className="text-xs font-medium text-muted">Scopes</div>
            <div className="flex flex-wrap gap-2">
              {SCOPES.map((s) => (
                <ToggleChip key={s} selected={scopes.has(s)} onClick={() => toggleScope(s)}>{s}</ToggleChip>
              ))}
            </div>
            <div className="text-2xs text-faint">{Array.from(scopes).map((s) => SCOPE_HINT[s]).join(" · ") || "no scopes · defaults to read"}</div>
          </div>
          {err && <ErrorBox>{err}</ErrorBox>}
          <div className="flex justify-end gap-2 border-t border-border pt-4">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? "Creating…" : "Create token"}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
