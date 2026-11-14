// Pure helpers for the Tracker tab, the Learned layouts panel and presenter mode (contracts V9).
// Wording rules (design §3 anti-goals): a learned row says it used AI, how many calls, and that code
// checked it; a code row says 0 AI calls; with the scripted agent we say "scripted agent", never imply
// a model ran; held rows say why and what a person can do.
import type { Layout, ParserOrigin, Settings, TrackerRow, TrackerTotals } from "../api";

type S = Pick<Settings, "agent" | "ai_enabled"> | null | undefined;

export const aiCalls = (n: number) => `${n} AI call${n === 1 ? "" : "s"}`;
const scriptedAgent = (s: S) => s?.agent === "scripted";

const FORM_CHANGED = new Set(["CONTRACT_CHANGED", "SINK_CONTRACT"]);
const HELD_WHY: Record<string, string> = {
  PARSE_NO_MATCH: "unknown layout",
  CONTRACT_CHANGED: "form changed",
  SINK_CONTRACT: "form changed",
  VALIDATION_FAILED: "title or company missing",
  VERIFY_MISMATCH: "form did not confirm",
};

/** The short label on a row's chip. */
export function labelText(row: TrackerRow, s: S): string {
  if (row.status === "pending") return "sending…";
  if (row.status === "running") return "writing to the form…";
  if (row.status === "learning") return scriptedAgent(s) ? "learning this layout (scripted agent)…" : "learning this layout with AI…";
  if (row.status === "failed") return row.reason ? `failed: ${row.reason}` : "failed";
  switch (row.label) {
    case "learned_now":
      return scriptedAgent(s)
        ? `learned now · scripted agent, ${aiCalls(row.ai_calls)} (checked by code)`
        : `learned now · ${aiCalls(row.ai_calls)} (checked by code)`;
    case "held":
      return row.reason && HELD_WHY[row.reason] ? `held: ${HELD_WHY[row.reason]}` : row.reason ? `held: ${row.reason}` : "held";
    case "duplicate":
      return "already in tracker";
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
export function heldHint(row: TrackerRow, s: S): string | null {
  if (row.label !== "held" && row.status !== "held") return null;
  if (row.reason === "PARSE_NO_MATCH") {
    if (s && !s.ai_enabled) return "No reader knows this layout and AI is off, so nothing was learned or written. Turn AI on and replay held pages: Patchwork learns the layout once, checks it in code, and writes the row.";
    if (row.learn_thread_id) return "A reader for this layout was learned but needs a person: check its evidence and approve it in Agent Activity. The page goes in once it is approved.";
    return "No reader knows this layout yet. Replay held pages to learn it once with AI (checked by code).";
  }
  if (isFormChanged(row)) return "The tracker form changed since this integration was built, so nothing was written. Repair with AI, check the proposed mapping and approve it; the held pages then go in.";
  if (row.reason === "VALIDATION_FAILED") return "The reader found no title or company on this page, so it was held instead of writing a half-empty row. Open the page to check; if the layout changed, revert its reader and replay to learn it again.";
  if (row.reason === "VERIFY_MISMATCH") return "The form was submitted but never showed its confirmation, so this row may not be in the Sheet. Check the Sheet, then replay held pages.";
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
