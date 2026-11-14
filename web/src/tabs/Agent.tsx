import { useEffect, useMemo, useRef } from "react";
import { threadBelongs } from "../lib/integrations";
import { api, type ThreadStatus } from "../api";
import type { FeedItem, SubagentSpan } from "../lib/derive";
import { fmtDuration, fmtTime } from "../lib/derive";
import { useAgentThread, useApp } from "../state";
import { GateCard } from "../components/gates";
import { Card, Empty, PageHead, Spinner } from "../components/ui";

const AGENT_COLOR: Record<string, string> = {
  supervisor: "var(--ink)",
  interpreter: "var(--accent)",
  builder: "var(--ok)",
  tester: "var(--val)",
  repairer: "var(--pnm)",
  learner: "var(--accent)",
};
const agentColor = (a: string) => AGENT_COLOR[a] ?? "var(--muted)";

function AgentTag({ name }: { name: string }) {
  return (
    <span className="font-mono text-[12px] font-semibold uppercase tracking-wide" style={{ color: agentColor(name) }}>
      {name}
    </span>
  );
}

const STATE_STYLE: Record<string, { fg: string; bg: string }> = {
  running: { fg: "var(--accent)", bg: "var(--accent-soft)" },
  waiting: { fg: "var(--gate-ink)", bg: "var(--gate)" },
  done: { fg: "var(--ok)", bg: "var(--ok-soft)" },
  failed: { fg: "var(--bad)", bg: "var(--bad-soft)" },
};
function StatePill({ state }: { state: string | null }) {
  const s = STATE_STYLE[state ?? ""] ?? { fg: "var(--muted)", bg: "var(--sunken)" };
  return (
    <span className="pill" style={{ color: s.fg, background: s.bg }}>
      {state === "running" && <Spinner />}
      {state === "waiting" ? "waiting for you" : state ?? "…"}
    </span>
  );
}

function threadLabel(t: ThreadStatus) {
  return `${t.mode ?? t.thread_id.split("-")[0]} · ${t.thread_id} · ${t.state}`;
}

function spanSeconds(s: SubagentSpan): number | null {
  if (!s.endT) return null;
  return (new Date(s.endT).getTime() - new Date(s.startT).getTime()) / 1000;
}

function Timeline({ spans, running }: { spans: SubagentSpan[]; running: boolean }) {
  if (!spans.length) return <p className="m-0 text-muted">The supervisor hasn't delegated yet.</p>;
  return (
    <ol className="m-0 p-0 list-none relative">
      <span className="absolute left-[7px] top-2 bottom-2 w-[2px] bg-line" aria-hidden />
      {spans.map((s, i) => {
        const open = s.endT === null;
        return (
          <li key={i} className="relative pl-7 pb-4 last:pb-0">
            <span
              className="absolute left-0 top-1 w-4 h-4 rounded-full border-2"
              style={{ borderColor: agentColor(s.name), background: open ? "var(--surface)" : agentColor(s.name) }}
              aria-hidden
            />
            <div className="flex items-baseline justify-between gap-2">
              <AgentTag name={s.name} />
              <span className="font-mono text-xs text-muted num">
                {open ? (running ? <span className="inline-flex items-center gap-1.5"><Spinner /> working</span> : "open") : fmtDuration(spanSeconds(s))}
              </span>
            </div>
            <div className="text-[14px]">{s.task}</div>
            <div className="font-mono text-xs text-muted">
              {s.tools} tool call{s.tools === 1 ? "" : "s"}
              {s.failedTools > 0 && <span style={{ color: "var(--bad)" }}> · {s.failedTools} failed</span>}
            </div>
            {s.summary && <div className="text-[13px] mt-1 px-2 py-1 rounded bg-sunken">{s.summary}</div>}
          </li>
        );
      })}
    </ol>
  );
}

function argsText(args: Record<string, unknown>) {
  const s = JSON.stringify(args);
  return s === "{}" ? "" : s.length > 160 ? s.slice(0, 157) + "…" : s;
}

