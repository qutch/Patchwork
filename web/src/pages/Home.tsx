import type { IntegrationState, IntegrationSummary } from "../api";
import { destinationLabel, destinationWhere, humanName, threadBelongs } from "../lib/integrations";
import { toHash, type Route } from "../routes";
import { useApp } from "../state";
import { Empty, PageHead } from "../components/ui";

const STATE_LABEL: Record<IntegrationState, string> = { draft: "Draft", teaching: "Teaching", live: "Live" };
const STATE_STYLE: Record<IntegrationState, { fg: string; bg: string }> = {
  draft: { fg: "var(--muted)", bg: "var(--sunken)" },
  teaching: { fg: "var(--accent)", bg: "var(--accent-soft)" },
  live: { fg: "var(--ok)", bg: "var(--ok-soft)" },
};

export function StateBadge({ state }: { state: IntegrationState }) {
  const s = STATE_STYLE[state];
  return <span className="pill" style={{ color: s.fg, background: s.bg }}>{STATE_LABEL[state]}</span>;
}

/** Where a card goes: a draft resumes its wizard, anything else opens its page. */
export function routeFor(i: Pick<IntegrationSummary, "name" | "state">): Route {
  return i.state === "draft" || i.state === "teaching"
    ? { page: "new", draft: i.name }
    : { page: "integration", name: i.name, section: "rows" };
}

/** One card per integration: what it does, where it writes, and whether anything needs a person. */
export default function Home() {
  const { integrations, pageIntegrations, incidents, threads, navigate, setCredsOpen } = useApp();
  return (
    <>
      <PageHead
        eyebrow="Integrations"
        title="Your integrations"
        right={
          <>
            <button className="btn" onClick={() => setCredsOpen(true)}>Credentials</button>
            <button className="btn btn-primary" onClick={() => navigate({ page: "new" })}>New integration</button>
          </>
        }
      >
        Each one reads something, checks it, and writes it somewhere. Open one to see its rows, what needs you, and how it was built.
      </PageHead>

      {integrations.length === 0 ? (
        <Empty action={<button className="btn btn-primary" onClick={() => navigate({ page: "new" })}>New integration</button>}>
          Nothing here yet. Describe what you want to move and where it should go, and Patchwork builds it from a few examples.
        </Empty>
      ) : (
        <ul className="m-0 p-0 list-none grid gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-label="Integrations">
          {integrations.map((i) => {
            const state: IntegrationState = i.state ?? "live";
            const listed = pageIntegrations.find((p) => p.name === i.name);
            const d = i.destination ?? i.sink ?? listed?.destination ?? listed?.sink ?? null;
            const intent = i.intent ?? listed?.intent ?? null;
            const open = incidents.filter((x) => x.integration === i.name && x.status === "open").length;
            const waiting = threads.filter((t) => t.state === "waiting" && threadBelongs(t, i.name)).length;
            const needs = open + waiting;
            const href = toHash(routeFor(i));
            return (
              <li key={i.name} data-integration={i.name}>
                <a
                  href={href}
                  onClick={(e) => {
                    e.preventDefault();
                    navigate(routeFor(i));
                  }}
                  className="card card-b grid gap-2 content-start no-underline h-full"
                  style={{ color: "var(--ink)" }}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs text-muted truncate">{i.name}</span>
                    <StateBadge state={state} />
                  </span>
                  <span className="text-[18px] font-bold leading-tight">{intent?.trim() || humanName(i.name)}</span>
                  <span className="text-[13px] text-muted">
                    {d ? <>Writes to <b className="text-ink">{destinationLabel(d)}</b>{destinationWhere(d) ? ` · ${destinationWhere(d)}` : ""}</> : state === "live" ? "Writes to its destination" : "Destination not chosen yet"}
                  </span>
                  <span className="text-[13px] flex flex-wrap gap-2 items-center">
                    {state === "live" && i.active_version != null && <span className="font-mono text-muted">v{i.active_version}</span>}
                    {needs > 0 ? (
                      <span className="pill" style={{ color: "var(--bad)", background: "var(--bad-soft)" }}>{needs} need{needs === 1 ? "s" : ""} you</span>
                    ) : state === "live" ? (
                      <span className="text-muted">nothing needs you</span>
                    ) : (
                      <span className="text-muted">Continue setup →</span>
                    )}
                  </span>
                </a>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
