// Pure helpers for the Tracker tab, the Learned layouts panel and presenter mode (contracts V9).
// Wording rules (design §3 anti-goals): a learned row says it used AI, how many calls, and that code
// checked it; a code row says 0 AI calls; with the scripted agent we say "scripted agent", never imply
// a model ran; held rows say why and what a person can do.
import type { Layout, ParserOrigin, Settings, TrackerResponse, TrackerRow, TrackerTotals } from "../api";
import { JOBS_WORDING, type Wording } from "./integrations";

type S = Pick<Settings, "agent" | "ai_enabled"> | null | undefined;

export const aiCalls = (n: number) => `${n} AI call${n === 1 ? "" : "s"}`;
const scriptedAgent = (s: S) => s?.agent === "scripted";

const FORM_CHANGED = new Set(["CONTRACT_CHANGED", "SINK_CONTRACT"]);
const heldWhy = (reason: string, w: Wording): string | undefined =>
  ({
    PARSE_NO_MATCH: "unknown layout",
    CONTRACT_CHANGED: w.changed,
    SINK_CONTRACT: w.changed,
    VALIDATION_FAILED: w.missing,
    VERIFY_MISMATCH: w.unconfirmed,
    AUTH: "credential rejected",
  } as Record<string, string>)[reason];

/** The short label on a row's chip. `w` words it for the integration (default: the jobs demo). */
export function labelText(row: TrackerRow, s: S, w: Wording = JOBS_WORDING): string {
  if (row.status === "pending") return "sending…";
  if (row.status === "running") return w.writing;
  if (row.status === "learning") return scriptedAgent(s) ? "learning this layout (scripted agent)…" : "learning this layout with AI…";
  if (row.status === "failed") {
    // V13: a run whose spec names an unset credential fails to start ("credential <NAME> is not set: ...")
    const cred = /^credential ([A-Z][A-Z0-9_]*) is not set/.exec(row.detail ?? "");
    if (cred) return `failed: ${cred[1]} not set`;
    return row.reason ? `failed: ${row.reason}` : "failed";
  }
  switch (row.label) {
    case "learned_now":
      return scriptedAgent(s)
        ? `learned now · scripted agent, ${aiCalls(row.ai_calls)} (checked by code)`
        : `learned now · ${aiCalls(row.ai_calls)} (checked by code)`;
    case "held":
      return row.reason && heldWhy(row.reason, w) ? `held: ${heldWhy(row.reason, w)}` : row.reason ? `held: ${row.reason}` : "held";
    case "duplicate":
      return w.duplicate;
    case "code":
    default:
      return `code · ${aiCalls(row.ai_calls)}`;
  }
}

export type LabelKind = "code" | "learned" | "held" | "duplicate" | "learning" | "pending" | "failed";
export function labelKind(row: TrackerRow): LabelKind {
  if (row.status === "pending" || row.status === "running") return "pending";
  if (row.status === "learning") return "learning";
  if (row.status === "failed") return "failed";
  if (row.label === "learned_now") return "learned";
  if (row.label === "held" || row.status === "held") return "held";
  if (row.label === "duplicate" || row.status === "duplicate") return "duplicate";
  return "code";
}

export const isFormChanged = (row: TrackerRow) => !!row.reason && FORM_CHANGED.has(row.reason);

/** What a person can do about a held row (null for rows that aren't held). */
export function heldHint(row: TrackerRow, s: S, w: Wording = JOBS_WORDING): string | null {
  if (row.label !== "held" && row.status !== "held") return null;
  if (row.reason === "PARSE_NO_MATCH") {
    if (s && !s.ai_enabled) return "No reader knows this layout and AI is off, so nothing was learned or written. Turn AI on and replay held pages: Patchwork learns the layout once, checks it in code, and writes the row.";
    if (row.learn_thread_id) return "A reader for this layout was learned but needs a person: check its evidence and approve it in Agent Activity. The page goes in once it is approved.";
    return "No reader knows this layout yet. Replay held pages to learn it once with AI (checked by code).";
  }
  if (isFormChanged(row)) return w.changedHint;
  if (row.reason === "VALIDATION_FAILED") return w.missingHint;
  if (row.reason === "VERIFY_MISMATCH") return w.unconfirmedHint;
  if (row.reason === "AUTH") return "The destination rejected its credential, so nothing was written. Check the credential's value in .env (Patchwork never shows it), restart the control plane, then replay held pages.";
  return row.detail ?? "Held for a person. Check the page, then replay held pages.";
}

