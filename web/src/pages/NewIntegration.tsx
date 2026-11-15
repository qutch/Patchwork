import { useEffect, useMemo, useState } from "react";
import { api, ApiError, type ApiOperation, type DestinationCheck, type DestinationInput, type Draft, type PageRecord, type ReviewEdit } from "../api";
import { errText, useAgentThread, useApp, usePoll } from "../state";
import { AiToggle } from "../components/AiToggle";
import ReviewTable from "../components/ReviewTable";
import { AskUserCard } from "../components/gates";
import { Card, Empty, PageHead, Spinner, Tabs } from "../components/ui";

const LAST_BASE = "pw-last-airtable-base";
const rememberedBase = (): string => {
  try {
    return localStorage.getItem(LAST_BASE) ?? "";
  } catch {
    return "";
  }
};
const rememberBase = (v: string) => {
  try {
    localStorage.setItem(LAST_BASE, v);
  } catch {
    /* private mode: the base just isn't remembered */
  }
};

const STEPS = ["What", "Where", "Examples", "Review"] as const;
const APP_ID = /app[A-Za-z0-9]{14,}/;

function Steps({ step }: { step: number }) {
  return (
    <ol className="m-0 p-0 list-none flex flex-wrap gap-2" aria-label="Steps">
      {STEPS.map((s, i) => (
        <li
          key={s}
          aria-current={step === i + 1 ? "step" : undefined}
          className="px-3 py-1.5 rounded-md text-[14px] font-medium"
          style={{ color: step === i + 1 ? "var(--accent)" : step > i + 1 ? "var(--ink)" : "var(--muted)", background: step === i + 1 ? "var(--accent-soft)" : "var(--sunken)" }}
        >
          <span className="font-mono text-xs mr-1.5">{i + 1}</span>{s}
        </li>
      ))}
    </ol>
  );
}

function Problem({ text }: { text: string | null }) {
  if (!text) return null;
  return <div role="alert" className="rounded-md border-l-4 px-3 py-2 text-[14px]" style={{ borderColor: "var(--bad)", background: "var(--bad-soft)" }}>{text}</div>;
}

