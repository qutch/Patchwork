// Page integrations, destinations and credentials (contracts V13, V14, V15). Pure helpers: the Tracker tab,
// Learned layouts and presenter mode show one page integration at a time, worded for what it does.
//
// Tolerant by design: older backends have no `integrations` on /api/extension, no /api/credentials and no
// `destination` anywhere, so every helper falls back to what the UI showed for jobs_to_tracker before.
import { parse as parseYaml } from "yaml";
import { TRACKER, type Credential, type ThreadStatus, type DestinationInfo, type ExtensionInfo, type IntegrationSummary, type PageIntegration } from "../api";

/** The v1 integration (email → stockroom API): never a page integration. */
export const V1_INTEGRATION = "po_to_stockroom";
export const isJobs = (name: string | null | undefined) => name === TRACKER;

// ------------------------------------------------------------------ which integrations
const jobsFirst = <T extends { name: string }>(xs: T[]) => [...xs].sort((a, b) => Number(isJobs(b.name)) - Number(isJobs(a.name)));

/**
 * The page integrations to choose from: `GET /api/extension` → `integrations` (V15 item 2), enriched with what
 * `/api/integrations` says about each; without that list, every integration but v1's. Never empty.
 */
export function pageIntegrations(ext: ExtensionInfo | null | undefined, summaries: IntegrationSummary[] | null | undefined): PageIntegration[] {
  const byName = new Map((summaries ?? []).map((s) => [s.name, s]));
  const fromExt = (ext?.integrations ?? []).filter((i) => i && typeof i.name === "string" && i.name);
  const list: PageIntegration[] = fromExt.length
    ? fromExt.map((i) => {
        const s = byName.get(i.name);
        return { ...i, intent: i.intent ?? s?.intent ?? null, destination: i.destination ?? i.sink ?? s?.destination ?? s?.sink ?? null };
      })
    : jobsFirst((summaries ?? []).filter((s) => s.name !== V1_INTEGRATION))
        .map((s) => ({ name: s.name, intent: s.intent ?? null, destination: s.destination ?? s.sink ?? null }));
  return list.length ? list : [{ name: TRACKER, intent: null, destination: null }];
}

/** `default_integration` when it is one of the choices, else jobs_to_tracker, else the first. */
export function defaultIntegration(ext: ExtensionInfo | null | undefined, list: PageIntegration[]): string {
  const names = list.map((i) => i.name);
  if (ext?.default_integration && names.includes(ext.default_integration)) return ext.default_integration;
  return names.includes(TRACKER) ? TRACKER : names[0] ?? TRACKER;
}

/** The remembered (or URL) choice when it still exists; the default otherwise. */
export function pickIntegration(wanted: string | null | undefined, list: PageIntegration[], dflt: string): string {
  return wanted && list.some((i) => i.name === wanted) ? wanted : dflt;
}

/**
 * Does an agent thread belong to this integration? The thread says so (`integration`, or a draft's `subject`)
 * when the control plane does; otherwise build and repair threads are the v1 integration's and learn threads
 * the page integrations'.
 */
export function threadBelongs(t: ThreadStatus, name: string): boolean {
  if (t.integration) return t.integration === name;
  if (t.subject === name || t.subject === `draft-${name}`) return true;
  if (t.subject?.startsWith("draft-")) return false;
  return t.mode === "learn" ? name !== V1_INTEGRATION : name === V1_INTEGRATION;
}

