import { useApp } from "../state";

export function AiToggle({ big = false }: { big?: boolean }) {
  const { settings, setAi } = useApp();
  const on = !!settings?.ai_enabled;
  return (
    <button
      role="switch"
      aria-checked={on}
      aria-label="AI on or off"
      disabled={!settings}
      onClick={() => setAi(!on)}
      className={`inline-flex items-center gap-3 rounded-md border-2 font-mono font-semibold ${big ? "px-4 py-2.5 text-[16px]" : "px-3 py-1.5 text-[14px]"}`}
      style={{
        borderColor: on ? "var(--accent)" : "var(--ink)",
        background: on ? "var(--accent)" : "var(--surface)",
        color: on ? "var(--surface)" : "var(--ink)",
      }}
      title={on ? "Agents may call the model to build and repair. Runs never do." : "No model calls anywhere. Runs work exactly the same."}
    >
      <span
        className="relative inline-block rounded-full"
        style={{ width: 34, height: 18, background: on ? "rgba(255,255,255,.35)" : "var(--sunken)", border: `1px solid ${on ? "transparent" : "var(--line)"}` }}
        aria-hidden
      >
        <span
          className="absolute top-[2px] rounded-full transition-all"
          style={{ width: 12, height: 12, left: on ? 18 : 3, background: on ? "var(--surface)" : "var(--muted)" }}
        />
      </span>
      AI {on ? "ON" : "OFF"}
    </button>
  );
}