function FeedRow({ item, threadId }: { item: FeedItem; threadId: string }) {
  if (item.kind === "tool") {
    const r = item.result;
    return (
      <li className="grid grid-cols-[96px_minmax(0,1fr)] gap-3 px-4 py-2.5 border-b border-line">
        <AgentTag name={item.agent} />
        <div className="min-w-0">
          <div className="font-mono text-[13.5px] break-words">
            <span className="font-semibold">{item.tool}</span>
            <span className="text-muted">({argsText(item.args)})</span>
          </div>
          <div className="text-[13.5px] mt-0.5 flex gap-2 items-baseline">
            {r ? (
              <>
                <span className="font-mono font-bold" style={{ color: r.ok ? "var(--ok)" : "var(--bad)" }}>{r.ok ? "✓" : "✗"}</span>
                <span style={{ color: r.ok ? undefined : "var(--bad)" }} className="break-words min-w-0">{r.summary}</span>
              </>
            ) : (
              <span className="text-muted inline-flex items-center gap-2"><Spinner /> running…</span>
            )}
          </div>
        </div>
      </li>
    );
  }
  if (item.kind === "gate") {
    return (
      <li className="px-4 py-3 border-b border-line">
        {item.answer ? (
          <GateCard ev={item.ev} threadId={threadId} answer={item.answer} />
        ) : (
          <div className="border-l-4 border-gate pl-3 py-1 font-medium">
            Paused: {item.ev.type === "ask_user" ? item.ev.question : `deploy v${item.ev.version} needs approval`} <span className="text-muted">(see the card above)</span>
          </div>
        )}
      </li>
    );
  }
  const ev = item.ev;
  switch (ev.type) {
    case "message":
      return (
        <li className="grid grid-cols-[96px_minmax(0,1fr)] gap-3 px-4 py-2.5 border-b border-line">
          <AgentTag name={ev.agent} />
          <div className="text-[15px] italic">{ev.text}</div>
        </li>
      );
    case "artifact":
      return (
        <li className="grid grid-cols-[96px_minmax(0,1fr)] gap-3 px-4 py-2 border-b border-line">
          <span className="font-mono text-xs text-muted uppercase">artifact</span>
          <div className="font-mono text-[13px] break-all">
            <b>{ev.kind}</b>
            {ev.version != null && ` v${ev.version}`} · <span className="text-muted">{ev.path}</span>
          </div>
        </li>
      );
    case "deployed":
      return (
        <li className="px-4 py-3 border-b border-line font-semibold" style={{ color: "var(--ok)", background: "var(--ok-soft)" }}>
          ✓ Deployed {ev.name} v{ev.version} <span className="font-normal">(previous: {ev.previous_version ? `v${ev.previous_version}` : "none"})</span>
        </li>
      );
    case "done":
      return (
        <li className="px-4 py-2.5 border-b border-line font-mono text-[13px]">
          <b>done</b> <span className="text-muted">{JSON.stringify(ev.result)}</span>
        </li>
      );
    case "error":
      return (
        <li className="px-4 py-3 border-b border-line font-mono text-[13px]" style={{ color: "var(--bad)", background: "var(--bad-soft)" }}>
          error: {ev.message}
        </li>
      );
    case "evidence":
      return (
        <li className="grid grid-cols-[96px_minmax(0,1fr)] gap-3 px-4 py-2.5 border-b border-line">
          <span className="font-mono text-xs uppercase font-semibold" style={{ color: ev.ok ? "var(--ok)" : "var(--bad)" }}>evidence</span>
          <div className="grid gap-1 text-[13.5px]">
            <div>Code checked every value for <span className="font-mono">{ev.parser_id}</span> against <span className="font-mono">{ev.page_id}</span>: <b style={{ color: ev.ok ? "var(--ok)" : "var(--bad)" }}>{ev.ok ? "all found" : "not all found"}</b></div>
            {ev.fields.map((f) => (
              <div key={f.name} className="font-mono text-[12.5px]">
                <span style={{ color: f.ok ? "var(--ok)" : "var(--bad)" }}>{f.ok ? "✓" : "✗"}</span> {f.name} = {JSON.stringify(f.value)} <span className="text-muted">· {f.where ?? "not found"}</span>
              </div>
            ))}
          </div>
        </li>
      );
    case "learned":
      return (
        <li className="px-4 py-3 border-b border-line text-[14px]" style={ev.deployed ? { color: "var(--ok)", background: "var(--ok-soft)" } : undefined}>
          <b>{ev.deployed ? "✓ Learned" : "Learned, not deployed yet:"}</b> reader <span className="font-mono">{ev.parser_id}</span> for {ev.site}
          {ev.version != null && <> (v{ev.version})</>} · {ev.approval === "auto" ? "auto-approved" : ev.approval === "human" ? "approved by a person" : "waiting for a person"}
          <div className="text-[13px] font-normal" style={{ color: "var(--ink)" }}>{ev.reason}</div>
        </li>
      );
    case "subagent_start":
      return (
        <li className="px-4 py-1.5 border-b border-line bg-sunken text-[13px]">
          → <AgentTag name={ev.name} /> <span className="text-muted">started: {ev.task}</span>
        </li>
      );
    case "subagent_end":
      return (
        <li className="px-4 py-1.5 border-b border-line bg-sunken text-[13px]">
          ← <AgentTag name={ev.name} /> <span className="text-muted">finished: {ev.summary}</span>
        </li>
      );
    default:
      return null;
  }
}

