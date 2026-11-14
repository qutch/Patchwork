import { useEffect, useRef, useState } from "react";
import { api, streams, type Candidates, type CaptureEvent, type RecordingSummary } from "../api";
import { describeEvent } from "../lib/derive";
import { useApp } from "../state";
import { Card, Empty, PageHead, Spinner } from "../components/ui";
import { MappingTable } from "./Review";

const DEFAULT_INTENT = "For every new purchase order message, record incoming stock";
const DEFAULT_ALLOW = "localhost:8001, localhost:8002";
const DEFAULT_URL = "http://localhost:8001/";

const KIND_COLOR: Record<string, string> = {
  navigate: "var(--muted)", GET: "var(--accent)", POST: "var(--ok)", PUT: "var(--ok)", PATCH: "var(--ok)", DELETE: "var(--bad)",
  click: "var(--ink)", copy: "var(--pnm)", paste: "var(--pnm)", input: "var(--val)", change: "var(--val)", submit: "var(--ok)",
};

export default function RecordTab() {
  const { recordingId, setRecordingId, setThreadId, go, act, settings, refresh } = useApp();
  const [intent, setIntent] = useState(DEFAULT_INTENT);
  const [allow, setAllow] = useState(DEFAULT_ALLOW);
  const [startUrl, setStartUrl] = useState(DEFAULT_URL);
  const [rec, setRec] = useState<RecordingSummary | null>(null);
  const [events, setEvents] = useState<CaptureEvent[]>([]);
  const [cands, setCands] = useState<Candidates | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const feedRef = useRef<HTMLDivElement>(null);

  // load the current recording, then follow its stream while it is live
  useEffect(() => {
    setRec(null);
    setEvents([]);
    setCands(null);
    if (!recordingId) return;
    let handle: { close(): void } | null = null;
    let cancelled = false;
    void (async () => {
      const r = await act(() => api.recording(recordingId));
      if (!r || cancelled) {
        if (!r && !cancelled) setRecordingId(null);
        return;
      }
      setRec(r);
      setIntent(r.intent);
      setAllow(r.allowlist.join(", "));
      setEvents(r.events);
      setCands(r.candidates);
      if (r.status === "recording") {
        handle = streams.recording(recordingId, {
          after: r.events.length,
          onMessage: (m) => {
            if (m.event === "capture") setEvents((xs) => [...xs, m.data as CaptureEvent]);
            else setRec((prev) => ({ ...(prev ?? r), ...(m.data as RecordingSummary), status: "stopped" }));
          },
          isTerminal: (m) => m.event === "stopped",
        });
      }
    })();
    return () => {
      cancelled = true;
      handle?.close();
    };
  }, [recordingId, act, setRecordingId]);

  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: "smooth" });
  }, [events.length]);

  const live = rec?.status === "recording";
  const allowlist = allow.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  const t0 = events[0]?.t ?? 0;
  const sawWrite = events.some((e) => e.type === "net" && e.method && e.method !== "GET");

  const start = async () => {
    setBusy("start");
    const r = await act(() => api.startRecording({ intent, allowlist, start_url: startUrl || undefined }));
    setBusy(null);
    if (r) setRecordingId(r.id);
  };
  const stop = async () => {
    if (!recordingId) return;
    setBusy("stop");
    const s = await act(() => api.stopRecording(recordingId));
    setBusy(null);
    if (s) setRec((prev) => ({ ...(prev ?? s), ...s, status: "stopped" }));
  };
  const find = async () => {
    if (!recordingId) return;
    setBusy("find");
    const c = await act(() => api.findCandidates(recordingId));
    setBusy(null);
    if (c) setCands(c);
  };
  const build = async () => {
    if (!recordingId) return;
    setBusy("build");
    const r = await act(() => api.build(recordingId));
    setBusy(null);
    if (r) {
      setThreadId(r.thread_id);
      refresh();
      go("agent");
    }
  };
  const reset = () => {
    setRecordingId(null);
  };

  return (
    <>
      <PageHead eyebrow="Step 1 · show it once" title="Record">
        Type one sentence of intent, then do the task once by hand in the browser that opens. Only requests to allowlisted hosts are
        captured, and secrets are redacted.
      </PageHead>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        <Card
          title="New recording"
          right={
            rec && (
              <span className="pill" style={{ color: live ? "var(--bad)" : "var(--muted)", background: live ? "var(--bad-soft)" : "var(--sunken)" }}>
                {live ? <><span className="w-2 h-2 rounded-full pulse" style={{ background: "var(--bad)" }} /> REC</> : "stopped"}
              </span>
            )
          }
        >
          <div className="grid gap-4">
            <div>
              <label className="label" htmlFor="intent">Intent</label>
              <textarea id="intent" className="input text-[16px] leading-snug" rows={3} value={intent} disabled={!!rec} onChange={(e) => setIntent(e.target.value)} />
            </div>
            <div>
              <label className="label" htmlFor="allow">Allowlist (host:port, comma separated)</label>
              <input id="allow" className="input font-mono" value={allow} disabled={!!rec} onChange={(e) => setAllow(e.target.value)} />
              <div className="flex flex-wrap gap-1.5 mt-2">
                {allowlist.map((h) => (
                  <span key={h} className="pill" style={{ background: "var(--accent-soft)", color: "var(--accent)" }}>{h}</span>
                ))}
              </div>
            </div>
            <div>
              <label className="label" htmlFor="url">Start URL</label>
              <input id="url" className="input font-mono" value={startUrl} disabled={!!rec} onChange={(e) => setStartUrl(e.target.value)} />
            </div>
            <div className="flex flex-wrap gap-2">
              {!rec && (
                <button className="btn btn-primary btn-lg" onClick={start} disabled={!!busy || !intent.trim() || !allowlist.length}>
                  ● Start recording
                </button>
              )}
              {live && (
                <button className="btn btn-lg" style={{ borderColor: "var(--bad)", color: "var(--bad)" }} onClick={stop} disabled={!!busy}>
                  ■ Stop
                </button>
              )}
              {rec && !live && (
                <button className="btn" onClick={reset}>New recording</button>
              )}
              {!rec && (
                <button className="btn" onClick={() => setRecordingId("fixture")} title="Play back fixtures/recording.json instead of a live browser">
                  Use fixture recording
                </button>
              )}
            </div>
            {rec && (
              <div className="font-mono text-xs text-muted grid gap-0.5">
                <span>id: {rec.id}{rec.id === "fixture" && " (fixtures/recording.json, played back)"}</span>
                <span>{events.length} events captured</span>
              </div>
            )}
          </div>
        </Card>

        <Card title="Live event feed" right={live && <span className="flex items-center gap-2 text-sm text-muted"><Spinner /> capturing</span>} bodyClass="">
          {events.length === 0 ? (
            <div className="p-4">
              <Empty>{live ? "Waiting for the first event from the browser…" : "Start a recording: navigations, clicks, copies, form input and allowlisted network calls stream in here."}</Empty>
            </div>
          ) : (
            <div ref={feedRef} className="max-h-[440px] overflow-auto">
              <ol className="m-0 p-0 list-none">
                {events.map((e, i) => {
                  const d = describeEvent(e);
                  return (
                    <li key={i} className="grid grid-cols-[56px_88px_minmax(0,1fr)] gap-3 px-4 py-2 border-b border-line items-baseline">
                      <span className="font-mono text-xs text-muted num">+{((e.t - t0) / 1000).toFixed(1)}s</span>
                      <span className="font-mono text-xs font-semibold uppercase" style={{ color: KIND_COLOR[d.kind] ?? "var(--ink)" }}>{d.kind}</span>
                      <span className="min-w-0">
                        <span className="text-[14px]">{d.text}</span>
                        {d.detail && <span className="block font-mono text-xs text-muted truncate">{d.detail}</span>}
                      </span>
                    </li>
                  );
                })}
              </ol>
            </div>
          )}
        </Card>
      </div>

      {rec && (
        <Card
          eyebrow="Step 2 · interpret"
          title="Mappings found in the recording"
          right={
            <>
              <button className="btn" onClick={find} disabled={!!busy || live || (!sawWrite && rec.id !== "fixture")}>
                {busy === "find" ? "Matching…" : cands ? "Re-run matcher" : "Find mappings"}
              </button>
              <button
                className="btn btn-primary btn-lg"
                onClick={build}
                disabled={!!busy || live || !settings?.ai_enabled}
                title={settings?.ai_enabled ? "Start the BUILD agent" : "Turn AI on to build"}
              >
                Build integration →
              </button>
            </>
          }
        >
          {live && <p className="m-0 text-muted">Stop the recording to look for mappings.</p>}
          {!live && !cands && (
            <p className="m-0 text-muted">
              The matcher finds each value you submitted to the destination inside what the source showed you, with no model involved.
              {!sawWrite && rec.id !== "fixture" && " No write to an allowlisted host was captured, so there is nothing to match yet."}
            </p>
          )}
          {cands && <MappingTable c={cands} />}
          {!settings?.ai_enabled && !live && <p className="mt-3 mb-0 text-sm text-muted">AI is off. Building needs the agents, so turn AI on in the header.</p>}
        </Card>
      )}
    </>
  );
}
