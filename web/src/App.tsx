import { useEffect, useState } from "react";
import { AppProvider, TABS, useApp } from "./state";
import { AiToggle } from "./components/AiToggle";
import RecordTab from "./tabs/Record";
import ReviewTab from "./tabs/Review";
import AgentTab from "./tabs/Agent";
import IntegrationTab from "./tabs/Integration";
import RunsTab from "./tabs/Runs";

type Theme = "system" | "light" | "dark";
function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    const a = document.documentElement.getAttribute("data-theme");
    return a === "light" || a === "dark" ? a : "system";
  });
  useEffect(() => {
    const el = document.documentElement;
    if (theme === "system") el.removeAttribute("data-theme");
    else el.setAttribute("data-theme", theme);
    try {
      if (theme === "system") localStorage.removeItem("pw-theme");
      else localStorage.setItem("pw-theme", theme);
    } catch {
      /* ignore */
    }
  }, [theme]);
  const next = () => setTheme((t) => (t === "system" ? "light" : t === "light" ? "dark" : "system"));
  return [theme, next];
}

function Header() {
  const { tab, go, settings, threads, incidents, apiDown } = useApp();
  const [theme, nextTheme] = useTheme();
  const waiting = threads.some((t) => t.state === "waiting");
  return (
    <header className="border-b border-line bg-surface sticky top-0 z-20">
      <div className="max-w-[1320px] mx-auto px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-3">
        <div className="flex items-baseline gap-2">
          <span className="display text-[22px] font-extrabold tracking-tight">Patchwork</span>
          <span className="font-mono text-xs text-muted hidden sm:inline">integration console</span>
        </div>
        <nav className="flex flex-wrap gap-1 order-3 lg:order-none w-full lg:w-auto" aria-label="Sections">
          {TABS.map((t, i) => {
            const active = tab === t.id;
            return (
              <a
                key={t.id}
                href={`#${t.id}`}
                onClick={(e) => {
                  e.preventDefault();
                  go(t.id);
                }}
                aria-current={active ? "page" : undefined}
                className="relative px-3 py-2 rounded-md text-[15px] font-medium no-underline"
                style={{ color: active ? "var(--accent)" : "var(--ink)", background: active ? "var(--accent-soft)" : "transparent" }}
              >
                <span className="font-mono text-xs text-muted mr-1.5">{i + 1}</span>
                {t.label}
                {t.id === "agent" && waiting && (
                  <span className="ml-2 inline-block w-2.5 h-2.5 rounded-full align-middle pulse" style={{ background: "var(--gate)" }} title="An agent is waiting for you" />
                )}
                {t.id === "runs" && incidents.length > 0 && (
                  <span className="ml-2 inline-block w-2.5 h-2.5 rounded-full align-middle" style={{ background: "var(--bad)" }} title="Open incident" />
                )}
              </a>
            );
          })}
        </nav>
        <div className="ml-auto flex items-center gap-3">
          {settings && (
            <span className="font-mono text-xs text-muted hidden md:inline" title="Agent implementation and model">
              agent: {settings.agent}
            </span>
          )}
          {apiDown && <span className="pill" style={{ color: "var(--bad)", background: "var(--bad-soft)" }}>API unreachable</span>}
          <AiToggle />
          <button className="btn" onClick={nextTheme} title="Theme: system → light → dark" aria-label={`Theme: ${theme}`}>
            {theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐"} <span className="font-mono text-xs">{theme}</span>
          </button>
        </div>
      </div>
    </header>
  );
}

function Notice() {
  const { notice } = useApp();
  if (!notice) return null;
  const err = notice.kind === "error";
  return (
    <div
      role={err ? "alert" : "status"}
      className="fixed bottom-4 left-1/2 -translate-x-1/2 z-50 rounded-md px-4 py-2.5 shadow-lg font-medium max-w-[90vw]"
      style={{ background: err ? "var(--bad)" : "var(--ink)", color: "var(--surface)" }}
    >
      {notice.text}
    </div>
  );
}

function Body() {
  const { tab } = useApp();
  return (
    <main className="max-w-[1320px] mx-auto px-4 py-6 grid gap-6">
      {tab === "record" && <RecordTab />}
      {tab === "review" && <ReviewTab />}
      {tab === "agent" && <AgentTab />}
      {tab === "integration" && <IntegrationTab />}
      {tab === "runs" && <RunsTab />}
    </main>
  );
}

export default function App() {
  return (
    <AppProvider>
      <Header />
      <Body />
      <Notice />
    </AppProvider>
  );
}
