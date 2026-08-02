"use client";

import { useEffect, useState } from "react";
import { KeyRound, Trash2, Plus, X, SquarePen, CircleCheck, CircleSlash, Boxes } from "lucide-react";
import type { Environment, Secret } from "@/lib/types";
import { upsertSecret, deleteSecret, type SecretPayload } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input, Field } from "@/components/ui/input";
import { Switch, Tooltip } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ErrorBox, EmptyState, ListSkeleton, ResourceState } from "@/components/ui/feedback";
import { ListRow } from "@/components/ui/list-row";
import { PageContainer } from "@/components/ui/page";
import { SectionHeader } from "@/components/ui/section";

const cleanHost = (v: string) =>
  v.trim().toLowerCase().replace(/^https?:\/\//, "").split("/")[0].split(":")[0];

// A store-unavailable (503, no KEK) failure should read as a setup hint, not a
// scary red error. The proxy surfaces "(503)" or an orchestrator error string.
const isUnavailable = (e: string | null) => !!e && /\b503\b|unavailab|no kek|secrets?_unavail/i.test(e);

function HostChips({ hosts, onRemove }: { hosts: string[]; onRemove?: (h: string) => void }) {
  if (hosts.length === 0) return <span className="text-sm text-faint">no hosts</span>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {hosts.map((h) => (
        <span key={h} className="inline-flex items-center gap-1 rounded-md border px-2 py-1 font-mono text-xs"
          style={{ borderColor: "color-mix(in oklch, var(--accent) 30%, var(--border))", background: "color-mix(in oklch, var(--accent) 8%, transparent)" }}>
          {h}
          {onRemove && <button type="button" aria-label={`Remove ${h}`} onClick={() => onRemove(h)} className="text-muted transition-colors hover:text-error"><X className="size-3" /></button>}
        </span>
      ))}
    </div>
  );
}

// "Blast radius" line: which environments reference this secret.
function UsedBy({ envs }: { envs: Environment[] }) {
  if (envs.length === 0) {
    return <div className="text-2xs text-muted">Used by no environment yet.</div>;
  }
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-muted">
      <span className="inline-flex items-center gap-1"><Boxes className="size-3" /> used by</span>
      {envs.map((e) => (
        <span key={e.id} className="inline-flex items-center gap-1 rounded border border-border bg-surface-2 px-1.5 py-0.5">{e.name}</span>
      ))}
    </div>
  );
}

