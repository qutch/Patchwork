// Cumulative AI calls vs postings (bench v2). One y-axis, two series:
// Patchwork (measured: solid) and a browser agent (estimate: dotted, as on the v1 chart).
// Colours are the validated --s-pw / --s-manual pair (dataviz validator: light and dark pass).
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { BenchKind } from "../api";
import { kindLabel, niceTicks, type AiSeries, type AiSeriesId, type LineStyle } from "../lib/bench";

const COLOR: Record<AiSeriesId, string> = { patchwork: "var(--s-pw)", browser_agent: "var(--s-manual)" };
const DASH: Record<LineStyle, string | undefined> = { measured: undefined, extrapolated: "8 5", estimate: "2 5" };
const soft = (k: BenchKind) => k !== "measured" && k !== "measured (earlier run)";
const approx = (k: BenchKind) => (soft(k) ? "≈ " : "");

function useWidth(fallback: number): [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth || fallback);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setW(el.clientWidth || fallback));
    ro.observe(el);
    return () => ro.disconnect();
  }, [fallback]);
  return [ref, w];
}

export function KindTag({ kind }: { kind: BenchKind }) {
  return (
    <span className="pill" style={soft(kind)
      ? { color: "var(--muted)", background: "transparent", border: "1px dashed var(--muted)", padding: "3px 6px" }
      : { color: "var(--ink)", background: "var(--sunken)", border: "1px solid transparent", padding: "3px 6px" }}>
      {kind === "estimate" ? "≈ " : ""}{kindLabel(kind)}
    </span>
  );
}

export function AiLegend({ series, big = false }: { series: AiSeries[]; big?: boolean }) {
  return (
    <div className={`flex flex-wrap gap-x-6 gap-y-2 items-center ${big ? "text-[18px]" : "text-[13.5px]"}`}>
      {series.map((s) => (
        <span key={s.id} className="inline-flex items-center gap-2">
          <svg width={big ? 40 : 28} height="10" aria-hidden>
            <line x1="1" y1="5" x2={big ? 39 : 27} y2="5" stroke={COLOR[s.id]} strokeWidth={big ? 3 : 2} strokeDasharray={DASH[s.style]} strokeLinecap="round" />
          </svg>
          <span className="font-semibold">{s.label}</span>
          <KindTag kind={s.kind} />
        </span>
      ))}
      {series.some((s) => s.learns.length) && (
        <span className="inline-flex items-center gap-2 text-muted">
          <svg width="12" height="12" aria-hidden><circle cx="6" cy="6" r="4" fill="var(--s-pw)" stroke="var(--surface)" strokeWidth="2" /></svg>
          a new layout learned (the only postings that cost AI calls)
        </span>
      )}
    </div>
  );
}

