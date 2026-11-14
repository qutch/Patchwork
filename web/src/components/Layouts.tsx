import { useState } from "react";
import { api, type Layout, type LearnPolicy } from "../api";
import { clock, layoutRows, originText } from "../lib/tracker";
import { useApp, usePoll } from "../state";
import { Card, Empty } from "./ui";

/** "css .posting-headline h2", "JSON-LD hiringOrganization.name", … */
export function readerText(x: unknown): string {
  if (!x || typeof x !== "object") return String(x ?? "");
  const f = x as Record<string, unknown>;
  const parts: string[] = [];
  if (f.jsonld) parts.push(`JSON-LD ${f.jsonld}`);
  if (f.css) parts.push(`css ${f.css}${f.attr ? ` @${f.attr}` : ""}`);
  if (f.regex) parts.push(`regex ${f.regex}`);
  if (f.from) parts.push(`from ${f.from}`);
  if ("const" in f) parts.push(`always ${JSON.stringify(f.const)}`);
  return parts.join(" → ") || JSON.stringify(f);
}

function PolicySwitch() {
  const { settings, act, refresh, notify } = useApp();
  const policy: LearnPolicy = settings?.learn_policy ?? "auto";
  const set = async (p: LearnPolicy) => {
    if (p === policy) return;
    const r = await act(() => api.putSettings({ learn_policy: p }));
    if (r) {
      refresh();
      notify("info", p === "auto" ? "New layouts are used as soon as every code check passes." : "Every new layout now waits for a person.");
    }
  };
  return (
    <div className="flex items-center gap-1" role="radiogroup" aria-label="When a new layout is learned">
      <span className="font-mono text-xs text-muted mr-1">new layouts:</span>
      {(["auto", "approve"] as const).map((p) => (
        <button key={p} className="tab-btn" role="radio" aria-checked={policy === p} aria-pressed={policy === p} onClick={() => set(p)}
          title={p === "auto" ? "Use a learned reader when every code check passes and nothing was ambiguous" : "Always ask a person before using a learned reader"}>
          {p === "auto" ? "use when checks pass" : "ask me first"}
        </button>
      ))}
    </div>
  );
}

function RevertButton({ l, last, integration }: { l: Layout; last: boolean; integration: string }) {
  const { act, notify, refresh } = useApp();
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!l.active) return <span className="font-mono text-xs text-muted">reverted</span>;
  if (last) return <span className="font-mono text-xs text-muted" title="The last reader can't be removed">last reader</span>;
  if (!confirm) return <button className="btn btn-danger" onClick={() => setConfirm(true)}>Revert…</button>;
  const go = async () => {
    setBusy(true);
    const r = await act(() => api.revertLayout(l.parser_id, integration));
    setBusy(false);
    setConfirm(false);
    if (r) {
      notify("info", `Reverted: v${r.version} deployed without ${l.parser_id}. New ${l.site ?? "pages"} pages will be held or learned again.`);
      refresh();
    }
  };
  return (
    <span className="inline-flex gap-1.5 items-center flex-wrap justify-end">
      <span className="text-[12px]">Remove <b>{l.parser_id}</b>? No AI; tests run first.</span>
      <button className="btn btn-danger" onClick={go} disabled={busy}>{busy ? "Reverting…" : "Revert"}</button>
      <button className="btn" onClick={() => setConfirm(false)} disabled={busy}>Cancel</button>
    </span>
  );
}

/** Every reader the integration has used: where it came from, who approved it, one-click revert. */
export function LayoutsPanel({ integration }: { integration: string }) {
  const q = usePoll(async () => ({ integration, layouts: await api.layouts(integration) }), 2000, [integration]);
  // null until the chosen integration's layouts arrive (never the previous integration's)
  const data = q.data && q.data.integration === integration ? q.data.layouts : null;
  const rows = layoutRows(data ?? []);
  const nActive = rows.filter((l) => l.active).length;
  return (
    <Card eyebrow="Approvals history" title="Learned layouts" right={<PolicySwitch />} bodyClass="">
      {!data ? (
        <div className="card-b text-muted">{q.error ? `Couldn't load layouts: ${q.error}` : "Loading…"}</div>
      ) : !rows.length ? (
        <div className="card-b"><Empty>No readers yet. Teach the integration from one recording first.</Empty></div>
      ) : (
        <div className="overflow-x-auto">
          <table className="tbl">
            <thead>
              <tr><th>Site</th><th>Reads</th><th>Origin</th><th>Approved</th><th className="text-right">Since</th><th /></tr>
            </thead>
            <tbody>
              {rows.map((l) => {
                const o = originText(l.origin);
                return (
                  <tr key={l.parser_id} style={l.active ? undefined : { opacity: 0.6 }}>
                    <td>
                      <div className="font-semibold">{l.site ?? <span className="text-muted">any site</span>}</div>
                      <div className="font-mono text-[12px] text-muted">{l.parser_id}{l.fingerprint ? ` · fingerprint ${l.fingerprint}` : ""}</div>
                    </td>
                    <td className="font-mono text-[12px] min-w-[26ch] max-w-[44ch]">
                      {Object.entries(l.fields).map(([k, v]) => (
                        <div key={k} className="break-all"><span className="font-semibold">{k}</span> <span className="text-muted">← {readerText(v)}</span></div>
                      ))}
                    </td>
                    <td className="text-[13px]">
                      <div>{o.how}</div>
                      {l.origin?.at && <div className="text-muted text-[12px]">{l.origin.kind === "learned" ? "learned" : "at"} {clock(l.origin.at)}</div>}
                    </td>
                    <td className="text-[13px]">
                      <span className="pill" style={l.origin?.approved_by === "auto"
                        ? { color: "var(--accent)", background: "var(--accent-soft)" }
                        : l.origin?.approved_by === "human" ? { color: "var(--ok)", background: "var(--ok-soft)" } : { color: "var(--muted)", background: "var(--sunken)" }}>
                        {l.origin?.approved_by === "auto" ? "auto" : l.origin?.approved_by === "human" ? "person" : "—"}
                      </span>
                      <div className="text-[12px] text-muted mt-1">{o.approval}</div>
                    </td>
                    <td className="text-right font-mono text-[12.5px] num">v{l.introduced_in}</td>
                    <td className="text-right"><RevertButton l={l} last={l.active && nActive === 1} integration={integration} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
