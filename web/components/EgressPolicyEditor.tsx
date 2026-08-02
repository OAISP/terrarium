"use client";

import { useState } from "react";
import { Plus, X, AlertTriangle, Globe, Server, Network, Check, Ban, ScanEye, Power, Lock, FlaskConical, Route } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { EgressRule, EgressHostOverride } from "@/lib/types";
import { ACTION_META, type ActionKey, destType, hostCountLabel, effectiveReach, simulate } from "@/lib/egress";

export type EgressDraft = {
  mode: "enforce" | "monitor";
  rules: EgressRule[];
  hosts?: EgressHostOverride[];
  allow_metadata?: boolean;
};

const ACTIONS: ActionKey[] = ["allow", "deny", "inspect"];
// Precedence order (Warden evaluates deny > inspect > allow); we group + sort by it so the
// table reads the way traffic is actually decided — no drag-order (the model is a set, not
// first-match). See lib/egress.ts / warden/src/policy.rs.
const ORDER: Record<ActionKey, number> = { deny: 0, inspect: 1, allow: 2 };

function DestGlyph({ dest }: { dest: string }) {
  const k = destType(dest).kind;
  const Icon = k === "domain" ? Globe : k === "ip" ? Server : Network;
  return <Icon className="size-3.5 flex-none text-faint" aria-label={k} />;
}

function ActionTag({ action }: { action: ActionKey }) {
  const m = ACTION_META[action];
  const Icon = action === "allow" ? Check : action === "deny" ? Ban : ScanEye;
  return (
    <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs font-semibold"
      style={{ color: m.token, background: `color-mix(in oklch, ${m.token} 12%, transparent)`, border: `1px solid color-mix(in oklch, ${m.token} 28%, transparent)` }}>
      <Icon className="size-3" /> {m.label}
    </span>
  );
}

function portsLabel(ports?: number[] | null): string {
  return ports && ports.length ? ports.join(", ") : "80, 443";
}
function parsePorts(raw: string): number[] | null {
  const ps = raw.split(/[\s,]+/).map((s) => Number(s.trim())).filter((n) => Number.isInteger(n) && n >= 1 && n <= 65535);
  return ps.length ? Array.from(new Set(ps)) : null;
}

function RuleRow({ rule, onToggle, onRemove }: { rule: EgressRule; onToggle: () => void; onRemove: () => void }) {
  const m = ACTION_META[rule.action];
  const info = destType(rule.dest);
  const off = rule.enabled === false;
  const count = hostCountLabel(info.hosts);
  return (
    <div className="grid grid-cols-[3px_auto_1fr_auto_auto] items-center gap-2 rounded-md border border-border bg-surface px-2 py-1.5"
      style={{ opacity: off ? 0.5 : 1 }}>
      <span className="h-full w-[3px] rounded-full" style={{ background: off ? "var(--faint)" : m.token }} aria-hidden />
      <ActionTag action={rule.action} />
      <div className="flex min-w-0 items-center gap-1.5">
        <DestGlyph dest={rule.dest} />
        <span className={`truncate font-mono text-xs ${off ? "line-through" : ""}`}>{rule.dest}</span>
        {info.entireInternet && <span className="flex-none rounded px-1 text-2xs font-semibold" style={{ color: "var(--c-error)", background: "color-mix(in oklch,var(--c-error) 14%,transparent)" }}>ENTIRE INTERNET</span>}
        {!info.entireInternet && info.broad && <AlertTriangle className="size-3 flex-none" style={{ color: "var(--c-error)" }} aria-label="broad range" />}
        {count && <span className="flex-none rounded px-1 text-2xs" style={{ color: info.broad ? "var(--c-error)" : "var(--muted)", background: `color-mix(in oklch, ${info.broad ? "var(--c-error)" : "var(--muted)"} 10%, transparent)` }}>{count}</span>}
        {rule.action !== "deny" && <span className="flex-none font-mono text-2xs text-faint" title="allowed ports">:{portsLabel(rule.ports)}</span>}
        {rule.note && <span className="truncate text-2xs text-faint">· {rule.note}</span>}
      </div>
      <span aria-hidden />
      <div className="flex items-center gap-0.5">
        <button type="button" onClick={onToggle} aria-label={off ? "Enable rule" : "Disable rule"} title={off ? "Enable" : "Disable"}
          className="rounded p-1 text-muted transition-colors hover:text-text"><Power className="size-3.5" style={{ color: off ? undefined : "var(--accent)" }} /></button>
        <button type="button" onClick={onRemove} aria-label="Delete rule" className="rounded p-1 text-muted transition-colors hover:text-error"><X className="size-3.5" /></button>
      </div>
    </div>
  );
}

