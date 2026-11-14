import { useState } from "react";
import { api, type InterruptEvent, type ReviewEdit } from "../api";
import { splitFileDiffs } from "../lib/derive";
import { useApp } from "../state";
import { Diff, DiffStat, TestsPill, Tabs } from "./ui";
import ReviewTable from "./ReviewTable";

type Ask = Extract<InterruptEvent, { type: "ask_user" }>;
type Deploy = Extract<InterruptEvent, { type: "request_deploy" }>;

/** Human gate: the agent is paused on ask_user until someone answers. */
export function AskUserCard({ ev, threadId, answer }: { ev: Ask; threadId: string; answer?: Record<string, unknown> | null }) {
  const { act, refresh } = useApp();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const send = async (a: string) => {
    if (!a.trim()) return;
    setBusy(true);
    await act(() => api.resume(threadId, { interrupt_id: ev.interrupt_id, answer: a.trim() }));
    setBusy(false);
    refresh();
  };
  if (answer) {
    return (
      <div className="border-l-4 border-gate bg-surface rounded-r-md px-4 py-3 grid gap-1">
        <div className="eyebrow">Question answered · {ev.interrupt_id}</div>
        <div className="font-medium">{ev.question}</div>
        <div className="text-sm">
          <span className="text-muted">Answer: </span>
          <b>{String(answer.answer ?? "")}</b>
        </div>
      </div>
    );
  }
  return (
    <section className="gate-card" aria-label="Agent question awaiting your answer">
      <div className="gate-band">
        <b>◆ Human gate · the agent is asking</b>
        <span className="font-mono text-xs">{ev.interrupt_id} · agent paused</span>
      </div>
      <div className="p-4 grid gap-3">
        <p className="m-0 text-[18px] font-semibold leading-snug">{ev.question}</p>
        <div className="flex flex-wrap gap-2">
          {ev.options.map((o) => (
            <button key={o} className="btn btn-lg btn-gate" disabled={busy} onClick={() => send(o)}>
              {o}
            </button>
          ))}
        </div>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void send(text);
          }}
        >
          <input className="input" placeholder="…or type your own answer" value={text} onChange={(e) => setText(e.target.value)} aria-label="Free-text answer" />
          <button className="btn" disabled={busy || !text.trim()} type="submit">
            Send
          </button>
        </form>
      </div>
    </section>
  );
}

/** Human gate: approve or reject a tested version before it goes live. */
export function DeployCard({ ev, threadId, answer }: { ev: Deploy; threadId: string; answer?: Record<string, unknown> | null }) {
  const { act, refresh } = useApp();
  const [view, setView] = useState<"spec" | "code" | "patch" | "tests">(ev.spec_diff ? "spec" : "code");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const decide = async (decision: "approve" | "reject", edits?: ReviewEdit[]) => {
    setBusy(true);
    await act(() => api.resume(threadId, { interrupt_id: ev.interrupt_id, decision, comment, ...(edits?.length ? { edits } : {}) }));
    setBusy(false);
    refresh();
  };
  const isDiff = ev.spec_diff.startsWith("---");
  const codeFiles = splitFileDiffs(ev.code_diff);
  if (answer) {
    const ok = answer.decision === "approve";
    return (
      <div className="border-l-4 border-gate bg-surface rounded-r-md px-4 py-3 grid gap-1">
        <div className="eyebrow">Deploy decision · {ev.interrupt_id}</div>
        <div className="font-medium">
          {ev.name} v{ev.version}: <span style={{ color: ok ? "var(--ok)" : "var(--bad)" }}>{ok ? "approved" : "rejected"}</span>
          {answer.comment ? <span className="text-muted"> · “{String(answer.comment)}”</span> : null}
        </div>
      </div>
    );
  }
  return (
    <section className="gate-card" aria-label="Deploy awaiting approval">
      <div className="gate-band">
        <b>◆ Human gate · approve deploy</b>
        <span className="font-mono text-xs">{ev.interrupt_id} · nothing goes live without you</span>
      </div>
      <div className="p-4 grid gap-3">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <div>
            <div className="display text-[22px] font-extrabold leading-tight">
              Deploy <span className="font-mono">{ev.name}</span> v{ev.version}?
            </div>
            <p className="m-0 text-muted">{ev.summary}</p>
          </div>
          <TestsPill passed={ev.tests.passed} summary={ev.tests.summary} />
        </div>
        {ev.review && ev.review.length > 0 && <ReviewTable rows={ev.review} pages={{}} busy={busy} onDecide={decide} />}
        <div className="border border-line rounded-md overflow-hidden">
          <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-line bg-surface flex-wrap">
            <Tabs
              value={view}
              onChange={setView}
              items={[
                { id: "spec", label: isDiff ? "Spec diff" : "Spec (new)" },
                { id: "code", label: <>Code diff {ev.code_diff && <DiffStat text={ev.code_diff} />}</> },
                ...(ev.spec_patch ? [{ id: "patch" as const, label: `JSON Patch (${ev.spec_patch.length} ops)` }] : []),
                { id: "tests", label: "Test results" },
              ]}
            />
          </div>
          {view === "spec" && <Diff text={ev.spec_diff} maxHeight={380} />}
          {view === "code" &&
            (Object.keys(codeFiles).length ? (
              <Diff text={ev.code_diff} maxHeight={380} />
            ) : (
              <div className="p-3 text-sm text-muted">First version: no previous code to diff. The generated files are on the Integration tab.</div>
            ))}
          {view === "patch" && ev.spec_patch && (
            <pre className="code" style={{ maxHeight: 380 }}>
              {ev.spec_patch.map((op, i) => (
                <div key={i}>
                  <span style={{ color: op.op === "remove" ? "var(--bad)" : op.op === "add" ? "var(--ok)" : "var(--accent)" }}>{op.op.padEnd(8)}</span>
                  {op.from ? `${op.from} → ` : ""}
                  {op.path}
                  {op.value !== undefined ? `  ${JSON.stringify(op.value)}` : ""}
                </div>
              ))}
            </pre>
          )}
          {view === "tests" && (
            <pre className="code" style={{ maxHeight: 380, whiteSpace: "pre-wrap" }}>
              {ev.tests.summary}
            </pre>
          )}
        </div>
        {!(ev.review && ev.review.length > 0) && <div className="flex flex-wrap gap-2 items-center">
          <input className="input flex-1 min-w-[220px]" placeholder="Comment (optional, sent with your decision)" value={comment} onChange={(e) => setComment(e.target.value)} aria-label="Comment" />
          <button className="btn btn-lg btn-gate" disabled={busy || !ev.tests.passed} onClick={() => decide("approve")}>
            ✓ Approve &amp; deploy v{ev.version}
          </button>
          <button className="btn btn-lg btn-danger" disabled={busy} onClick={() => decide("reject")}>
            Reject
          </button>
        </div>}
      </div>
    </section>
  );
}

export function GateCard({ ev, threadId, answer }: { ev: InterruptEvent; threadId: string; answer?: Record<string, unknown> | null }) {
  return ev.type === "ask_user" ? <AskUserCard ev={ev} threadId={threadId} answer={answer} /> : <DeployCard ev={ev} threadId={threadId} answer={answer} />;
}