/** Local calendar day (YYYY-MM-DD) of an ISO timestamp. */
export function localDay(iso: string | Date): string {
  const d = typeof iso === "string" ? new Date(iso) : iso;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Counts over rows; `day` (local YYYY-MM-DD) keeps only rows captured that day. */
export function totals(rows: TrackerRow[], day?: string): TrackerTotals {
  const rs = day ? rows.filter((r) => localDay(r.captured_at) === day) : rows;
  return {
    pages: rs.length,
    ai_calls: rs.reduce((a, r) => a + (r.ai_calls || 0), 0),
    code: rs.filter((r) => r.label === "code").length,
    learned_now: rs.filter((r) => r.label === "learned_now").length,
    held: rs.filter((r) => r.label === "held").length,
    duplicate: rs.filter((r) => r.label === "duplicate").length,
  };
}

// ------------------------------------------------------------------ layouts
/** Active first; within each group the newest learned/taught first (no date last), then by parser id. */
export function layoutRows(layouts: Layout[]): Layout[] {
  const t = (l: Layout) => (l.origin?.at ? new Date(l.origin.at).getTime() : -Infinity);
  return [...layouts].sort((a, b) => Number(b.active) - Number(a.active) || t(b) - t(a) || a.parser_id.localeCompare(b.parser_id));
}

export function originText(o: ParserOrigin | null | undefined): { how: string; approval: string } {
  if (!o) return { how: "unknown origin", approval: "—" };
  const approval = o.approved_by === "auto" ? "auto-approved: every code check passed" : o.approved_by === "human" ? "approved by a person" : "waiting for a person";
  if (o.kind === "learned") return { how: "learned by AI, checked by code", approval };
  if (o.kind === "taught") return { how: "taught from one recording", approval: o.approved_by ? approval : "—" };
  return { how: "built in (test fixture)", approval: "—" };
}

/** Short local clock time ("14:02") for row timestamps. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** "jobs.lever.co" etc. for a row, with the platform when a fingerprint routed it. */
export function routedText(row: TrackerRow): string {
  if (!row.routed_by) return "";
  if (row.routed_by === "fingerprint") return `recognised by page structure as ${row.parser ?? "?"}`;
  if (row.routed_by === "match") return "matched by site";
  return "default parser";
}

// ------------------------------------------------------------------ generic rows (V15 item 3)
/** What older backends send: title and company on every row, no `field_names`. */
export const LEGACY_FIELDS = ["title", "company"];

/** The columns to show: the response's `field_names`, else title and company. */
export function fieldColumns(resp: Pick<TrackerResponse, "field_names"> | null | undefined): string[] {
  const f = resp?.field_names;
  const cols = Array.isArray(f) ? f.filter((x) => typeof x === "string" && x) : [];
  return cols.length ? cols : LEGACY_FIELDS;
}

/** True when the columns are the jobs demo's title/company, shown the way they always were. */
export const isLegacyColumns = (cols: string[]) => cols.length > 0 && cols.every((c) => c === "title" || c === "company");

/** A row's value for one field: `fields[name]`, else the legacy `title`/`company`. null when empty. */
export function fieldValue(row: TrackerRow, name: string): string | null {
  const has = !!row.fields && typeof row.fields === "object" && name in row.fields;
  const v = has ? row.fields![name] : name === "title" ? row.title : name === "company" ? row.company : undefined;
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** "product" → "Product", "date_applied" → "Date applied". */
export function fieldLabel(name: string): string {
  const s = name.replace(/[_-]+/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : name;
}