function AddRule({ onAdd }: { onAdd: (r: EgressRule) => boolean }) {
  const [action, setAction] = useState<ActionKey>("allow");
  const [dest, setDest] = useState("");
  const [ports, setPorts] = useState("");
  const [note, setNote] = useState("");
  const [hint, setHint] = useState<string | null>(null);
  const info = dest.trim() ? destType(dest) : null;

  const submit = () => {
    const d = dest.trim().toLowerCase();
    if (!d) return;
    if (d.includes("*")) { setHint("Wildcards aren't supported. Add an exact host, IP, or CIDR."); return; }
    const ok = onAdd({ action, dest: d, ports: action === "deny" ? null : parsePorts(ports), enabled: true, note: note.trim() || undefined });
    if (!ok) { setHint("That rule already exists."); return; }
    setDest(""); setPorts(""); setNote(""); setHint(null);
  };
  return (
    <div className="rounded-md border border-dashed border-border-2 bg-surface-2 p-2">
      <div className="grid grid-cols-[7.5rem_1fr_auto] items-center gap-2">
        <select value={action} onChange={(e) => setAction(e.target.value as ActionKey)} aria-label="Action"
          className="h-8 rounded-md border border-border bg-surface px-2 text-sm text-text outline-none focus-visible:ring-2 focus-visible:ring-accent">
          {ACTIONS.map((a) => <option key={a} value={a}>{ACTION_META[a].label}</option>)}
        </select>
        <div className="flex items-center gap-2">
          <Input value={dest} onChange={(e) => { setDest(e.target.value); if (hint) setHint(null); }}
            placeholder="api.github.com   ·   10.20.0.0/16   ·   192.168.5.10" aria-label="Destination" className="font-mono"
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } }} />
          {info && <span className="flex-none rounded border border-border px-1.5 py-0.5 text-2xs uppercase text-faint">{info.kind}</span>}
        </div>
        <Button size="sm" onClick={submit} aria-label="Add rule"><Plus className="size-4" /> Add</Button>
      </div>
      {action !== "deny" && (
        <div className="mt-2 grid grid-cols-[7.5rem_1fr] items-center gap-2">
          <Input value={ports} onChange={(e) => setPorts(e.target.value)} placeholder="ports (80, 443)" aria-label="Ports"
            className="font-mono" onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } }} />
          <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="note (optional) · why is this reachable?" aria-label="Note"
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } }} />
        </div>
      )}
      {hint && <div className="mt-1.5 text-2xs text-error">{hint}</div>}
      {info?.broad && <div className="mt-1.5 flex items-center gap-1 text-2xs" style={{ color: "var(--c-error)" }}><AlertTriangle className="size-3" /> Broad range{hostCountLabel(info.hosts) ? ` (${hostCountLabel(info.hosts)})` : ""} · exposes the whole range to the agent; prefer a narrower CIDR or exact host.</div>}
    </div>
  );
}

function ReachStrip({ draft }: { draft: EgressDraft }) {
  const r = effectiveReach(draft.rules, draft.mode, !!draft.allow_metadata);
  const tone = r.tone === "danger" ? "var(--c-error)" : r.tone === "warn" ? "var(--c-result)" : "var(--accent)";
  return (
    <div className="flex items-start gap-2 rounded-md border p-2.5 text-xs"
      style={{ borderColor: `color-mix(in oklch, ${tone} 30%, var(--border))`, background: `color-mix(in oklch, ${tone} 8%, transparent)` }}>
      {r.tone !== "safe" && <AlertTriangle className="mt-0.5 size-3.5 flex-none" style={{ color: tone }} />}
      <div className="min-w-0">
        <div className="text-2xs font-semibold uppercase tracking-wide text-faint">Effective reach</div>
        <div className="font-medium" style={{ color: tone }}>✓ Anthropic API · {r.headline}</div>
      </div>
    </div>
  );
}

