import { useMemo, useState } from "react";
import type { ReviewEdit, ReviewRow, ReviewSource } from "../api";

/** A source in words: strings as sent, objects described (`{css: "h1"}` -> "reader: h1"). */
export function sourceLabel(s: ReviewSource): string {
  if (typeof s === "string") return s;
  if (typeof s.label === "string" && s.label) return s.label;
  if (typeof s.css === "string") return `reader: ${s.css}`;
  if (typeof s.jsonld === "string") return `reader: jsonld ${s.jsonld}`;
  if (typeof s.record === "string") return s.record === "canonical_url" ? "the page's canonical URL" : `the page's ${s.record.replace(/_/g, " ")}`;
  if (s.today) return "today's date";
  if ("const" in s) return `constant: ${String(s.const)}`;
  if (s.skip) return "skipped";
  return JSON.stringify(s);
}

/** A picked alternative shows its own preview when the backend sent one; otherwise it says it is not computed yet. */
function altValue(a: ReviewSource, id: string) {
  const v = typeof a === "object" && a.values && typeof a.values === "object" ? (a.values as Record<string, string | null>)[id] : undefined;
  return v ?? <span className="text-muted" title="Checked by code when you approve">recomputed on approve</span>;
}

/**
 * What is wrong with a typed value for this row, or null if it fits: the same checks the backend runs before it
 * accepts the edit (contracts V15 number/date/url rules, a choice field's allowed values).
 */