export function AiCallsChart({ series, height = 280, big = false, label }: { series: AiSeries[]; height?: number; big?: boolean; label: string }) {
  const [wrap, width] = useWidth(640);
  const narrow = width < 520;
  const fs = big ? 15 : 11;
  const M = { l: big ? 58 : 44, r: narrow ? 12 : big ? 210 : 150, t: big ? 34 : 26, b: big ? 46 : 36 };
  const iw = Math.max(80, width - M.l - M.r);
  const ih = height - M.t - M.b;
  const xMax = Math.max(1, ...series.map((s) => s.points.at(-1)?.x ?? 0));
  const yTicks = niceTicks(Math.max(1, ...series.map((s) => s.points.at(-1)?.y ?? 0)), 4);
  const yMax = yTicks.at(-1)!;
  const xTicks = niceTicks(xMax, narrow ? 4 : 6).filter((t) => t <= xMax);
  const sx = (x: number) => M.l + (x / xMax) * iw;
  const sy = (y: number) => M.t + ih - (y / yMax) * ih;
  const path = (s: AiSeries) => s.points.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join("");

  // end labels, nudged apart
  let prev = -Infinity;
  const ends = series
    .map((s) => ({ s, y: sy(s.points.at(-1)!.y) }))
    .sort((a, b) => a.y - b.y)
    .map((e) => {
      const ly = Math.max(e.y, prev + (big ? 44 : 32));
      prev = ly;
      return { ...e, ly };
    });

  const [hx, setHx] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const onMove = (e: PointerEvent<SVGRectElement>) => {
    const box = svgRef.current?.getBoundingClientRect();
    if (!box) return;
    setHx(Math.min(xMax, Math.max(0, Math.round(((e.clientX - box.left - M.l) / iw) * xMax))));
  };
  const onKey = (e: KeyboardEvent<SVGSVGElement>) => {
    const step = e.shiftKey ? Math.max(1, Math.round(xMax / 10)) : 1;
    if (e.key === "ArrowRight") setHx((x) => Math.min(xMax, (x ?? 0) + step));
    else if (e.key === "ArrowLeft") setHx((x) => Math.max(0, (x ?? 0) - step));
    else if (e.key === "Escape") setHx(null);
    else return;
    e.preventDefault();
    e.stopPropagation();
  };
  const at = (s: AiSeries, x: number) => s.points.find((p) => p.x === x)?.y ?? null;
  const learnAt = hx == null ? null : series.find((s) => s.id === "patchwork")?.learns.find((l) => l.x === hx);

  return (
    <figure className="m-0 grid gap-2 min-w-0">
      <figcaption className={`${big ? "text-[20px]" : "text-sm"} font-semibold`}>{label}</figcaption>
      <div ref={wrap} className="relative min-w-0">
        <svg
          ref={svgRef}
          width={width}
          height={height}
          role="img"
          tabIndex={0}
          onKeyDown={onKey}
          onBlur={() => setHx(null)}
          aria-label={`${label}. ${series.map((s) => `${s.label} (${s.kind}): ${s.points.at(-1)!.y} AI calls after ${xMax} postings`).join("; ")}. Use left and right arrows to read each posting.`}
          style={{ display: "block", overflow: "visible", font: `${fs}px var(--mono)` }}
        >
          {yTicks.map((t) => (
            <g key={t}>
              <line x1={M.l} x2={M.l + iw} y1={sy(t)} y2={sy(t)} stroke={t === 0 ? "var(--line)" : "var(--grid)"} strokeWidth="1" />
              <text x={M.l - 8} y={sy(t)} dy="0.32em" textAnchor="end" fill="var(--muted)" className="num">{t}</text>
            </g>
          ))}
          {xTicks.map((t) => (
            <text key={t} x={sx(t)} y={M.t + ih + fs + 6} textAnchor="middle" fill="var(--muted)" className="num">{t}</text>
          ))}
          <text x={M.l + iw / 2} y={height - 4} textAnchor="middle" fill="var(--muted)">postings, in the order they arrived</text>
          <text x={M.l - (big ? 46 : 36)} y={M.t - (big ? 20 : 14)} fill="var(--muted)">AI calls</text>

          {series.map((s) => (
            <path key={s.id} d={path(s)} fill="none" stroke={COLOR[s.id]} strokeWidth={big ? 3 : 2} strokeDasharray={DASH[s.style]} strokeLinejoin="round" strokeLinecap="round" />
          ))}
          {series.flatMap((s) =>
            s.learns.map((l) => (
              <circle key={`${s.id}-${l.x}`} cx={sx(l.x)} cy={sy(l.y)} r={big ? 6 : 4.5} fill={COLOR[s.id]} stroke="var(--surface)" strokeWidth="2" />
            )),
          )}

          {!narrow && ends.map(({ s, y, ly }) => (
            <g key={s.id}>
              {Math.abs(ly - y) > 2 && <line x1={M.l + iw + 4} y1={y} x2={M.l + iw + 12} y2={ly} stroke="var(--muted)" strokeWidth="1" />}
              <text x={M.l + iw + 14} y={ly} dy="0.32em" fill="var(--ink)" style={{ font: `600 ${big ? 17 : 12.5}px var(--body)` }}>
                {s.label}: {approx(s.kind)}{s.points.at(-1)!.y}
              </text>
              <text x={M.l + iw + 14} y={ly + (big ? 19 : 14)} dy="0.32em" fill="var(--muted)" style={{ font: `${big ? 14 : 11}px var(--mono)` }}>{kindLabel(s.kind)}</text>
            </g>
          ))}

          {hx != null && (
            <g pointerEvents="none">
              <line x1={sx(hx)} x2={sx(hx)} y1={M.t} y2={M.t + ih} stroke="var(--muted)" strokeWidth="1" />
              {series.map((s) => {
                const y = at(s, hx);
                return y == null ? null : <circle key={s.id} cx={sx(hx)} cy={sy(y)} r="4.5" fill={COLOR[s.id]} stroke="var(--surface)" strokeWidth="2" />;
              })}
            </g>
          )}
          <rect x={M.l} y={M.t} width={iw} height={ih} fill="transparent" onPointerMove={onMove} onPointerLeave={() => setHx(null)} />
        </svg>
        {hx != null && (
          <div
            className="absolute pointer-events-none card px-3 py-2 text-[12.5px] grid gap-0.5 shadow-md"
            style={{ left: Math.min(sx(hx) + 12, Math.max(0, width - 250)), top: M.t, minWidth: 200 }}
          >
            <b>after posting {hx}</b>
            {series.map((s) => (
              <span key={s.id} className="num">
                <span style={{ color: COLOR[s.id] }} aria-hidden>━ </span>{s.label}: {approx(s.kind)}{at(s, hx) ?? "—"} AI calls <span className="text-muted">({kindLabel(s.kind)})</span>
              </span>
            ))}
            {learnAt && <span className="text-muted">learned {learnAt.site}: {learnAt.calls} calls</span>}
          </div>
        )}
      </div>
    </figure>
  );
}

/** The same numbers as a table (every posting), for screen readers and anyone checking the chart. */
export function AiCallsTable({ series, postings }: { series: AiSeries[]; postings: { n: number; site: string; label: string | null; ai_calls: number; correct: boolean | null }[] }) {
  const pw = series.find((s) => s.id === "patchwork");
  const ba = series.find((s) => s.id === "browser_agent");
  return (
    <details>
      <summary className="cursor-pointer text-[13px] font-semibold">Show every posting as a table</summary>
      <div className="overflow-auto mt-2" style={{ maxHeight: 320 }}>
        <table className="tbl">
          <thead>
            <tr><th>#</th><th>Site</th><th>Result</th><th className="text-right">AI calls</th><th className="text-right">Patchwork total</th>{ba && <th className="text-right">Browser agent total ({kindLabel(ba.kind)})</th>}<th>Correct</th></tr>
          </thead>
          <tbody>
            {postings.map((p) => (
              <tr key={p.n}>
                <td className="num">{p.n}</td>
                <td className="font-mono text-[12.5px]">{p.site}</td>
                <td className="text-[12.5px]">{p.label === "learned_now" ? "learned now" : p.label ?? "—"}</td>
                <td className="text-right num">{p.ai_calls}</td>
                <td className="text-right num">{pw?.points.find((x) => x.x === p.n)?.y ?? "—"}</td>
                {ba && <td className="text-right num text-muted">{approx(ba.kind)}{ba.points.find((x) => x.x === p.n)?.y ?? "—"}</td>}
                <td>{p.correct == null ? "—" : p.correct ? "✓" : <span style={{ color: "var(--bad)" }}>✗</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