const IPV4 = /^(\d{1,3}\.){3}\d{1,3}$/;
function HostOverrides({ hosts, onChange }: { hosts: EgressHostOverride[]; onChange: (h: EgressHostOverride[]) => void }) {
  const [host, setHost] = useState("");
  const [ip, setIp] = useState("");
  const [hint, setHint] = useState<string | null>(null);
  const add = () => {
    const h = host.trim().toLowerCase();
    const a = ip.trim();
    if (!h || !a) return;
    if (!IPV4.test(a) && !a.includes(":")) { setHint("Enter a valid IP address."); return; }
    if (hosts.some((x) => x.host === h)) { setHint(`${h} already has an override.`); return; }
    onChange([...hosts, { host: h, ip: a }]);
    setHost(""); setIp(""); setHint(null);
  };
  return (
    <div className="rounded-md border border-border bg-surface-2 p-2.5">
      <div className="mb-1 flex items-center gap-1.5 text-xs font-medium"><Route className="size-3.5 text-muted" /> Host overrides <span className="font-normal text-faint">· resolve an internal name to a fixed IP (bypasses DNS)</span></div>
      <div className="mb-2 text-2xs text-muted">For internal names your DNS server knows but the sandbox&apos;s resolver can&apos;t (split-horizon). You still need an Allow rule for the host or its IP/CIDR.</div>
      {hosts.length > 0 && (
        <div className="mb-2 space-y-1">
          {hosts.map((o) => (
            <div key={o.host} className="flex items-center gap-2 rounded border border-border bg-surface px-2 py-1 font-mono text-xs">
              <span className="min-w-0 flex-1 truncate">{o.host}</span>
              <span className="text-faint">→</span>
              <span className="text-muted">{o.ip}</span>
              <button type="button" onClick={() => onChange(hosts.filter((x) => x.host !== o.host))} aria-label={`Remove override ${o.host}`} className="text-muted hover:text-error"><X className="size-3.5" /></button>
            </div>
          ))}
        </div>
      )}
      <div className="grid grid-cols-[1fr_9rem_auto] items-center gap-2">
        <Input value={host} onChange={(e) => { setHost(e.target.value); if (hint) setHint(null); }} placeholder="git.internal.example" aria-label="Override host" className="font-mono"
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }} />
        <Input value={ip} onChange={(e) => { setIp(e.target.value); if (hint) setHint(null); }} placeholder="10.1.20.50" aria-label="Override IP" className="font-mono"
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }} />
        <Button variant="outline" size="sm" onClick={add} aria-label="Add host override"><Plus className="size-4" /></Button>
      </div>
      {hint && <div className="mt-1.5 text-2xs text-error">{hint}</div>}
    </div>
  );
}

function Simulator({ draft }: { draft: EgressDraft }) {
  const [q, setQ] = useState("");
  const parts = q.trim().split(":");
  const host = parts[0].trim();
  const port = parts[1] ? Number(parts[1]) || 443 : 443;
  const v = host ? simulate(draft.rules, draft.mode, !!draft.allow_metadata, host, port, draft.hosts ?? []) : null;
  const tone = !v ? "var(--muted)" : v.decision === "block" ? "var(--c-error)" : v.decision === "inspect" ? "var(--c-result)" : "var(--accent)";
  return (
    <div className="rounded-md border border-border bg-surface-2 p-2.5">
      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium"><FlaskConical className="size-3.5 text-muted" /> Test a destination</div>
      <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="api.github.com:443   or   10.20.0.5:5432" className="font-mono" aria-label="Test destination" />
      {v && (
        <div className="mt-2 flex items-start gap-2 text-xs">
          <span className="flex-none rounded px-1.5 py-0.5 text-2xs font-semibold uppercase" style={{ color: tone, background: `color-mix(in oklch, ${tone} 12%, transparent)` }}>
            {v.decision === "block" ? "Blocked" : v.decision === "inspect" ? "Allow + Inspect" : "Allowed"}
          </span>
          <span className="text-muted">{v.reason}{v.approximate ? " · (domain resolves at runtime; IP/CIDR rules + the private floor apply to the resolved address)" : ""}</span>
        </div>
      )}
    </div>
  );
}