export function SecretsView({ secrets, environments, loading, error, onChanged }: {
  secrets: Secret[]; environments: Environment[]; loading: boolean; error: string | null; onChanged: () => void;
}) {
  const [editing, setEditing] = useState<Secret | "new" | null>(null);
  const [confirmDel, setConfirmDel] = useState<Secret | null>(null);

  return (
    <PageContainer width="narrow">
      {/* Said once — the Hud subtitle already states what this page is, and the value template
          is taught by the form's live preview (`Authorization: Bearer ••••••`) rather than by a
          paragraph about it. Environments is the section directly above, so no cross-reference. */}
      <SectionHeader hint={<>Injected at the egress boundary. <span className="text-faint">The value never enters the sandbox.</span></>}>
        <Button onClick={() => setEditing("new")}><Plus /> New secret</Button>
      </SectionHeader>

      {isUnavailable(error) ? (
        <EmptyState
          icon={CircleSlash}
          title="Secrets store unavailable"
          description="The orchestrator has no encryption key (KEK) configured, so secrets can't be stored. Set one and reload to manage host-scoped credentials."
        />
      ) : (
        <ResourceState
          loading={loading}
          error={error}
          isEmpty={secrets.length === 0}
          skeleton={<ListSkeleton rows={3} />}
          empty={
            <EmptyState
              icon={KeyRound}
              title="No secrets yet"
              description="Add a host-scoped credential, e.g. a GitHub PAT for api.github.com. Warden injects it at the egress boundary."
              action={<Button onClick={() => setEditing("new")}><Plus /> New secret</Button>}
            />
          }
        >
          <div className="space-y-2">
            {secrets.map((s) => {
              const envs = environments.filter((e) => e.secrets.includes(s.name));
              return (
              <ListRow
                key={s.name}
                icon={KeyRound}
                iconTone={s.enabled ? "var(--accent)" : undefined}
                title={s.name}
                badges={
                  <>
                    <span className="rounded-md border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-2xs text-muted">{s.header}</span>
                    {s.has_value
                      ? <span className="inline-flex items-center gap-1 text-2xs text-accent"><CircleCheck className="size-3" /> value set</span>
                      : <span className="inline-flex items-center gap-1 text-2xs text-faint"><CircleSlash className="size-3" /> no value</span>}
                    {!s.enabled && <span className="rounded-md border border-border bg-surface-2 px-1.5 py-0.5 text-2xs text-faint">disabled</span>}
                  </>
                }
                subtitle={
                  <div className="space-y-1.5">
                    <HostChips hosts={s.scopes} />
                    <UsedBy envs={envs} />
                  </div>
                }
                actions={<>
                  <Button variant="outline" size="sm" onClick={() => setEditing(s)}><SquarePen className="size-3.5" /> Edit</Button>
                  <Tooltip label="Delete secret"><Button variant="ghost" size="icon-sm" aria-label={`Delete secret ${s.name}`} onClick={() => setConfirmDel(s)}><Trash2 /></Button></Tooltip>
                </>}
              />
              );
            })}
          </div>
        </ResourceState>
      )}

      <SecretDialog
        key={editing === "new" ? "new" : editing?.name ?? "closed"}
        secret={editing === "new" ? null : editing}
        open={editing != null}
        onOpenChange={(o) => { if (!o) setEditing(null); }}
        onSaved={() => { setEditing(null); onChanged(); }}
      />
      <ConfirmDialog
        open={confirmDel != null}
        onOpenChange={(o) => { if (!o) setConfirmDel(null); }}
        title={`Delete secret "${confirmDel?.name ?? ""}"?`}
        description={(() => {
          if (!confirmDel) return "";
          const n = environments.filter((e) => e.secrets.includes(confirmDel.name)).length;
          const bits: string[] = [];
          if (n > 0) bits.push(`${n} environment${n === 1 ? "" : "s"}`);
          const scope = bits.length ? `${bits.join(" and ")} lose it. ` : "";
          return `${scope}Warden stops injecting it immediately; requests to its hosts go out without the credential.`;
        })()}
        confirmLabel="Delete"
        destructive
        onConfirm={async () => { if (confirmDel) { await deleteSecret(confirmDel.name); onChanged(); } }}
      />
    </PageContainer>
  );
}

