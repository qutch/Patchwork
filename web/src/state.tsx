import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  api, streams, ApiError,
  type AgentEvent, type IncidentSummary, type IntegrationSummary, type Settings, type ThreadStatus,
} from "./api";
import { deriveThread, mergeEvents } from "./lib/derive";

export type Tab = "record" | "review" | "agent" | "integration" | "runs" | "bench";
export const TABS: { id: Tab; label: string }[] = [
  { id: "record", label: "Record" },
  { id: "review", label: "Review" },
  { id: "agent", label: "Agent Activity" },
  { id: "integration", label: "Integration" },
  { id: "runs", label: "Runs" },
  { id: "bench", label: "Benchmark" },
];

const store = {
  get(k: string): string | null {
    try {
      return localStorage.getItem(k);
    } catch {
      return null;
    }
  },
  set(k: string, v: string | null) {
    try {
      if (v === null) localStorage.removeItem(k);
      else localStorage.setItem(k, v);
    } catch {
      /* private mode: state just won't survive a reload */
    }
  },
};

function usePersisted(key: string): [string | null, (v: string | null) => void] {
  const [v, setV] = useState<string | null>(() => store.get(key));
  const set = useCallback((nv: string | null) => {
    store.set(key, nv);
    setV(nv);
  }, [key]);
  return [v, set];
}

/** Poll `fn` every `ms` while mounted; `reload()` fetches now. */
export function usePoll<T>(fn: () => Promise<T>, ms: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async () => {
    try {
      const d = await fnRef.current();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    void reload();
    if (!ms) return;
    const id = setInterval(reload, ms);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, reload, ...deps]);
  return { data, error, reload, setData };
}

interface Ctx {
  tab: Tab;
  go: (t: Tab) => void;
  settings: Settings | null;
  setAi: (on: boolean) => Promise<void>;
  apiDown: boolean;
  recordingId: string | null;
  setRecordingId: (id: string | null) => void;
  threadId: string | null;
  setThreadId: (id: string | null) => void;
  runId: number | null;
  setRunId: (id: number | null) => void;
  threads: ThreadStatus[];
  incidents: IncidentSummary[];
  integrations: IntegrationSummary[];
  integrationName: string;
  refresh: () => void;
  notice: { kind: "error" | "info"; text: string } | null;
  notify: (kind: "error" | "info", text: string) => void;
  /** Run an API action and surface its error as a notice. */
  act: <T>(fn: () => Promise<T>) => Promise<T | undefined>;
}
const AppCtx = createContext<Ctx | null>(null);
export const useApp = () => {
  const c = useContext(AppCtx);
  if (!c) throw new Error("useApp outside provider");
  return c;
};

function tabFromHash(): Tab {
  const h = (typeof location !== "undefined" ? location.hash : "").replace("#", "");
  return (TABS.find((t) => t.id === h)?.id ?? "record") as Tab;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [tab, setTab] = useState<Tab>(tabFromHash);
  useEffect(() => {
    const on = () => setTab(tabFromHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const go = useCallback((t: Tab) => {
    if (location.hash !== `#${t}`) location.hash = t;
    setTab(t);
    window.scrollTo({ top: 0 });
  }, []);

  const [recordingId, setRecordingId] = usePersisted("pw-recording");
  const [threadId, setThreadId] = usePersisted("pw-thread");
  const [runIdS, setRunIdS] = usePersisted("pw-run");
  const runId = runIdS ? Number(runIdS) : null;
  const setRunId = useCallback((id: number | null) => setRunIdS(id == null ? null : String(id)), [setRunIdS]);

  const [notice, setNotice] = useState<Ctx["notice"]>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout>>();
  const notify = useCallback((kind: "error" | "info", text: string) => {
    setNotice({ kind, text });
    clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), kind === "error" ? 7000 : 3500);
  }, []);
  const act = useCallback(async <T,>(fn: () => Promise<T>) => {
    try {
      return await fn();
    } catch (e) {
      notify("error", e instanceof ApiError ? `${e.status}: ${e.detail}` : e instanceof Error ? e.message : String(e));
      return undefined;
    }
  }, [notify]);

  const settingsQ = usePoll(api.settings, 5000);
  const threadsQ = usePoll(api.threads, 1500);
  const incidentsQ = usePoll(() => api.incidents("open"), 2000);
  const integrationsQ = usePoll(api.integrations, 2000);

  // A thread remembered from an earlier session may be gone (demo reset, new workspace). Check once,
  // on the first thread list: later checks could race a thread created a moment ago.
  const checkedThread = useRef(false);
  useEffect(() => {
    if (checkedThread.current || !threadsQ.data) return;
    checkedThread.current = true;
    if (threadId && !threadsQ.data.some((t) => t.thread_id === threadId)) setThreadId(null);
  }, [threadsQ.data, threadId, setThreadId]);

  const refresh = useCallback(() => {
    void threadsQ.reload();
    void incidentsQ.reload();
    void integrationsQ.reload();
    void settingsQ.reload();
  }, [threadsQ, incidentsQ, integrationsQ, settingsQ]);

  const setAi = useCallback(async (on: boolean) => {
    const prev = settingsQ.data;
    if (prev) settingsQ.setData({ ...prev, ai_enabled: on });
    const s = await act(() => api.putSettings({ ai_enabled: on }));
    if (s) settingsQ.setData(s);
    else void settingsQ.reload();
  }, [act, settingsQ]);

  const integrations = integrationsQ.data ?? [];
  const value: Ctx = {
    tab, go,
    settings: settingsQ.data,
    setAi,
    apiDown: !!settingsQ.error && !settingsQ.data,
    recordingId, setRecordingId,
    threadId, setThreadId,
    runId, setRunId,
    threads: threadsQ.data ?? [],
    incidents: incidentsQ.data ?? [],
    integrations,
    integrationName: integrations[0]?.name ?? "po_to_stockroom",
    refresh, notice, notify, act,
  };
  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>;
}

/** Live agent thread: initial fetch via the stream (after=0), then follow; reconnects with ?after=. */
export function useAgentThread(threadId: string | null) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [conn, setConn] = useState<"open" | "reconnecting" | "closed">("closed");
  useEffect(() => {
    setEvents([]);
    if (!threadId) return;
    let buf: AgentEvent[] = [];
    let raf = 0;
    const flush = () => {
      raf = 0;
      const b = buf;
      buf = [];
      setEvents((list) => mergeEvents(list, b));
    };
    const h = streams.agent(threadId, {
      onMessage: (m) => {
        buf.push(m.data);
        if (!raf) raf = requestAnimationFrame(flush);
      },
      isTerminal: (m) => m.data.type === "status" && (m.data.state === "done" || m.data.state === "failed"),
      onStateChange: setConn,
    });
    return () => {
      h.close();
      if (raf) cancelAnimationFrame(raf);
    };
  }, [threadId]);
  const view = useMemo(() => deriveThread(events), [events]);
  return { events, view, conn };
}

export function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e);
}
