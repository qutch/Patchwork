import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  api, streams, ApiError,
  type AgentEvent, type Credential, type ExtensionInfo, type IncidentSummary, type IntegrationSummary, type PageIntegration,
  type Settings, type ThreadStatus,
} from "./api";
import { deriveThread, mergeEvents } from "./lib/derive";
import { parseHash, toHash, type Route } from "./routes";
import { defaultIntegration, pageIntegrations, pickIntegration, specInfo, V1_INTEGRATION, type SpecInfo } from "./lib/integrations";

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
  route: Route;
  navigate: (r: Route) => void;
  /** the Credentials drawer (opened from the header, Home and the wizard) */
  credsOpen: boolean;
  setCredsOpen: (open: boolean) => void;
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
  /** the v1 integration the Review, Integration and Runs tabs follow */
  integrationName: string;
  /** `GET /api/extension` (token, control URL and, from V15, the page integrations) */
  extension: ExtensionInfo | null;
  extensionError: string | null;
  /** the page integrations to choose from (never empty; jobs_to_tracker on older backends) */
  pageIntegrations: PageIntegration[];
  /** the one the Tracker, Learned layouts and presenter mode show */
  pageIntegration: string;
  setPageIntegration: (name: string) => void;
  isPageIntegration: (name: string) => boolean;
  /** V13 credential names and whether they're set; null when the backend has no /api/credentials */
  credentials: Credential[] | null;
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

function routeFromHash(): Route {
  const h = typeof location !== "undefined" ? location.hash : "";
  return parseHash(h, store.get("pw-page-integration") ?? undefined);
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [route, setRoute] = useState<Route>(routeFromHash);
  const [credsOpen, setCredsOpen] = useState(false);
  useEffect(() => {
    // an old tab hash (#tracker, #agent, ...) is rewritten to its new address
    const canon = toHash(route);
    if (typeof location !== "undefined" && location.hash && location.hash !== canon) history.replaceState(null, "", canon);
    const on = () => setRoute(routeFromHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const navigate = useCallback((r: Route) => {
    const h = toHash(r);
    if (location.hash !== h) location.hash = h;
    setRoute(r);
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
  const extensionQ = usePoll(api.extension, 10000);
  const credentialsQ = usePoll(api.credentials, 5000);

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
    void credentialsQ.reload();
  }, [threadsQ, incidentsQ, integrationsQ, settingsQ, credentialsQ]);

  const setAi = useCallback(async (on: boolean) => {
    const prev = settingsQ.data;
    if (prev) settingsQ.setData({ ...prev, ai_enabled: on });
    const s = await act(() => api.putSettings({ ai_enabled: on }));
    if (s) settingsQ.setData(s);
    else void settingsQ.reload();
  }, [act, settingsQ]);

  const integrations = integrationsQ.data ?? [];
  const pageList = useMemo(() => pageIntegrations(extensionQ.data, integrationsQ.data), [extensionQ.data, integrationsQ.data]);
  const pageNames = useMemo(() => new Set(pageList.map((i) => i.name)), [pageList]);
  const isPageIntegration = useCallback((n: string) => pageNames.has(n), [pageNames]);
  // ?integration=X (presenter links) wins over the remembered choice
  const [storedPage, setStoredPage] = usePersisted("pw-page-integration");
  const [urlPage, setUrlPage] = useState(() => (typeof location !== "undefined" ? new URLSearchParams(location.search).get("integration") : null));
  const wanted = urlPage ?? storedPage;
  // until the extension info has loaded, keep the wanted name so a reload doesn't flash the default integration
  const loaded = extensionQ.data !== null || extensionQ.error !== null;
  const routedName = route.page === "integration" ? route.name : null;
  const chosenIntegration = loaded ? pickIntegration(wanted, pageList, defaultIntegration(extensionQ.data, pageList)) : wanted ?? pageList[0].name;
  // the integration page being shown wins over the remembered choice (the Tracker, Layouts and Presenter follow it)
  const pageIntegration = routedName ?? chosenIntegration;
  const setPageIntegration = useCallback((n: string) => {
    setUrlPage(null);
    setStoredPage(n);
  }, [setStoredPage]);
  const value: Ctx = {
    route, navigate, credsOpen, setCredsOpen,
    settings: settingsQ.data,
    setAi,
    apiDown: !!settingsQ.error && !settingsQ.data,
    recordingId, setRecordingId,
    threadId, setThreadId,
    runId, setRunId,
    threads: threadsQ.data ?? [],
    incidents: incidentsQ.data ?? [],
    integrations,
    // v1 tabs (Review, Integration, Runs) follow the v1 integration; the Tracker tab follows the chosen page integration
    integrationName: routedName ?? integrations.find((i) => i.name === V1_INTEGRATION)?.name ?? integrations.find((i) => !pageNames.has(i.name))?.name ?? V1_INTEGRATION,
    extension: extensionQ.data,
    extensionError: extensionQ.error,
    pageIntegrations: pageList,
    pageIntegration,
    setPageIntegration,
    isPageIntegration,
    credentials: credentialsQ.data ?? null,
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

/**
 * What the page integration does and where it writes: the control plane's own `destination`/`intent` when it lists
 * them, else read from the active version's spec (`GET /api/integrations/{name}/versions/{active}` -> `spec_yaml`).
 * Refetched when the active version changes. Fields stay null when nothing says (older backends, no version yet).
 */
export function useIntegrationInfo(name: string): SpecInfo {
  const { integrations, pageIntegrations: list } = useApp();
  const active = integrations.find((i) => i.name === name)?.active_version ?? null;
  const listed = list.find((i) => i.name === name);
  const [spec, setSpec] = useState<{ key: string; info: SpecInfo } | null>(null);
  const key = `${name}@${active}`;
  useEffect(() => {
    if (active == null) return;
    let live = true;
    api.version(name, active).then(
      (v) => live && setSpec({ key, info: specInfo(v.spec_yaml) }),
      () => live && setSpec({ key, info: specInfo(null) }),
    );
    return () => {
      live = false;
    };
  }, [name, active, key]);
  const fromSpec = spec?.key === key ? spec.info : null;
  return useMemo(() => ({
    intent: listed?.intent ?? fromSpec?.intent ?? null,
    destination: listed?.destination ?? listed?.sink ?? fromSpec?.destination ?? null,
    sinkFields: fromSpec?.sinkFields ?? [],
  }), [listed, fromSpec]);
}