// ------------------------------------------------------------------------------- 1. what
function StepWhat({ onCreated }: { onCreated: (d: Draft) => void }) {
  const [intent, setIntent] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  // ?from_page=<page id>: suggest the intent from that page's title (the demo's "I was just looking at this")
  const fromPage = typeof location !== "undefined" ? new URLSearchParams(location.search).get("from_page") : null;
  useEffect(() => {
    if (!fromPage) return;
    let live = true;
    api.page(fromPage).then(
      (r) => live && !touched && r.page.title && setIntent((cur) => cur || `Save pages like “${r.page.title.split(/ [-|:] /)[0].trim()}”`),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [fromPage, touched]);
  const create = async () => {
    setBusy(true);
    setProblem(null);
    try {
      onCreated(await api.newIntegration(intent.trim()));
    } catch (e) {
      setProblem(errText(e));
    }
    setBusy(false);
  };
  return (
    <Card eyebrow="Step 1" title="What should it do?">
      <div className="grid gap-4 max-w-[760px]">
        <label className="grid gap-1">
          <span className="label m-0">In a sentence</span>
          <textarea className="input" rows={2} value={intent} placeholder="Describe what should be copied, from where to where"
            onChange={(e) => { setTouched(true); setIntent(e.target.value); }} aria-label="What should it do" />
        </label>
        <div className="rounded-md bg-sunken px-4 py-3 grid gap-1 text-[13.5px]">
          <b>What an integration can do</b>
          <ul className="m-0 pl-5 grid gap-0.5">
            <li>One page is one record.</li>
            <li>Each value comes from the page, from the record, from today's date, from a constant, or from code.</li>
            <li>It reads pages and APIs.</li>
          </ul>
        </div>
        <Problem text={problem} />
        <div><button className="btn btn-primary" disabled={busy || !intent.trim()} onClick={create}>Continue</button></div>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------------------- 2. where
type Kind = DestinationInput["kind"];

function StepWhere({ draft, onChecked }: { draft: Draft; onChecked: () => void }) {
  const { credentials, setCredsOpen } = useApp();
  const was = draft.destination as Record<string, string | undefined> | null;
  const [kind, setKind] = useState<Kind>((was?.kind as Kind) ?? "airtable");
  const [base, setBase] = useState(was?.base_id ?? rememberedBase());
  const [table, setTable] = useState(was?.table ?? "");
  const [tables, setTables] = useState<string[] | null>(null);
  const names = (credentials ?? []).map((c) => c.name);
  const [credential, setCredential] = useState(was?.credential ?? "");
  const cred = credential || (names.includes("AIRTABLE_TOKEN") ? "AIRTABLE_TOKEN" : names[0] ?? "");
  const [url, setUrl] = useState(was?.url ?? "");
  const [apiBase, setApiBase] = useState(was?.base_url ?? "");
  const [ops, setOps] = useState<ApiOperation[] | null>(null);
  const [op, setOp] = useState("");
  const [dbPath, setDbPath] = useState(was?.path ?? "");
  const [dbTable, setDbTable] = useState(was?.table ?? "");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<DestinationCheck | null>(null);
  const isSet = (n: string) => credentials?.find((c) => c.name === n)?.set;
  const baseId = base.match(APP_ID)?.[0] ?? "";

  const findTables = async () => {
    setProblem(null);
    setBusy(true);
    try {
      setTables((await api.airtableTables(baseId, cred)).map((t) => t.name));
    } catch (e) {
      setProblem(errText(e));
    }
    setBusy(false);
  };
  const findOps = async () => {
    setProblem(null);
    setBusy(true);
    try {
      const l = await api.apiOperations(apiBase, credential || undefined);
      setOps(l);
      if (l[0]) setOp(`${l[0].method} ${l[0].path}`);
    } catch (e) {
      setProblem(errText(e));
    }
    setBusy(false);
  };
  const input = (): DestinationInput | null => {
    if (kind === "airtable") return base.trim() && table.trim() && cred ? { kind, ...(base.includes("://") ? { base_url: base.trim() } : { base_id: base.trim() }), table: table.trim(), credential: cred } : null;
    if (kind === "form") return url.trim() ? { kind, url: url.trim() } : null;
    if (kind === "api") {
      const [method, path] = op.split(" ");
      return apiBase.trim() && method && path ? { kind, base_url: apiBase.trim(), operation: { method, path }, ...(credential ? { credential } : {}) } : null;
    }
    return dbPath.trim() && dbTable.trim() ? { kind, path: dbPath.trim(), table: dbTable.trim() } : null;
  };
  const check = async () => {
    const d = input();
    if (!d) return;
    setProblem(null);
    setResult(null);
    setBusy(true);
    try {
      setResult(await api.putDestination(draft.name, d));
      if (d.kind === "airtable") rememberBase(base.trim());
    } catch (e) {
      setProblem(errText(e));
    }
    setBusy(false);
  };
  const CredPick = ({ optional = false }: { optional?: boolean }) => (
    <label className="grid gap-1">
      <span className="label m-0">Credential</span>
      <span className="flex flex-wrap gap-2 items-center">
        <select className="input w-auto min-w-[220px] font-mono text-[13px]" aria-label="Credential" value={optional ? credential : cred} onChange={(e) => setCredential(e.target.value)}>
          {optional && <option value="">None</option>}
          {names.map((n) => <option key={n} value={n}>{n}{isSet(n) ? "" : " (not set)"}</option>)}
        </select>
        <button className="btn" type="button" onClick={() => setCredsOpen(true)}>Add a credential</button>
      </span>
    </label>
  );
  return (
    <Card eyebrow="Step 2" title="Where should it write?">
      <div className="grid gap-4 max-w-[760px]">
        <Tabs value={kind} onChange={(k) => { setKind(k); setResult(null); setProblem(null); }}
          items={[{ id: "airtable", label: "Airtable" }, { id: "form", label: "Web form" }, { id: "api", label: "API" }, { id: "sqlite", label: "SQLite" }]} />
        {kind === "airtable" && (
          <>
            <label className="grid gap-1"><span className="label m-0">Base (its address or id)</span>
              <input className="input font-mono" aria-label="Base" value={base} placeholder="https://airtable.com/app…" onChange={(e) => { setBase(e.target.value); setTables(null); }} /></label>
            <CredPick />
            <label className="grid gap-1"><span className="label m-0">Table</span>
              <span className="flex flex-wrap gap-2 items-center">
                {tables ? (
                  <select className="input w-auto min-w-[220px]" aria-label="Table" value={table} onChange={(e) => setTable(e.target.value)}>
                    <option value="">Choose a table…</option>
                    {tables.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                ) : (
                  <input className="input w-auto min-w-[220px]" aria-label="Table" value={table} placeholder="Table name" onChange={(e) => setTable(e.target.value)} />
                )}
                <button className="btn" type="button" disabled={busy || !baseId || !cred} onClick={findTables}>Find tables</button>
              </span></label>
          </>
        )}
        {kind === "form" && (
          <label className="grid gap-1"><span className="label m-0">Form address</span>
            <input className="input font-mono" aria-label="Form address" value={url} placeholder="https://…" onChange={(e) => setUrl(e.target.value)} /></label>
        )}
        {kind === "api" && (
          <>
            <label className="grid gap-1"><span className="label m-0">API address</span>
              <input className="input font-mono" aria-label="API address" value={apiBase} placeholder="https://api.example.com" onChange={(e) => { setApiBase(e.target.value); setOps(null); }} /></label>
            <CredPick optional />
            <div className="flex flex-wrap gap-2 items-center">
              <button className="btn" type="button" disabled={busy || !apiBase.trim()} onClick={findOps}>Find operations</button>
              {ops && (
                <select className="input w-auto min-w-[260px]" aria-label="Operation" value={op} onChange={(e) => setOp(e.target.value)}>
                  {ops.map((o) => <option key={`${o.method} ${o.path}`} value={`${o.method} ${o.path}`}>{o.method} {o.path} · {o.summary}</option>)}
                </select>
              )}
            </div>
          </>
        )}
        {kind === "sqlite" && (
          <>
            <label className="grid gap-1"><span className="label m-0">Database file</span>
              <input className="input font-mono" aria-label="Database file" value={dbPath} onChange={(e) => setDbPath(e.target.value)} /></label>
            <label className="grid gap-1"><span className="label m-0">Table</span>
              <input className="input" aria-label="Table" value={dbTable} onChange={(e) => setDbTable(e.target.value)} /></label>
          </>
        )}
        <Problem text={problem} />
        {result && (
          <div className="rounded-md px-4 py-3 grid gap-2" style={{ background: "var(--ok-soft)" }} role="status">
            <b style={{ color: "var(--ok)" }}>✓ Connected to {result.title}</b>
            <span className="text-[13px]">It wants: {result.fields.map((f) => `${f.name} (${f.type}${f.required ? ", required" : ""})`).join(" · ")}</span>
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          <button className="btn" disabled={busy || !input()} onClick={check}>{busy ? <Spinner /> : null}Check the connection</button>
          <button className="btn btn-primary" disabled={!result} onClick={onChecked}>Continue</button>
        </div>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------------------- 3. examples
function StepExamples({ draft, pages, onTaught, onBack }: {
  draft: Draft; pages: PageRecord[]; onTaught: (threadId: string | null, aiOff: boolean) => void; onBack: () => void;
}) {
  const { extension } = useApp();
  const [off, setOff] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const chosen = pages.filter((p) => !off.has(p.id));
  const sites = new Set(chosen.map((p) => p.site));
  const teach = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const r = await api.teach(draft.name, chosen.map((p) => p.id));
      onTaught(r.thread_id, false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && /\bAI is off\b/i.test(e.detail)) onTaught(null, true);
      else setProblem(errText(e));
    }
    setBusy(false);
  };
  return (
    <Card eyebrow="Step 3" title="Show it a few pages">
      <div className="grid gap-4 max-w-[760px]">
        <ol className="m-0 pl-5 grid gap-1 text-[14px]">
          <li>In the extension's options, send pages to <span className="font-mono font-semibold">{draft.name}</span>{extension ? "" : " (setup is on any integration's Setup section)"}.</li>
          <li>Open an example page and press <kbd className="font-mono font-semibold">Alt+Shift+P</kbd>. Send two or three, ideally from different sites.</li>
          <li>They appear below as they arrive. Nothing is written anywhere yet.</li>
        </ol>
        {pages.length === 0 ? (
          <Empty>Waiting for the first page…</Empty>
        ) : (
          <ul className="m-0 p-0 list-none border border-line rounded-md" aria-label="Pages sent">
            {pages.map((p) => (
              <li key={p.id} className="flex items-center gap-3 px-3 py-2 border-b border-line last:border-b-0">
                <input type="checkbox" aria-label={`Use ${p.title || p.url}`} checked={!off.has(p.id)}
                  onChange={() => setOff((s) => { const n = new Set(s); if (n.has(p.id)) n.delete(p.id); else n.add(p.id); return n; })} />
                <span className="grid min-w-0">
                  <span className="font-semibold text-[14px] truncate">{p.title || p.url}</span>
                  <span className="font-mono text-xs text-muted truncate">{p.site}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
        <Problem text={problem} />
        <div className="flex flex-wrap gap-2 items-center">
          <button className="btn" onClick={onBack}>Back</button>
          <button className="btn btn-primary" disabled={busy || chosen.length === 0} onClick={teach}>
            Teach from {chosen.length} page{chosen.length === 1 ? "" : "s"}
          </button>
          {chosen.length > 0 && <span className="text-muted text-[13px]">{sites.size} site{sites.size === 1 ? "" : "s"}</span>}
        </div>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------------------- 4. review
function StepReview({ draft, threadId, aiOff, pages, retry, retryError, onRestart }: {
  draft: Draft; threadId: string | null; aiOff: boolean; pages: PageRecord[]; retry: () => void; retryError: string | null; onRestart: () => void;
}) {
  const { settings, act, refresh, navigate } = useApp();
  const { view } = useAgentThread(threadId);
  const [busy, setBusy] = useState(false);
  const labels = useMemo(() => Object.fromEntries(pages.map((p) => [p.id, p.site])), [pages]);
  const live = draft.state === "live";
  const rows = usePoll(() => (live ? api.tracker({ integration: draft.name, limit: 50 }) : Promise.resolve(null)), 0, [live]);

  if (!threadId) {
    return (
      <Card eyebrow="Step 4" title="Review what it proposes">
        <div className="grid gap-3 max-w-[760px]">
          {aiOff && !settings?.ai_enabled ? (
            <>
              <p className="m-0 font-semibold">Turn AI on to let Patchwork propose a mapping.</p>
              <p className="m-0 text-muted text-[13.5px]">The model only proposes. Every value it suggests is checked by code against your example pages before you see it.</p>
              <div><AiToggle /></div>
            </>
          ) : (
            <p className="m-0">AI is on. Ready when you are.</p>
          )}
          <Problem text={retryError} />
          <div className="flex gap-2"><button className="btn" onClick={onRestart}>Back to examples</button>
            <button className="btn btn-primary" disabled={!settings?.ai_enabled} onClick={retry}>Teach now</button></div>
        </div>
      </Card>
    );
  }
  if (live) {
    const written = (rows.data?.rows ?? []).filter((r) => r.status === "success").length;
    return (
      <Card eyebrow="Step 4" title="It's live">
        <div className="grid gap-3 max-w-[760px]">
          <p className="m-0">
            <b>{draft.name}</b> is live. {written ? `${written} row${written === 1 ? "" : "s"} written from your examples.` : "Your example pages are being written."}
          </p>
          <div><button className="btn btn-primary" onClick={() => navigate({ page: "integration", name: draft.name, section: "rows" })}>Open {draft.name}</button></div>
        </div>
      </Card>
    );
  }
  const pending = view.pending?.type === "request_deploy" ? view.pending : null;
  // the agent may stop to ask before it proposes (a field no example page shows): the person answers here
  const asking = view.pending?.type === "ask_user" ? view.pending : null;
  const decide = async (decision: "approve" | "reject", edits: ReviewEdit[]) => {
    if (!pending) return;
    setBusy(true);
    await act(() => api.resume(threadId, { interrupt_id: pending.interrupt_id, decision, ...(edits.length ? { edits } : {}) }));
    setBusy(false);
    refresh();
  };
  // only a rejected teach ends without going live; right after Approve the draft is still catching up
  const finishedWithout = view.state === "done" && !live && draft.state === "draft";
  return (
    <Card eyebrow="Step 4" title="Review what it proposes">
      <div className="grid gap-4">
        {asking && threadId && (
          <div className="grid gap-2">
            <p className="m-0 text-[14px]">Patchwork needs an answer before it can propose the mapping.</p>
            <AskUserCard ev={asking} threadId={threadId} />
          </div>
        )}
        {!pending && !asking && !finishedWithout && (
          <div className="grid gap-2" aria-live="polite">
            <span className="inline-flex items-center gap-2 text-muted"><Spinner /> Patchwork is reading your examples…</span>
            <ol className="m-0 p-0 list-none grid gap-1 text-[14px]">
              {view.todos.map((t, i) => (
                <li key={i} style={{ color: t.status === "completed" ? "var(--muted)" : undefined, fontWeight: t.status === "in_progress" ? 600 : 400 }}>
                  {t.status === "completed" ? "✓" : t.status === "in_progress" ? "…" : "·"} {t.content}
                </li>
              ))}
            </ol>
          </div>
        )}
        {pending && (
          <>
            <p className="m-0 text-[14px]">
              {pending.summary}. Rows marked <span className="pill" style={{ color: "var(--accent)", background: "var(--accent-soft)" }}>AI</span> were
              proposed by the model and checked by code. Change anything you disagree with, then approve.
            </p>
            {pending.review && pending.review.length > 0 ? (
              <ReviewTable rows={pending.review} pages={labels} busy={busy} onDecide={decide} />
            ) : (
              <Empty>The proposal has no rows to review.</Empty>
            )}
          </>
        )}
        {finishedWithout && (
          <div className="grid gap-2">
            <p className="m-0">Nothing was made live. Your example pages are still here.</p>
            <div><button className="btn" onClick={onRestart}>Back to examples</button></div>
          </div>
        )}
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------------------- wizard
export default function NewIntegration() {
  const { route, navigate } = useApp();
  const name = route.page === "new" ? route.draft : undefined;
  const draftQ = usePoll(() => (name ? api.draft(name) : Promise.resolve(null)), name ? 1500 : 0, [name]);
  const draft = name && draftQ.data?.name === name ? draftQ.data : null;
  const pagesQ = usePoll(() => (name ? api.pages({ integration: name }) : Promise.resolve([] as PageRecord[])), name ? 1500 : 0, [name]);
  const pages = name ? pagesQ.data ?? [] : [];

  // where the person is: from the draft (so a reload resumes), moved by the buttons
  const natural = !name ? 1 : !draft ? 0 : draft.state === "draft" ? (draft.destination ? 3 : 2) : 4;
  const [override, setOverride] = useState<number | null>(null);
  const [tid, setTid] = useState<string | null>(null);
  const [aiOff, setAiOff] = useState(false);
  useEffect(() => {
    setOverride(null);
    setTid(null);
    setAiOff(false);
  }, [name]);
  const step = name && !draft ? 0 : override ?? natural;
  const threadId = tid ?? draft?.thread_id ?? null;
  const [retryError, setRetryError] = useState<string | null>(null);

  if (name && !draft) {
    return draftQ.error ? (
      <Empty action={<button className="btn btn-primary" onClick={() => navigate({ page: "new" })}>Start a new integration</button>}>
        Couldn't find the draft {name}: {draftQ.error}
      </Empty>
    ) : (
      <span className="inline-flex items-center gap-2 text-muted"><Spinner /> Loading…</span>
    );
  }
  const taught = (id: string | null, off: boolean) => {
    setTid(id);
    setAiOff(off);
    setOverride(4);
    void draftQ.reload();
  };
  const reteach = async () => {
    if (!draft) return;
    setRetryError(null);
    try {
      taught((await api.teach(draft.name, draft.example_page_ids.length ? draft.example_page_ids : pages.map((p) => p.id))).thread_id, false);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 409 && /\bAI is off\b/i.test(e.detail))) setRetryError(errText(e));
    }
  };
  return (
    <>
      <PageHead eyebrow="New integration" title={draft ? draft.intent : "New integration"}>
        Say what it should do, point it at where the data goes, show it a few examples, and approve what it proposes.
      </PageHead>
      <Steps step={step} />
      {step === 1 && <StepWhat onCreated={(d) => navigate({ page: "new", draft: d.name })} />}
      {step === 2 && draft && <StepWhere draft={draft} onChecked={() => { void draftQ.reload(); setOverride(3); }} />}
      {step === 3 && draft && <StepExamples draft={draft} pages={pages} onTaught={taught} onBack={() => setOverride(2)} />}
      {step === 4 && draft && (
        <StepReview draft={draft} threadId={threadId} aiOff={aiOff} pages={pages} retry={reteach} retryError={retryError}
          onRestart={() => { setTid(null); setAiOff(false); setOverride(3); }} />
      )}
    </>
  );
}
