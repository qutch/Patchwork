import { useEffect, useState } from "react";
import { AppProvider, useApp } from "./state";
import { AiToggle } from "./components/AiToggle";
import Presenter from "./components/Presenter";
import RecordTab from "./tabs/Record";
import BenchmarkTab from "./tabs/Benchmark";
import Home from "./pages/Home";
import IntegrationPage from "./pages/IntegrationPage";
import NewIntegration from "./pages/NewIntegration";
import CredentialsDrawer from "./components/CredentialsDrawer";

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
  const { route, navigate, setCredsOpen, settings, threads, incidents, apiDown } = useApp();
  const [theme, nextTheme] = useTheme();
  const waiting = threads.some((t) => t.state === "waiting");
  const link = (active: boolean) => ({
    className: "relative px-3 py-2 rounded-md text-[15px] font-medium no-underline",
    style: { color: active ? "var(--accent)" : "var(--ink)", background: active ? "var(--accent-soft)" : "transparent" },
    "aria-current": active ? ("page" as const) : undefined,
  });
  return (
    <header className="border-b border-line bg-surface sticky top-0 z-20">
      <div className="max-w-[1320px] mx-auto px-4 py-3 flex flex-wrap items-center gap-x-4 gap-y-3">
        <div className="flex items-baseline gap-2">
          <span className="display text-[22px] font-extrabold tracking-tight">Patchwork</span>
          <span className="font-mono text-xs text-muted hidden 2xl:inline">integration console</span>
        </div>
        <nav className="flex flex-wrap gap-1 order-3 lg:order-none w-full lg:w-auto" aria-label="Sections">
          <a href="#/" onClick={(e) => { e.preventDefault(); navigate({ page: "home" }); }} {...link(route.page === "home" || route.page === "integration")}>
            Integrations
            {(waiting || incidents.length > 0) && (
              <span className="ml-2 inline-block w-2.5 h-2.5 rounded-full align-middle pulse" style={{ background: incidents.length ? "var(--bad)" : "var(--gate)" }} title="Something needs you" />
            )}
          </a>
          <a href="#/new" onClick={(e) => { e.preventDefault(); navigate({ page: "new" }); }} {...link(route.page === "new")}>
            New integration
          </a>
        </nav>
        <div className="ml-auto flex items-center gap-3">
          {settings && (
            <span className="font-mono text-xs text-muted hidden 2xl:inline" title="Agent implementation and model">
              agent: {settings.agent}
            </span>
          )}
          {apiDown && <span className="pill" style={{ color: "var(--bad)", background: "var(--bad-soft)" }}>API unreachable</span>}
          <AiToggle />
          <button className="btn" onClick={() => setCredsOpen(true)}>Credentials</button>
          <details className="relative">
            <summary className="btn list-none cursor-pointer" aria-label="More">More</summary>
            <div className="absolute right-0 mt-1 z-30 card card-b grid gap-1 min-w-[160px]">
              <a href="#/bench" onClick={(e) => { e.preventDefault(); navigate({ page: "bench" }); (e.currentTarget.closest("details") as HTMLDetailsElement | null)?.removeAttribute("open"); }} {...link(route.page === "bench")}>
                Benchmark
              </a>
            </div>
          </details>
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
  const { route } = useApp();
  return (
    <main className="max-w-[1320px] mx-auto px-4 py-6 grid gap-6">
      {route.page === "home" && <Home />}
      {route.page === "new" && (route.mode === "show" ? <RecordTab /> : <NewIntegration />)}
      {route.page === "integration" && <IntegrationPage name={route.name} section={route.section} />}
      {route.page === "bench" && <BenchmarkTab />}
    </main>
  );
}

const PRESENT = typeof location !== "undefined" && new URLSearchParams(location.search).get("present") === "1";

export default function App() {
  return (
    <AppProvider>
      {PRESENT ? <Presenter /> : (
        <>
          <Header />
          <Body />
          <CredentialsDrawer />
        </>
      )}
      <Notice />
    </AppProvider>
  );
}