export function EgressPolicyEditor({
  draft, onChange, alwaysAllow, showMetadata,
}: {
  draft: EgressDraft;
  onChange: (d: EgressDraft) => void;
  alwaysAllow?: string[];
  showMetadata?: boolean;
}) {
  const rules = [...draft.rules].map((r, i) => ({ r, i })).sort((a, b) => ORDER[a.r.action] - ORDER[b.r.action]);
  const setRules = (next: EgressRule[]) => onChange({ ...draft, rules: next });
  const addRule = (rule: EgressRule): boolean => {
    const key = (x: EgressRule) => `${x.action}|${x.dest}|${(x.ports ?? []).join(",")}`;
    if (draft.rules.some((x) => key(x) === key(rule))) return false;
    setRules([...draft.rules, rule]);
    return true;
  };
  const toggleAt = (i: number) => setRules(draft.rules.map((r, j) => (j === i ? { ...r, enabled: r.enabled === false } : r)));
  const removeAt = (i: number) => setRules(draft.rules.filter((_, j) => j !== i));

  return (
    <div className="space-y-3">
      {/* mode — the fallthrough rule (enforce = default-deny, monitor = default-allow + log) */}
      <div className="flex gap-2">
        {(["enforce", "monitor"] as const).map((m) => {
          const on = draft.mode === m;
          const danger = m === "monitor";
          const tone = on ? (danger ? "var(--c-result)" : "var(--accent)") : "var(--border)";
          return (
            <button key={m} type="button" aria-pressed={on} onClick={() => onChange({ ...draft, mode: m })}
              className="flex-1 rounded-lg border p-2.5 text-left transition-colors"
              style={{ borderColor: on ? `color-mix(in oklch, ${tone} 45%, var(--border))` : "var(--border)", background: on ? `color-mix(in oklch, ${tone} 12%, transparent)` : "var(--surface-2)" }}>
              <div className="flex items-center gap-1.5 text-sm font-medium" style={{ color: on ? tone : "var(--text)" }}>
                {danger && on && <AlertTriangle className="size-3.5" />}{m === "enforce" ? "Enforce" : "Monitor"}
              </div>
              <div className="text-xs text-muted">{m === "enforce"
                ? "Default-deny · blocks anything no rule allows"
                : "Default-allow + log · nothing is blocked (floors still apply)"}</div>
            </button>
          );
        })}
      </div>

      <ReachStrip draft={draft} />

      {/* rule table */}
      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-2xs uppercase tracking-wide text-faint">
          <span>Rules</span>
          <span>evaluated by precedence: Block → Inspect → Allow (not top-to-bottom)</span>
        </div>
        {draft.mode === "monitor" && rules.some((x) => x.r.action === "deny") && (
          <div className="flex items-center gap-1 text-2xs" style={{ color: "var(--c-result)" }}>
            <AlertTriangle className="size-3" /> In monitor mode, Block rules are logged but NOT enforced.
          </div>
        )}
        {rules.length === 0 ? (
          <div className="rounded-md border border-dashed border-border bg-surface-2 p-4 text-center text-xs text-muted">
            No rules · in enforce mode this profile reaches only Anthropic; every other host is blocked. Add an Allow rule below.
          </div>
        ) : (
          <div className="space-y-1">
            {rules.map(({ r, i }) => <RuleRow key={`${r.action}-${r.dest}-${i}`} rule={r} onToggle={() => toggleAt(i)} onRemove={() => removeAt(i)} />)}
          </div>
        )}
        <AddRule onAdd={addRule} />
      </div>

      <HostOverrides hosts={draft.hosts ?? []} onChange={(h) => onChange({ ...draft, hosts: h })} />

      <Simulator draft={draft} />

      {alwaysAllow && alwaysAllow.length > 0 && (
        <div className="rounded-md border border-border bg-surface-2 p-2.5">
          <div className="mb-1 flex items-center gap-1.5 text-2xs text-faint"><Lock className="size-3" /> Always reachable · Anthropic. Mandatory, TLS-inspected, and credential-injected. Cannot be removed or denied; only the kill switch stops it.</div>
          <div className="flex flex-wrap gap-1">{alwaysAllow.map((h) => <span key={h} className="rounded bg-surface px-1.5 py-0.5 font-mono text-2xs text-muted">{h}</span>)}</div>
        </div>
      )}

      {showMetadata && (
        <button type="button" role="switch" aria-checked={!!draft.allow_metadata}
          onClick={() => onChange({ ...draft, allow_metadata: !draft.allow_metadata })}
          className="flex w-full items-start gap-3 rounded-lg border p-3 text-left transition-colors"
          style={{ borderColor: draft.allow_metadata ? "color-mix(in oklch,var(--c-error) 45%,var(--border))" : "var(--border)", background: draft.allow_metadata ? "color-mix(in oklch,var(--c-error) 10%,transparent)" : "var(--surface-2)" }}>
          <span aria-hidden className="mt-0.5 inline-flex h-5 w-9 flex-none items-center rounded-full p-0.5 transition-colors"
            style={{ background: draft.allow_metadata ? "var(--c-error)" : "color-mix(in oklch,var(--muted) 40%,transparent)" }}>
            <span className="h-4 w-4 rounded-full bg-white transition-transform" style={{ transform: draft.allow_metadata ? "translateX(16px)" : "none" }} />
          </span>
          <span className="min-w-0">
            <span className="flex items-center gap-1.5 text-sm font-medium" style={{ color: draft.allow_metadata ? "var(--c-error)" : "var(--text)" }}>
              <AlertTriangle className="size-3.5" /> Allow cloud-metadata access
            </span>
            <span className="mt-0.5 block text-xs text-muted">
              Lifts the hard block on <code className="font-mono">169.254.169.254</code> (the classic SSRF→credential pivot).
              The switch alone grants nothing; you still need an Allow rule covering that IP. Leave off unless you specifically need IMDS.
            </span>
          </span>
        </button>
      )}
    </div>
  );
}
