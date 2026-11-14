import { useMemo, useState, type ReactNode } from "react";
import { diffStats, parseUnifiedDiff } from "../lib/derive";

// Outcome colours from the walkthrough: success green, duplicate grey, VALIDATION_FAILED amber,
// NOT_FOUND orange-red, PARSE_NO_MATCH purple, contract/other errors red.
export const OUTCOME_STYLE: Record<string, { fg: string; bg: string; label: string }> = {
  success: { fg: "var(--ok)", bg: "var(--ok-soft)", label: "success" },
  duplicate: { fg: "var(--dup)", bg: "var(--dup-soft)", label: "duplicate" },
  quarantined: { fg: "var(--bad)", bg: "var(--bad-soft)", label: "quarantined" },
  VALIDATION_FAILED: { fg: "var(--val)", bg: "var(--val-soft)", label: "VALIDATION_FAILED" },
  NOT_FOUND: { fg: "var(--nf)", bg: "var(--nf-soft)", label: "NOT_FOUND" },
  PARSE_NO_MATCH: { fg: "var(--pnm)", bg: "var(--pnm-soft)", label: "PARSE_NO_MATCH" },
};
export const outcomeStyle = (k: string) => OUTCOME_STYLE[k] ?? { fg: "var(--bad)", bg: "var(--bad-soft)", label: k };

export function Pill({ k, children, title }: { k: string; children?: ReactNode; title?: string }) {
  const s = outcomeStyle(k);
  return (
    <span className="pill" style={{ color: s.fg, background: s.bg }} title={title}>
      {children ?? s.label}
    </span>
  );
}

export function TestsPill({ passed, summary }: { passed: boolean | number | null | undefined; summary?: string | null }) {
  const ok = !!passed;
  const short = (summary ?? "").split("\n").filter(Boolean).at(-1) ?? "";
  return (
    <span className="pill" style={{ color: ok ? "var(--ok)" : "var(--bad)", background: ok ? "var(--ok-soft)" : "var(--bad-soft)" }} title={summary ?? ""}>
      {ok ? "✓ tests" : "✗ tests"} {short && <span className="font-normal">· {short}</span>}
    </span>
  );
}

export function GateMark({ children = "Human gate" }: { children?: ReactNode }) {
  return (
    <span className="pill" style={{ background: "var(--gate)", color: "var(--gate-ink)" }}>
      ◆ {children}
    </span>
  );
}

export function Card({ title, eyebrow, right, children, className = "", bodyClass = "card-b" }: {
  title?: ReactNode; eyebrow?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; bodyClass?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || right || eyebrow) && (
        <header className="card-h">
          <div className="min-w-0">
            {eyebrow && <div className="eyebrow mb-1">{eyebrow}</div>}
            {title && <h2 className="text-[16px] font-bold leading-tight">{title}</h2>}
          </div>
          {right && <div className="flex items-center gap-2 flex-wrap">{right}</div>}
        </header>
      )}
      <div className={bodyClass}>{children}</div>
    </section>
  );
}

export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="border border-dashed border-line rounded-md p-6 text-center text-muted grid gap-3 justify-items-center">
      <div className="max-w-[60ch]">{children}</div>
      {action}
    </div>
  );
}

export function PageHead({ eyebrow, title, children, right }: { eyebrow: string; title: string; children?: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="grid gap-1.5 min-w-0">
        <div className="eyebrow">{eyebrow}</div>
        <h1 className="text-[28px] font-extrabold leading-none">{title}</h1>
        {children && <p className="text-muted max-w-[78ch] m-0">{children}</p>}
      </div>
      {right && <div className="flex gap-2 flex-wrap items-center">{right}</div>}
    </div>
  );
}

const DIFF_STYLE: Record<string, React.CSSProperties> = {
  add: { background: "var(--diff-add)", color: "var(--diff-add-ink)" },
  del: { background: "var(--diff-del)", color: "var(--diff-del-ink)" },
  hunk: { color: "var(--accent)", background: "var(--accent-soft)" },
  file: { color: "var(--ink)", fontWeight: 600 },
  meta: { color: "var(--muted)" },
  ctx: {},
};

/** Raw unified diff with +/- colouring. Non-diff text renders as plain code. */
export function Diff({ text, maxHeight = 460, empty = "No changes." }: { text: string; maxHeight?: number; empty?: string }) {
  const lines = useMemo(() => parseUnifiedDiff(text), [text]);
  if (!text) return <div className="text-muted text-sm p-3">{empty}</div>;
  return (
    <pre className="code" style={{ maxHeight, padding: "8px 0" }}>
      {lines.map((l, i) => (
        <div key={i} style={{ ...DIFF_STYLE[l.kind], padding: "0 14px", minHeight: "1.55em", whiteSpace: "pre" }}>
          {l.text}
        </div>
      ))}
    </pre>
  );
}

export function DiffStat({ text }: { text: string }) {
  const s = diffStats(text);
  return (
    <span className="font-mono text-xs">
      <span style={{ color: "var(--ok)" }}>+{s.add}</span> <span style={{ color: "var(--bad)" }}>−{s.del}</span>
    </span>
  );
}

export function CodeBlock({ text, maxHeight = 520, lineNumbers = true }: { text: string; maxHeight?: number; lineNumbers?: boolean }) {
  const lines = text.replace(/\n$/, "").split("\n");
  const w = String(lines.length).length;
  return (
    <pre className="code" style={{ maxHeight }}>
      {lines.map((l, i) => (
        <div key={i} style={{ whiteSpace: "pre", minHeight: "1.55em" }}>
          {lineNumbers && <span className="select-none text-muted inline-block text-right mr-4 opacity-70" style={{ width: `${w}ch` }}>{i + 1}</span>}
          {l}
        </div>
      ))}
    </pre>
  );
}

export function Tabs<T extends string>({ value, onChange, items }: { value: T; onChange: (v: T) => void; items: { id: T; label: ReactNode }[] }) {
  return (
    <div className="flex flex-wrap gap-1" role="tablist">
      {items.map((it) => (
        <button key={it.id} className="tab-btn" role="tab" aria-pressed={value === it.id} aria-selected={value === it.id} onClick={() => onChange(it.id)}>
          {it.label}
        </button>
      ))}
    </div>
  );
}

export function JsonInline({ value }: { value: unknown }) {
  if (value == null) return <span className="text-muted">—</span>;
  return <code className="font-mono text-[12.5px] break-all">{JSON.stringify(value)}</code>;
}

export function Spinner() {
  return <span className="inline-block w-2 h-2 rounded-full bg-accent pulse" aria-hidden />;
}

export function useToggle(init = false): [boolean, () => void] {
  const [v, setV] = useState(init);
  return [v, () => setV((x) => !x)];
}