function SecretDialog({ secret, open, onOpenChange, onSaved }: {
  secret: Secret | null; open: boolean; onOpenChange: (o: boolean) => void; onSaved: () => void;
}) {
  const isEdit = secret != null;
  const [name, setName] = useState(secret?.name ?? "");
  const [scopes, setScopes] = useState<string[]>(secret?.scopes ?? []);
  const [hostRaw, setHostRaw] = useState("");
  const [header, setHeader] = useState(secret?.header ?? "Authorization");
  const [template, setTemplate] = useState(secret?.template ?? "Bearer {value}");
  const [value, setValue] = useState("");
  const [enabled, setEnabled] = useState(secret?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Reset when a different secret (or "new") is opened — the keyed remount handles
  // most of it, but keep this defensive so reopening always starts clean.
  // Deliberate belt-and-braces on top of the keyed remount (see above): reopening a secret
  // must always start from a clean form.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setName(secret?.name ?? ""); setScopes(secret?.scopes ?? []); setHostRaw("");
    setHeader(secret?.header ?? "Authorization"); setTemplate(secret?.template ?? "Bearer {value}");
    setValue(""); setEnabled(secret?.enabled ?? true); setErr(null);
  }, [secret]);

  function addHost() {
    const h = cleanHost(hostRaw);
    if (h && !h.includes("*") && !scopes.includes(h)) setScopes((p) => [...p, h]);
    setHostRaw("");
  }

  const templateValid = template.includes("{value}");
  const preview = `${header || "Header"}: ${(template || "{value}").replace("{value}", "••••••")}`;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (!name.trim()) { setErr("Name is required."); return; }
    if (scopes.length === 0) { setErr("Add at least one host to scope this secret."); return; }
    if (!header.trim()) { setErr("Header is required."); return; }
    if (!templateValid) { setErr("Template must contain the literal {value} placeholder."); return; }
    if (!isEdit && !value) { setErr("A value is required to create a secret."); return; }

    const body: SecretPayload = { name: name.trim(), scopes, header: header.trim(), template: template.trim(), enabled };
    if (value) body.value = value; // omit on edit → keep the stored value
    setBusy(true);
    try {
      await upsertSecret(body);
      onSaved();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit secret · ${secret.name}` : "New secret"}</DialogTitle>
          <DialogDescription>Warden injects this header into requests to the scoped hosts. The value never enters the sandbox.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <Field label="Name" hint={isEdit ? "The key · not editable." : "Unique identifier for this credential."}>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="github-pat" disabled={isEdit} autoFocus={!isEdit} />
          </Field>

          <Field label="Scopes" hint="Exact hosts this credential is injected for. Scope is host-level.">
            <div className="rounded-lg border border-border bg-surface-2 p-3">
              <HostChips hosts={scopes} onRemove={(h) => setScopes((p) => p.filter((x) => x !== h))} />
              <div className="mt-2 flex gap-2">
                <Input value={hostRaw} onChange={(e) => setHostRaw(e.target.value)} placeholder="api.github.com" aria-label="Add host" className="font-mono"
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addHost(); } }} />
                <Button type="button" variant="outline" size="sm" aria-label="Add host" onClick={addHost}><Plus className="size-4" /></Button>
              </div>
            </div>
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Header"><Input value={header} onChange={(e) => setHeader(e.target.value)} placeholder="Authorization" className="font-mono" /></Field>
            <Field label="Template" hint="e.g. Bearer {value} · token {value} · {value}">
              <Input value={template} onChange={(e) => setTemplate(e.target.value)} placeholder="Bearer {value}" className="font-mono"
                aria-invalid={!templateValid} aria-describedby={!templateValid ? "secret-template-err" : undefined} />
            </Field>
          </div>
          {!templateValid && <div id="secret-template-err" role="alert" className="text-2xs text-error">Template must contain the literal <code className="font-mono">{`{value}`}</code> placeholder.</div>}

          <div className="rounded-md border border-border bg-surface-2 px-2.5 py-1.5 font-mono text-2xs text-muted">
            <span className="text-faint">preview · </span>{preview}
          </div>

          <Field label="Value" hint={isEdit ? "Leave blank to keep the current value." : "Your PAT / API key. Stored encrypted; never returned."}>
            <Input type="password" value={value} onChange={(e) => setValue(e.target.value)}
              placeholder={isEdit ? "•••••••• (unchanged)" : "ghp_…"} autoComplete="off" />
          </Field>

          <label className="flex items-center justify-between gap-3 rounded-lg border border-border bg-surface-2 px-3 py-2.5">
            <span className="text-sm">
              <span className="font-medium">Enabled</span>
              <span className="ml-2 text-xs text-muted">Warden only injects enabled secrets.</span>
            </span>
            <Switch checked={enabled} onCheckedChange={setEnabled} />
          </label>

          {err && <ErrorBox>{err}</ErrorBox>}
          <div className="flex justify-end gap-2 border-t border-border pt-4">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? "Saving…" : isEdit ? "Save secret" : "Create secret"}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