/** "track_product_prices" → "Track product prices". */
export function humanName(name: string): string {
  const s = name.replace(/[_-]+/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : name;
}

// ------------------------------------------------------------------ destinations
export interface SinkField {
  /** form label, Airtable field name or API body key */
  label: string;
  /** `from: title`, `const: …`, … as written in the spec (no values of credentials ever appear in a spec) */
  source: Record<string, unknown> | string | null;
}
export interface SpecInfo {
  intent: string | null;
  destination: DestinationInfo | null;
  /** the sink's fields/body, in spec order */
  sinkFields: SinkField[];
}

/** Intent, destination and sink fields from a version's `spec_yaml`; nulls when it can't be read. */
export function specInfo(specYaml: string | null | undefined): SpecInfo {
  const empty: SpecInfo = { intent: null, destination: null, sinkFields: [] };
  if (!specYaml) return empty;
  let doc: unknown;
  try {
    doc = parseYaml(specYaml);
  } catch {
    return empty;
  }
  if (!doc || typeof doc !== "object") return empty;
  const d = doc as Record<string, unknown>;
  const intent = typeof d.intent === "string" ? d.intent : null;
  const sink = d.sink && typeof d.sink === "object" ? (d.sink as Record<string, unknown>) : null;
  if (!sink || typeof sink.kind !== "string") return { ...empty, intent };
  const str = (v: unknown) => (typeof v === "string" ? v : null);
  const auth = sink.auth && typeof sink.auth === "object" ? (sink.auth as Record<string, unknown>) : null;
  const destination: DestinationInfo = {
    kind: sink.kind,
    url: str(sink.url),
    base_url: str(sink.base_url),
    base_id: str(sink.base_id),
    table: str(sink.table),
    credential: str(auth?.credential),
  };
  const raw = (sink.fields ?? sink.body) as unknown;
  const sinkFields: SinkField[] = raw && typeof raw === "object" && !Array.isArray(raw)
    ? Object.entries(raw as Record<string, unknown>).map(([label, v]) => ({ label, source: v && typeof v === "object" ? (v as Record<string, unknown>) : v == null ? null : String(v) }))
    : [];
  return { intent, destination, sinkFields };
}

const hostOf = (u: string | null | undefined) => {
  if (!u) return null;
  try {
    return new URL(u).host;
  } catch {
    return null;
  }
};
export const isGoogleForm = (d: DestinationInfo | null | undefined) => {
  if (d?.kind !== "form" || !d.url) return false;
  try {
    const u = new URL(d.url);
    return (u.hostname === "docs.google.com" && u.pathname.startsWith("/forms")) || u.hostname === "forms.gle";
  } catch {
    return false;
  }
};

/** "Google Form", "web form", "Airtable", "API" (or the raw kind for a sink this UI doesn't know yet). */
export function destinationLabel(d: DestinationInfo | null | undefined): string {
  if (!d) return "destination";
  if (d.kind === "form") return isGoogleForm(d) ? "Google Form" : "web form";
  if (d.kind === "airtable") return "Airtable";
  if (d.kind === "api") return "API";
  return d.kind;
}

/** Where exactly: the form's host, the Airtable base and table, the API's host. */
export function destinationWhere(d: DestinationInfo | null | undefined): string | null {
  if (!d) return null;
  if (d.kind === "airtable") return [d.table && `table ${d.table}`, d.base_id && `base ${d.base_id}`].filter(Boolean).join(" · ") || null;
  if (d.kind === "form") return hostOf(d.url);
  return hostOf(d.base_url ?? d.url);
}

/** Page head eyebrow: "Job postings → tracker form" for the jobs demo, else "<intent> → <destination kind>". */
export function eyebrowFor(name: string, intent: string | null | undefined, d: DestinationInfo | null | undefined): string {
  if (isJobs(name)) return `Job postings → ${!d || d.kind === "form" ? "tracker form" : destinationLabel(d)}`;
  return `${intent?.trim() || humanName(name)} → ${destinationLabel(d)}`;
}

/** What each destination field is filled from, in words (presenter's "shown once" view). */
export function sourceText(src: SinkField["source"]): string {
  if (src == null) return "—";
  if (typeof src === "string") return src;
  if (typeof src.from === "string") {
    if (src.from === "record.canonical_url") return "the page's canonical URL";
    if (src.from === "record.captured_date") return "the day you pressed the hotkey";
    if (src.from.startsWith("record.")) return `the page's ${src.from.slice(7).replace(/_/g, " ")}`;
    return `${src.from.replace(/_/g, " ")} read from the page`;
  }
  if ("const" in src) return "always the same value";
  return JSON.stringify(src);
}

// ------------------------------------------------------------------ credentials (V13: names only, never values)
/** Credential names this integration uses that are not set in `.env`. */
export function missingCredentials(creds: Credential[] | null | undefined, name: string, d?: DestinationInfo | null): string[] {
  const out = (creds ?? []).filter((c) => !c.set && ((c.used_by ?? []).includes(name) || (!!d?.credential && c.name === d.credential))).map((c) => c.name);
  return [...new Set(out)];
}

// ------------------------------------------------------------------ wording
/** Everything the tracker says that depends on what the pages are and where they go. */
export interface Wording {
  noun: string;
  nouns: string;
  /** the table's item column header in the title/company layout */
  item: string;
  writing: string;
  changed: string;
  missing: string;
  unconfirmed: string;
  duplicate: string;
  duplicateSub: string;
  changedHint: string;
  missingHint: string;
  unconfirmedHint: string;
}
/** What the jobs demo says today (jobs_to_tracker on a form). */
export const JOBS_WORDING: Wording = {
  noun: "job posting",
  nouns: "job postings",
  item: "Posting",
  writing: "writing to the form…",
  changed: "form changed",
  missing: "title or company missing",
  unconfirmed: "form did not confirm",
  duplicate: "already in tracker",
  duplicateSub: "same posting, skipped",
  changedHint: "The tracker form changed since this integration was built, so nothing was written. Repair with AI, check the proposed mapping and approve it; the held pages then go in.",
  missingHint: "The reader found no title or company on this page, so it was held instead of writing a half-empty row. Open the page to check; if the layout changed, revert its reader and replay to learn it again.",
  unconfirmedHint: "The form was submitted but never showed its confirmation, so this row may not be in the Sheet. Check the Sheet, then replay held pages.",
};

export function wordingFor(name: string, d: DestinationInfo | null | undefined): Wording {
  const jobs = isJobs(name);
  if (jobs && (!d || d.kind === "form")) return JOBS_WORDING;
  const kind = d?.kind ?? null;
  const short = kind === "form" ? "the form" : kind === "airtable" ? "Airtable" : kind === "api" ? "the API" : "the destination";
  const The = kind === "form" ? "The form" : kind === "airtable" ? "The Airtable table" : kind === "api" ? "The API" : "The destination";
  const changedWord = kind === "form" ? "form" : kind === "airtable" ? "table" : kind === "api" ? "API" : "destination";
  const nouns = jobs ? { noun: "job posting", nouns: "job postings", item: "Posting" } : { noun: "page", nouns: "pages", item: "Page" };
  return {
    ...nouns,
    writing: `writing to ${short}…`,
    changed: `${changedWord} changed`,
    missing: jobs ? JOBS_WORDING.missing : "a field is missing",
    unconfirmed: `${kind === "form" ? "form" : kind === "airtable" ? "Airtable" : kind === "api" ? "API" : "destination"} did not confirm`,
    duplicate: jobs ? JOBS_WORDING.duplicate : "already sent",
    duplicateSub: jobs ? JOBS_WORDING.duplicateSub : "same page, skipped",
    changedHint: `${The} changed since this integration was built, so nothing was written. Repair with AI, check the proposed mapping and approve it; the held pages then go in.`,
    missingHint: jobs
      ? JOBS_WORDING.missingHint
      : "The reader couldn't find every field on this page, so it was held instead of writing a half-empty row. Open the page to check; if the layout changed, revert its reader and replay to learn it again.",
    unconfirmedHint: kind === "form"
      ? "The form was submitted but never showed its confirmation, so this row may not have been saved. Check the form's responses, then replay held pages."
      : kind === "airtable"
        ? "Airtable accepted the row but reading it back did not match what was sent. Check the table, then replay held pages."
        : `${The} accepted the write but the check afterwards did not find it. Check it, then replay held pages.`,
  };
}
