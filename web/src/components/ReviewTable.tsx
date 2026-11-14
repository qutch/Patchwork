import { useMemo, useState } from "react";
import type { ReviewEdit, ReviewRow, ReviewSource } from "../api";

/** A source in words: strings as sent, objects described (`{css: "h1"}` -> "reader: h1"). */
export function sourceLabel(s: ReviewSource): string {
  if (typeof s === "string") return s;
  if (typeof s.css === "string") return `reader: ${s.css}`;
  if (typeof s.jsonld === "string") return `reader: jsonld ${s.jsonld}`;
  if (typeof s.record === "string") return s.record === "canonical_url" ? "the page's canonical URL" : `the page's ${s.record.replace(/_/g, " ")}`;
  if (s.today) return "today's date";
  if ("const" in s) return `constant: ${String(s.const)}`;
  if (s.skip) return "skipped";
  return JSON.stringify(s);
}

type Pick = { kind: "unset" } | { kind: "keep" } | { kind: "alt"; i: number } | { kind: "const"; text: string } | { kind: "skip" };
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
  const unresolved = rows.filter((r) => picks[r.dest_field]?.kind === "unset" || (picks[r.dest_field]?.kind === "const" && !(picks[r.dest_field] as { text: string }).text.trim()));
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
                  <td className="font-semibold">{r.dest_field}</td>
                  <td className="min-w-[230px]">
                    <div className="flex flex-wrap items-center gap-2">
                      {r.ai_proposed && <span className="pill" style={{ color: "var(--accent)", background: "var(--accent-soft)" }} title="Proposed by AI, checked by code">AI</span>}
                      <span className="text-[13px]">{sourceLabel(r.source)}</span>
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
                      <option value={SKIP}>Skip</option>
                    </select>
                    {p.kind === "const" && (
                      <input className="input mt-1.5 text-[13px]" aria-label={`Value for ${r.dest_field}`} placeholder="Value to write every time" value={p.text}
                        onChange={(e) => set(r.dest_field, { kind: "const", text: e.target.value })} />
                    )}
                  </td>
                  {pageIds.map((id) => (
                    <td key={id} className="text-[13px] break-all">
                      {p.kind === "skip" ? <span className="text-muted">skipped</span>
                        : p.kind === "const" ? (p.text.trim() || <span className="text-muted">—</span>)
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