export default function AgentTab({ integration }: { integration?: string } = {}) {
  const { threads: all, threadId: chosenId, setThreadId, navigate } = useApp();
  const threads = useMemo(() => (integration ? all.filter((t) => threadBelongs(t, integration)) : all), [all, integration]);
  // a thread of another integration falls back to this integration's newest; one just started (not listed yet) stays
  const threadId = !integration || (chosenId && (threads.some((t) => t.thread_id === chosenId) || !all.some((t) => t.thread_id === chosenId)))
    ? chosenId
    : threads.at(-1)?.thread_id ?? null;
  // follow the newest thread unless one is chosen
  useEffect(() => {
    if (!all.length) return;
    const newest = all[all.length - 1].thread_id;
    if (!chosenId) setThreadId(newest);
    else if (!all.some((t) => t.thread_id === chosenId)) {
      // just started (list not refreshed yet) or gone (e.g. after a reset)?
      api.thread(chosenId).catch(() => setThreadId(newest));
    }
  }, [all, chosenId, setThreadId]);
  const { view, conn, events } = useAgentThread(threadId);
  const feedRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = feedRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [view.feed.length]);

  const status = threads.find((t) => t.thread_id === threadId);
  const running = view.state === "running";
  const done = view.todos.filter((t) => t.status === "completed").length;
  const started = events[0]?.t;

  if (!threadId) {
    return (
      <>
        <PageHead eyebrow="Step 3 · watch the agents" title="Agent Activity" />
        <Empty action={<button className="btn btn-primary" onClick={() => navigate({ page: "new" })}>Start a new integration</button>}>
          No agent threads yet. Build an integration from a recording, or press “Repair with AI” on an incident.
        </Empty>
      </>
    );
  }

  return (
    <>
      <PageHead
        eyebrow={`Step 3 · ${view.mode === "repair" ? "repair" : "build"} thread`}
        title="Agent Activity"
        right={
          <>
            <StatePill state={view.state ?? status?.state ?? null} />
            <select className="input w-auto font-mono text-[13px]" value={threadId} onChange={(e) => setThreadId(e.target.value)} aria-label="Thread">
              {threads.map((t) => (
                <option key={t.thread_id} value={t.thread_id}>{threadLabel(t)}</option>
              ))}
              {!threads.some((t) => t.thread_id === threadId) && <option value={threadId}>{threadId}</option>}
            </select>
          </>
        }
      >
        The supervisor plans, delegates to subagents and calls tools that wrap the real compiler and runner. It stops at the yellow cards
        and waits for a person.
      </PageHead>

      {view.pending && <GateCard key={view.pending.interrupt_id} ev={view.pending} threadId={threadId} />}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
        <div className="grid gap-6 content-start">
          <Card eyebrow="Plan" title="Todo list" right={view.todos.length > 0 && <span className="font-mono text-sm text-muted num">{done}/{view.todos.length}</span>}>
            {view.todos.length === 0 ? (
              <p className="m-0 text-muted">Waiting for the plan…</p>
            ) : (
              <ol className="m-0 p-0 list-none grid gap-2">
                {view.todos.map((t, i) => (
                  <li key={i} className="flex gap-2.5 items-start text-[15px]">
                    <span
                      className="mt-0.5 w-[18px] h-[18px] flex-none rounded-[4px] border-2 grid place-items-center text-[11px] font-bold"
                      style={{
                        borderColor: t.status === "completed" ? "var(--ok)" : t.status === "in_progress" ? "var(--accent)" : "var(--line)",
                        background: t.status === "completed" ? "var(--ok)" : "transparent",
                        color: "var(--surface)",
                      }}
                      aria-label={t.status}
                    >
                      {t.status === "completed" ? "✓" : t.status === "in_progress" ? <span className="w-2 h-2 rounded-full bg-accent pulse" /> : ""}
                    </span>
                    <span style={{ color: t.status === "completed" ? "var(--muted)" : undefined, fontWeight: t.status === "in_progress" ? 600 : 400 }}>
                      {t.content}
                    </span>
                  </li>
                ))}
              </ol>
            )}
          </Card>
          <Card eyebrow="Delegation" title="Subagent timeline">
            <Timeline spans={view.subagents} running={running} />
          </Card>
        </div>

        <Card
          eyebrow="Stream"
          title="Tool calls and results"
          right={
            <span className="font-mono text-xs text-muted">
              {conn === "reconnecting" ? "reconnecting…" : `seq ${view.lastSeq}`}
              {started && ` · started ${fmtTime(started)}`}
            </span>
          }
          bodyClass=""
        >
          <div ref={feedRef} className="max-h-[640px] overflow-auto">
            {view.feed.length === 0 ? (
              <div className="p-4 text-muted">Connecting to the thread…</div>
            ) : (
              <ol className="m-0 p-0 list-none">
                {view.feed.map((item) => (
                  <FeedRow key={`${item.kind}-${item.seq}`} item={item} threadId={threadId} />
                ))}
                {running && (
                  <li className="px-4 py-3 text-muted inline-flex items-center gap-2">
                    <Spinner /> thinking…
                  </li>
                )}
              </ol>
            )}
          </div>
        </Card>
      </div>
    </>
  );
}
