import { useEffect, useState } from "react";
import { api, TRACKER, type Incident } from "../api";
import { useApp } from "../state";
import { AiToggle } from "./AiToggle";

function headline(inc: Incident): string {
  if (inc.type !== "CONTRACT_CHANGED") return inc.summary;
  return inc.integration === TRACKER ? "Blocked before any write: the tracker form changed" : "Run blocked before any write: Stockroom's contract changed";
}

/** An open incident with the Repair flow. `onStarted` replaces the default jump to Agent Activity. */
export function IncidentBanner({ id, big = false, onStarted }: { id: number; big?: boolean; onStarted?: (threadId: string) => void }) {
  const { settings, act, setThreadId, go, refresh } = useApp();
  const [inc, setInc] = useState<Incident | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.incident(id).then(setInc, () => setInc(null));
  }, [id]);
  if (!inc) return null;
  const errors = inc.detail?.contract_errors ?? [];
  const ai = !!settings?.ai_enabled;
  const repair = async () => {
    setBusy(true);
    const r = await act(() => api.repair(inc.id));
    setBusy(false);
    if (r) {
      setThreadId(r.thread_id);
      refresh();
      if (onStarted) onStarted(r.thread_id);
      else go("agent");
    }
  };
  return (
    <section className="rounded-md border-2 overflow-hidden" style={{ borderColor: "var(--bad)", background: "var(--bad-soft)" }} role="alert">
      <div className="px-5 py-4 flex flex-wrap items-center gap-4 justify-between">
        <div className="grid gap-1 min-w-0">
          <div className="font-mono text-[12px] font-bold uppercase tracking-wider" style={{ color: "var(--bad)" }}>
            Incident #{inc.id} · {[inc.type, ...inc.reason_codes.filter((c) => c !== inc.type)].join(" · ")} · v{inc.version}
          </div>
          <div className={`display ${big ? "text-[34px]" : "text-[22px]"} font-extrabold leading-tight`} style={{ color: "var(--ink)" }}>
            {headline(inc)}
          </div>
          <div className={big ? "text-[20px]" : "text-[15px]"}>{inc.summary}</div>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          {!ai && <AiToggle big={big} />}
          <button className="btn btn-lg btn-primary" onClick={repair} disabled={busy || !ai} title={ai ? "Start the REPAIR agent" : "Turn AI on to repair"}>
            Repair with AI →
          </button>
        </div>
      </div>
      {errors.length > 0 && (
        <div className="px-5 pb-4">
          <button className="btn" onClick={() => setOpen((o) => !o)}>
            {open ? "Hide" : "Show"} {errors.length} contract error{errors.length === 1 ? "" : "s"}
          </button>
          {open && (
            <pre className="code rounded mt-2" style={{ maxHeight: 260 }}>
              {errors.join("\n")}
            </pre>
          )}
        </div>
      )}
    </section>
  );
}
