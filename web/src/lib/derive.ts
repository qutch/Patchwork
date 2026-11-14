// Pure helpers that turn API payloads into what the views draw. Unit-tested.
import type { AgentEvent, CaptureEvent, InterruptEvent, ThreadState, Todo } from "../api";

// ------------------------------------------------------------------ agent threads
export interface SubagentSpan {
  name: string;
  task: string;
  startSeq: number;
  startT: string;
  endT: string | null;
  summary: string | null;
  tools: number;
  failedTools: number;
}
export interface ToolStep {
  kind: "tool";
  seq: number;
  agent: string;
  tool: string;
  args: Record<string, unknown>;
  result: { ok: boolean; summary: string } | null;
}
export type FeedItem =
  | ToolStep
  | { kind: "event"; seq: number; ev: AgentEvent }
  | { kind: "gate"; seq: number; ev: InterruptEvent; answer: Record<string, unknown> | null };

export interface ThreadView {
  mode: "build" | "repair" | null;
  state: ThreadState | null;
  todos: Todo[];
  subagents: SubagentSpan[];
  feed: FeedItem[];
  pending: InterruptEvent | null;
  lastSeq: number;
}

/** Fold an ordered list of agent events (any prefix of a thread) into a view model. */
export function deriveThread(events: AgentEvent[]): ThreadView {
  const v: ThreadView = { mode: null, state: null, todos: [], subagents: [], feed: [], pending: null, lastSeq: 0 };
  const openTool = new Map<string, ToolStep>(); // agent::tool -> last step without result
  const gates = new Map<string, Extract<FeedItem, { kind: "gate" }>>();
  for (const ev of events) {
    v.lastSeq = Math.max(v.lastSeq, ev.seq);
    switch (ev.type) {
      case "status":
        v.state = ev.state;
        v.mode = ev.mode;
        break;
      case "todos":
        v.todos = ev.todos;
        break;
      case "subagent_start":
        v.subagents.push({ name: ev.name, task: ev.task, startSeq: ev.seq, startT: ev.t, endT: null, summary: null, tools: 0, failedTools: 0 });
        v.feed.push({ kind: "event", seq: ev.seq, ev });
        break;
      case "subagent_end": {
        const s = [...v.subagents].reverse().find((x) => x.name === ev.name && x.endT === null);
        if (s) {
          s.endT = ev.t;
          s.summary = ev.summary;
        }
        v.feed.push({ kind: "event", seq: ev.seq, ev });
        break;
      }
      case "tool_call": {
        const step: ToolStep = { kind: "tool", seq: ev.seq, agent: ev.agent, tool: ev.tool, args: ev.args ?? {}, result: null };
        openTool.set(`${ev.agent}::${ev.tool}`, step);
        v.feed.push(step);
        const s = [...v.subagents].reverse().find((x) => x.name === ev.agent && x.endT === null);
        if (s) s.tools++;
        break;
      }
      case "tool_result": {
        const key = `${ev.agent}::${ev.tool}`;
        const step = openTool.get(key);
        if (step) {
          step.result = { ok: ev.ok, summary: ev.summary };
          openTool.delete(key);
        } else {
          v.feed.push({ kind: "tool", seq: ev.seq, agent: ev.agent, tool: ev.tool, args: {}, result: { ok: ev.ok, summary: ev.summary } });
        }
        if (!ev.ok) {
          const s = [...v.subagents].reverse().find((x) => x.name === ev.agent);
          if (s) s.failedTools++;
        }
        break;
      }
      case "ask_user":
      case "request_deploy": {
        const g = { kind: "gate" as const, seq: ev.seq, ev, answer: null };
        gates.set(ev.interrupt_id, g);
        v.feed.push(g);
        v.pending = ev;
        break;
      }
      case "resumed": {
        const g = gates.get(ev.interrupt_id);
        if (g) g.answer = ev.payload;
        if (v.pending?.interrupt_id === ev.interrupt_id) v.pending = null;
        break;
      }
      default:
        v.feed.push({ kind: "event", seq: ev.seq, ev });
    }
  }
  if (v.state === "done" || v.state === "failed") v.pending = null;
  return v;
}

/** Merge newly streamed events into a list, keeping seq order and dropping repeats. */
export function mergeEvents<T extends { seq: number }>(list: T[], incoming: T[]): T[] {
  if (!incoming.length) return list;
  const seen = new Set(list.map((e) => e.seq));
  const add = incoming.filter((e) => !seen.has(e.seq));
  if (!add.length) return list;
  return [...list, ...add].sort((a, b) => a.seq - b.seq);
}

