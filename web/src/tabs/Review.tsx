import { useEffect, useMemo, useState } from "react";
import YAML from "yaml";
import { api, type Candidates, type Mapping } from "../api";
import { segments } from "../lib/derive";
import { useAgentThread, useApp } from "../state";
import { AskUserCard } from "../components/gates";
import { Card, Empty, GateMark, PageHead } from "../components/ui";

const FIELD_CLASSES = ["f-po", "f-sku", "f-qty"];
/** Stable highlight colour per destination field (reference→PO blue, product→SKU teal, quantity→amber). */
export function fieldClass(dest: string, index: number): string {
  const d = dest.toLowerCase();
  if (/ref|po/.test(d)) return "f-po";
  if (/sku|product|item|part/.test(d)) return "f-sku";
  if (/qty|quant|units|amount/.test(d)) return "f-qty";
  return FIELD_CLASSES[index % 3];
}

function sourceLabel(m: Mapping): string {
  return m.kind === "text" ? `${m.source_field}${m.label ? ` · “${m.label}”` : ""}` : m.json_path;
}

/** Destination field ← source, with the provenance snippet highlighted in the source text. */
export function MappingTable({ c }: { c: Candidates }) {
  const [hover, setHover] = useState<string | null>(null);
  const textField = c.source.text_field;
  const text = String(c.source.record[textField] ?? "");
  const spans = c.mappings
    .map((m, i) => (m.kind === "text" && m.source_field === textField ? { start: m.offset[0], end: m.offset[1], key: m.dest_field, i } : null))
    .filter((x): x is { start: number; end: number; key: string; i: number } => !!x);
  const cls = Object.fromEntries(c.mappings.map((m, i) => [m.dest_field, fieldClass(m.dest_field, i)]));
  const segs = segments(text, spans);
  // a little context: the line around each span
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
      <div className="min-w-0 overflow-x-auto">
        <table className="tbl">
          <thead>
            <tr>
              <th>Destination field</th>
              <th aria-label="from" />
              <th>Source</th>
              <th>Value</th>
              <th>Candidate rule</th>
            </tr>
          </thead>
          <tbody>
            {c.mappings.map((m, i) => (
              <tr
                key={m.dest_field}
                onMouseEnter={() => setHover(m.dest_field)}
                onMouseLeave={() => setHover(null)}
                style={{ background: hover === m.dest_field ? "var(--sunken)" : undefined }}
              >
                <td>
                  <span className={`pill ${fieldClass(m.dest_field, i)}`}>{m.dest_field}</span>
                </td>
                <td className="text-muted font-mono" aria-hidden>←</td>
                <td className="font-mono text-[13px]">{sourceLabel(m)}</td>
                <td className="font-mono text-[13px] font-semibold">{JSON.stringify(m.value)}</td>
                <td className="font-mono text-[12.5px] text-muted break-all">
                  {m.kind === "text" ? m.candidate_regex : m.json_path}
                  {m.kind === "text" && m.cast ? ` → ${m.cast}` : ""}
                </td>
              </tr>
            ))}
            {c.unexplained.map((u) => (
              <tr key={u.dest_field}>
                <td><span className="pill" style={{ background: "var(--bad-soft)", color: "var(--bad)" }}>{u.dest_field}</span></td>
                <td className="text-muted font-mono">?</td>
                <td colSpan={3} className="text-muted text-sm">
                  Value <code className="font-mono">{JSON.stringify(u.value)}</code> wasn't found anywhere in the source. The agent will ask about it.
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="font-mono text-xs text-muted mt-3">
          {c.sink.method} {c.sink.base_url}{c.sink.path} ← {c.source.list.method} {c.source.base_url}{c.source.list.path}
        </div>
      </div>
      <div className="min-w-0">
        <div className="label">
          Source record {String(c.source.record[c.source.record_id] ?? "")} · field “{textField}”
        </div>
        {"subject" in c.source.record && <div className="font-semibold mb-1">{String(c.source.record.subject)}</div>}
        <pre className="m-0 whitespace-pre-wrap font-mono text-[14px] leading-relaxed bg-sunken rounded p-3 break-words">
          {segs.map((s, i) =>
            s.key ? (
              <mark
                key={i}
                className={cls[s.key]}
                style={{ outline: hover === s.key ? "2px solid var(--ink)" : undefined, outlineOffset: 1 }}
                title={`→ ${s.key}`}
              >
                {s.text}
              </mark>
            ) : (
              <span key={i}>{s.text}</span>
            ),
          )}
        </pre>
      </div>
    </div>
  );
}

interface Assumption {
  id: string;
  text: string;
  status: string;
}

export default function ReviewTab() {
  const { recordingId, integrations, integrationName, threads, threadId, navigate } = useApp();
  const [cands, setCands] = useState<Candidates | null>(null);
  const [assumptions, setAssumptions] = useState<Assumption[] | null>(null);
  const [specVersion, setSpecVersion] = useState<number | null>(null);
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({});
  const latest = integrations.find((i) => i.name === integrationName)?.latest_version ?? null;
  const { view } = useAgentThread(threadId);

  useEffect(() => {
    setCands(null);
    if (!recordingId) return;
    api.recording(recordingId).then((r) => setCands(r.candidates), () => setCands(null));
  }, [recordingId]);

  useEffect(() => {
    if (!latest) return;
    api.version(integrationName, latest).then(
      (v) => {
        try {
          const spec = YAML.parse(v.spec_yaml) as { assumptions?: Assumption[] };
          setAssumptions(spec.assumptions ?? []);
          setSpecVersion(v.version);
          setConfirmed((c) => ({ ...Object.fromEntries((spec.assumptions ?? []).map((a) => [a.id, a.status === "confirmed"])), ...c }));
        } catch {
          setAssumptions([]);
        }
      },
      () => setAssumptions(null),
    );
  }, [latest, integrationName]);

  const pendingAsks = threads.filter((t) => t.pending?.type === "ask_user");
  const answered = useMemo(
    () => view.feed.filter((f) => f.kind === "gate" && f.ev.type === "ask_user" && f.answer),
    [view.feed],
  );

  return (
    <>
      <PageHead eyebrow="Step 2 · check the agent's reading" title="Review">
        What the matcher and the interpreter think the recording means. Every mapping points at the exact text it came from.
      </PageHead>

      <Card eyebrow="Mappings" title="Destination field ← source">
        {cands ? (
          <MappingTable c={cands} />
        ) : (
          <Empty action={<button className="btn" onClick={() => navigate({ page: "new" })}>Start a new integration</button>}>
            No candidates yet. Record a transfer and press “Find mappings”.
          </Empty>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card
          eyebrow="Open questions"
          title="The agent needs a decision"
          right={pendingAsks.length > 0 && <GateMark>{pendingAsks.length} waiting</GateMark>}
          bodyClass="card-b grid gap-3"
        >
          {pendingAsks.map((t) =>
            t.pending?.type === "ask_user" ? <AskUserCard key={t.pending.interrupt_id} ev={t.pending} threadId={t.thread_id} /> : null,
          )}
          {answered.map((f) =>
            f.kind === "gate" && f.ev.type === "ask_user" ? <AskUserCard key={f.ev.interrupt_id} ev={f.ev} threadId={threadId!} answer={f.answer} /> : null,
          )}
          {!pendingAsks.length && !answered.length && <p className="m-0 text-muted">No questions right now. They appear here and on Agent Activity while an agent is paused on one.</p>}
        </Card>

        <Card eyebrow="Assumptions" title={specVersion ? `In the spec (v${specVersion})` : "In the spec"} bodyClass="card-b grid gap-2">
          {!assumptions && <p className="m-0 text-muted">The interpreter writes its assumptions into the spec. They show up here once a version is compiled.</p>}
          {assumptions?.length === 0 && <p className="m-0 text-muted">This spec lists no assumptions.</p>}
          {assumptions?.map((a) => {
            const on = !!confirmed[a.id];
            return (
              <label key={a.id} className="flex items-center gap-3 border border-line rounded-md px-3 py-2.5 cursor-pointer">
                <input
                  type="checkbox"
                  role="switch"
                  className="w-5 h-5 accent-[var(--accent)]"
                  checked={on}
                  onChange={() => setConfirmed((c) => ({ ...c, [a.id]: !on }))}
                />
                <span className="flex-1">
                  <span className="font-mono text-xs text-muted mr-2">{a.id}</span>
                  {a.text}
                </span>
                <span className="pill" style={{ color: on ? "var(--ok)" : "var(--val)", background: on ? "var(--ok-soft)" : "var(--val-soft)" }}>
                  {on ? "confirmed" : "unconfirmed"}
                </span>
              </label>
            );
          })}
          {assumptions && assumptions.length > 0 && (
            <p className="m-0 text-xs text-muted">Toggles are local to this page: the API has no endpoint to write assumptions back to the spec yet.</p>
          )}
        </Card>
      </div>
    </>
  );
}
