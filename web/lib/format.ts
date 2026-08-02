// Shared display formatting helpers.

export function fmtNum(n: number): string {
  if (!Number.isFinite(n)) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

export function fmtCost(n: number): string {
  if (!n) return "$0";
  if (n < 0.01) return "$" + n.toFixed(4);
  return "$" + n.toFixed(2);
}

export function fmtDuration(ms: number): string {
  if (!ms) return "0ms";
  if (ms < 1000) return Math.round(ms) + "ms";
  const s = ms / 1000;
  if (s < 60) return s.toFixed(1) + "s";
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

export function fmtTime(ts: string | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtClock(ts: string | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour12: false });
}

/** Pull a single human one-liner out of a tool's input — the command / file / url / pattern
 *  it acts on, in priority order. Shared by the transcript's tool summaries and the
 *  permission prompt header. */
export function toolOneLiner(input: Record<string, unknown> | undefined): string {
  if (!input) return "";
  const s = (k: string) => (typeof input[k] === "string" ? (input[k] as string) : "");
  return s("command") || s("file_path") || s("path") || s("url") || s("pattern") || s("query") || "";
}

/** Compact relative age ("just now", "14m", "3h", "2d", then a date). For a fleet list the
 *  question is "how old is this run", which an absolute timestamp makes you compute yourself;
 *  the exact time stays available as a title/tooltip. */
export function fmtAge(ts: string | null | undefined, now = Date.now()): string {
  if (!ts) return "—";
  const t = new Date(ts).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (now - t) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  if (s < 7 * 86400) return `${Math.round(s / 86400)}d`;
  return new Date(t).toLocaleDateString([], { month: "short", day: "numeric" });
}

const LIVE_STATUSES = new Set(["running", "starting", "idle", "ready"]);
export function isLive(status: string | null | undefined): boolean {
  if (!status) return false;
  return LIVE_STATUSES.has(status);
}