// ------------------------------------------------------------------ recordings
export function describeEvent(e: CaptureEvent): { kind: string; text: string; detail?: string } {
  const el = e.el;
  const field = el ? el.label || el.name || el.testid || el.tag || "element" : "";
  switch (e.type) {
    case "framenavigated":
      return { kind: "navigate", text: e.title ? `${e.title}` : e.url ?? "", detail: e.url };
    case "net": {
      const n = Array.isArray(e.resp) ? `${e.resp.length} records` : undefined;
      return { kind: (e.method ?? "GET").toUpperCase(), text: `${e.url} → ${e.status ?? "…"}`, detail: e.req ? JSON.stringify(e.req) : n };
    }
    case "click":
      return { kind: "click", text: el?.name ? `“${el.name}”` : field, detail: el?.css ?? undefined };
    case "copy":
      return { kind: "copy", text: `“${e.text ?? ""}”`, detail: el?.nearby ? `near ${el.nearby}` : undefined };
    case "paste":
    case "input":
    case "change":
      return { kind: e.type, text: `${field} = “${e.value ?? ""}”`, detail: el?.css ?? undefined };
    case "submit":
      return { kind: "submit", text: el?.nearby ? `form (${el.nearby})` : "form", detail: e.url };
    default:
      return { kind: e.type, text: e.url ?? "" };
  }
}

/** Host part of an absolute URL ("http://localhost:8001/x" -> "localhost:8001"). */
export function hostOf(url?: string): string {
  if (!url) return "";
  try {
    return new URL(url).host;
  } catch {
    return "";
  }
}

// ------------------------------------------------------------------ diffs
export type DiffLine = { kind: "add" | "del" | "ctx" | "hunk" | "file" | "meta"; text: string };

/** Python's difflib leaves no newline after a file's last line when the file has none, so
 *  diffs of several files joined together can glue the next "--- a" header onto it. */
export function normalizeDiff(text: string): string {
  return text.replace(/([^\n])(--- [^\n]+\n\+\+\+ )/g, "$1\n$2");
}

/** Classify each line of a unified diff. Text that isn't a diff is shown as plain context. */
export function parseUnifiedDiff(text: string): DiffLine[] {
  if (!text) return [];
  const lines = normalizeDiff(text).replace(/\n$/, "").split("\n");
  const isDiff = lines.some((l) => l.startsWith("@@") || l.startsWith("--- ") || l.startsWith("+++ "));
  if (!isDiff) return lines.map((t) => ({ kind: "ctx", text: t }));
  return lines.map((t): DiffLine => {
    if (t.startsWith("+++ ") || t.startsWith("--- ")) return { kind: "file", text: t };
    if (t.startsWith("@@")) return { kind: "hunk", text: t };
    if (t.startsWith("+")) return { kind: "add", text: t };
    if (t.startsWith("-")) return { kind: "del", text: t };
    if (t.startsWith("\\")) return { kind: "meta", text: t };
    return { kind: "ctx", text: t };
  });
}

export function diffStats(text: string): { add: number; del: number } {
  let add = 0, del = 0;
  for (const l of parseUnifiedDiff(text)) {
    if (l.kind === "add") add++;
    else if (l.kind === "del") del++;
  }
  return { add, del };
}

/** Split one string holding several concatenated file diffs into {file: diff}. */
export function splitFileDiffs(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  if (!text) return out;
  const parts = normalizeDiff(text).split(/(?=^--- )/m);
  for (const p of parts) {
    const m = p.match(/^\+\+\+ (.+)$/m);
    if (m && p.trim()) out[m[1].trim()] = p;
  }
  return out;
}

// ------------------------------------------------------------------ highlighting
export interface Span {
  start: number;
  end: number;
  key: string;
}
export type Segment = { text: string; key: string | null };

/** Cut `text` into plain and highlighted segments. Overlapping spans: the earlier one wins. */
export function segments(text: string, spans: Span[]): Segment[] {
  const sorted = [...spans].filter((s) => s.end > s.start && s.start >= 0 && s.end <= text.length).sort((a, b) => a.start - b.start);
  const out: Segment[] = [];
  let pos = 0;
  for (const s of sorted) {
    if (s.start < pos) continue;
    if (s.start > pos) out.push({ text: text.slice(pos, s.start), key: null });
    out.push({ text: text.slice(s.start, s.end), key: s.key });
    pos = s.end;
  }
  if (pos < text.length) out.push({ text: text.slice(pos), key: null });
  return out;
}

// ------------------------------------------------------------------ formatting
export function fmtDuration(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

export function fmtTime(t: string | number | null | undefined): string {
  if (t == null || t === "") return "—";
  const d = new Date(typeof t === "number" ? t : t);
  if (Number.isNaN(d.getTime())) return String(t);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