export function constProblem(row: { type?: string; values_allowed?: string[] }, text: string): string | null {
  const s = text.trim();
  if (!s) return null;
  const kind = row.type ?? "text";
  if (row.values_allowed?.length) return row.values_allowed.includes(s) ? null : `Choose one of: ${row.values_allowed.join(", ")}`;
  if (kind === "number" || kind === "integer") {
    const n = Number(s.replace(/,/g, ""));
    if (!Number.isFinite(n)) return "Expected a number";
    if (kind === "integer" && !Number.isInteger(n)) return "Expected a whole number";
  }
  if (kind === "date") {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    const d = m && new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    if (!m || !d || d.getUTCMonth() !== +m[2] - 1 || d.getUTCDate() !== +m[3]) return "Expected a date YYYY-MM-DD";
  }
  if (kind === "url" && !/^https?:\/\/[^\s/?#]+[^\s]*$/i.test(s)) return "Expected a link starting with http:// or https://";
  return null;
}

type Pick ={ kind: "unset" } | { kind: "keep" } | { kind: "alt"; i: number } | { kind: "const"; text: string } | { kind: "skip" };
const KEEP = "keep", CONST = "const", SKIP = "skip", UNSET = "unset";
const valueOf = (p: Pick) => (p.kind === "alt" ? `alt:${p.i}` : p.kind);

/** The edits a set of picks makes: nothing for a row left as proposed. */
export function editsFor(rows: ReviewRow[], picks: Record<string, Pick>): ReviewEdit[] {
  const out: ReviewEdit[] = [];
  for (const r of rows) {
    const p = picks[r.dest_field];
    if (!p) continue;
    if (p.kind === "alt") out.push({ dest_field: r.dest_field, choice: r.alternatives[p.i] });
    else if (p.kind === "const" && p.text.trim()) out.push({ dest_field: r.dest_field, choice: { const: p.text.trim() } });
    else if (p.kind === "skip") out.push({ dest_field: r.dest_field, choice: { skip: true } });
  }
  return out;
}

/**
 * The review of a teach thread's proposal: one row per destination field, one value column per example page.
 * Rows the AI proposed say so; rows that need a person start unchosen and block approval until chosen.
 * Approve sends the decision with exactly the edits the person made.
 */
export default function ReviewTable({ rows, pages, busy = false, onDecide }: {
  rows: ReviewRow[];
  /** example page id -> a short label (its site); an id without a label shows the id */
  pages: Record<string, string>;
  busy?: boolean;
  onDecide: (decision: "approve" | "reject", edits: ReviewEdit[]) => void;
}) {
  const pageIds = useMemo(() => [...new Set(rows.flatMap((r) => Object.keys(r.values)))], [rows]);
  const [picks, setPicks] = useState<Record<string, Pick>>(() =>
    Object.fromEntries(rows.map((r) => [r.dest_field, r.status === "needs_you" ? { kind: "unset" } : { kind: "keep" }])) as Record<string, Pick>);
  const set = (f: string, p: Pick) => setPicks((x) => ({ ...x, [f]: p }));
  const typedText = (f: string) => { const p = picks[f]; return p?.kind === "const" ? p.text : null; };
  const unresolved = rows.filter((r) => {
    const t = typedText(r.dest_field);
    return picks[r.dest_field]?.kind === "unset" || (t !== null && (!t.trim() || constProblem(r, t) !== null));
  });
  const edits = editsFor(rows, picks);
  return (
    <div className="grid gap-3">
      <div className="overflow-x-auto">
        <table className="tbl">
          <thead>
            <tr>
              <th>Field</th>
              <th>Comes from</th>
              {pageIds.map((id) => <th key={id} title={id}>{pages[id] ?? id}</th>)}
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const p = picks[r.dest_field] ?? { kind: KEEP };
              const needs = r.status === "needs_you" && (p.kind === "unset");
              return (
                <tr key={r.dest_field}>
                  <td className="font-semibold">
                    {r.dest_field}
                    {r.required && <span className="block text-muted text-[12px] font-normal">required</span>}
                  </td>
                  <td className="min-w-[230px]">
                    <div className="flex flex-wrap items-center gap-2">
                      {r.ai_proposed && <span className="pill" style={{ color: "var(--accent)", background: "var(--accent-soft)" }} title="Proposed by AI, checked by code">AI</span>}
                      <span className="text-[13px]">{r.label || sourceLabel(r.source)}</span>
                    </div>
                    <select
                      className="input mt-1.5 text-[13px]"
                      aria-label={`Source for ${r.dest_field}`}
                      value={valueOf(p)}
                      onChange={(e) => {
                        const v = e.target.value;
                        if (v === KEEP) set(r.dest_field, { kind: "keep" });
                        else if (v === SKIP) set(r.dest_field, { kind: "skip" });
                        else if (v === CONST) set(r.dest_field, { kind: "const", text: "" });
                        else if (v.startsWith("alt:")) set(r.dest_field, { kind: "alt", i: Number(v.slice(4)) });
                        else set(r.dest_field, { kind: "unset" });
                      }}
                    >
                      {r.status === "needs_you" && <option value={UNSET}>Choose…</option>}
                      <option value={KEEP}>{r.status === "needs_you" ? "Use as proposed" : "As proposed"}</option>
                      {r.alternatives.map((a, i) => <option key={i} value={`alt:${i}`}>{sourceLabel(a)}</option>)}
                      <option value={CONST}>Type a value…</option>
                      {!r.required && <option value={SKIP}>Skip</option>}
                    </select>
                    {p.kind === "const" && (r.values_allowed?.length ? (
                      <select className="input mt-1.5 text-[13px]" aria-label={`Value for ${r.dest_field}`} value={p.text}
                        onChange={(e) => set(r.dest_field, { kind: "const", text: e.target.value })}>
                        <option value="">Choose…</option>
                        {r.values_allowed.map((v) => <option key={v} value={v}>{v}</option>)}
                      </select>
                    ) : (
                      <input className="input mt-1.5 text-[13px]" aria-label={`Value for ${r.dest_field}`} value={p.text}
                        type={r.type === "date" ? "date" : r.type === "url" ? "url" : "text"}
                        inputMode={r.type === "number" || r.type === "integer" ? "decimal" : undefined}
                        placeholder={r.type === "date" ? "YYYY-MM-DD" : r.type === "url" ? "https://…" : "Value to write every time"}
                        aria-invalid={constProblem(r, p.text) !== null}
                        onChange={(e) => set(r.dest_field, { kind: "const", text: e.target.value })} />
                    ))}
                    {p.kind === "const" && constProblem(r, p.text) && (
                      <span className="block mt-1 text-[12px]" style={{ color: "var(--val)" }}>{constProblem(r, p.text)}</span>
                    )}
                  </td>
                  {pageIds.map((id) => (
                    <td key={id} className="text-[13px] break-all">
                      {p.kind === "skip" ? <span className="text-muted">skipped</span>
                        : p.kind === "const" ? (p.text.trim() || <span className="text-muted">—</span>)
                        : p.kind === "alt" ? altValue(r.alternatives[p.i], id)
                        : r.values[id] ?? <span className="text-muted">—</span>}
                    </td>
                  ))}
                  <td>
                    {needs ? (
                      <span className="pill" style={{ color: "var(--val)", background: "var(--val-soft)" }}>needs you</span>
                    ) : p.kind === "skip" ? (
                      <span className="pill" style={{ color: "var(--muted)", background: "var(--sunken)" }}>skipped</span>
                    ) : (
                      <span className="pill" style={{ color: "var(--ok)", background: "var(--ok-soft)" }}>✓ checked</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap gap-2 items-center">
        <button className="btn btn-lg btn-gate" disabled={busy || unresolved.length > 0} onClick={() => onDecide("approve", edits)}>
          ✓ Approve &amp; make it live
        </button>
        <button className="btn btn-lg btn-danger" disabled={busy} onClick={() => onDecide("reject", [])}>Reject</button>
        {unresolved.length > 0 && <span className="text-muted text-[13px]">{unresolved.length} row{unresolved.length === 1 ? "" : "s"} still need{unresolved.length === 1 ? "s" : ""} you: {unresolved.map((r) => r.dest_field).join(", ")}</span>}
      </div>
    </div>
  );
}
