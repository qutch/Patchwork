import { api } from "../api";
import { destinationLabel, humanName, threadBelongs } from "../lib/integrations";
import { SECTIONS, toHash, type Section } from "../routes";
import { useApp, usePoll } from "../state";
import { Card, Empty, PageHead } from "../components/ui";
import { GateCard } from "../components/gates";
import { IncidentBanner } from "../components/IncidentBanner";
import { IntegrationBar } from "../components/Destination";
import { LayoutsPanel } from "../components/Layouts";
import AgentTab from "../tabs/Agent";
import IntegrationTab from "../tabs/Integration";
import ReviewTab from "../tabs/Review";
import RunsTab from "../tabs/Runs";
import TrackerTab, { ExtensionCard, LabelChip, usePageView } from "../tabs/Tracker";
import { StateBadge } from "./Home";

/** Incidents, held records and open interrupts for one integration: everything waiting for a person. */
function Needs({ name, page }: { name: string; page: boolean }) {
  const { incidents, threads, settings } = useApp();
  const view = usePageView(name);
  const rows = usePoll(() => (page ? api.tracker({ integration: name, limit: 100 }) : Promise.resolve(null)), page ? 2000 : 0, [name, page]);
  const open = incidents.filter((i) => i.integration === name && i.status === "open");
  const held = (rows.data?.rows ?? []).filter((r) => r.status === "held");
  const waiting = threads.filter((t) => t.pending && threadBelongs(t, name));
  const nothing = !open.length && !held.length && !waiting.length;
  return (
    <>
      <PageHead eyebrow="Needs you" title="Waiting for a person">
        Anything Patchwork was not sure about is held here and nothing is written until you decide.
      </PageHead>
      {open.map((i) => <IncidentBanner key={i.id} id={i.id} />)}
      {waiting.map((t) => t.pending && <GateCard key={t.pending.interrupt_id} ev={t.pending} threadId={t.thread_id} />)}
      {held.length > 0 && (
        <Card eyebrow="Held records" title={`${held.length} held, nothing written`} bodyClass="">
          <ul className="m-0 p-0 list-none">
            {held.map((r) => (
              <li key={r.page_id} className="px-4 py-3 border-b border-line last:border-b-0 flex flex-wrap items-center gap-3">
                <LabelChip row={r} settings={settings} w={view.w} />
                <a className="break-all text-[13.5px]" href={r.url} target="_blank" rel="noreferrer">{r.title || r.url}</a>
                <span className="text-muted text-[13px]">{r.detail ?? r.reason}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
      {nothing && <Empty>Nothing needs you right now.</Empty>}
    </>
  );
}

function Setup({ name, page }: { name: string; page: boolean }) {
  const view = usePageView(name);
  if (!page) return <ReviewTab />;
  return (
    <>
      <PageHead eyebrow="Setup" title="How pages reach this integration">
        {view.intent ? `“${view.intent.trim()}”. ` : ""}Where it writes, and the hotkey that sends it a page.
      </PageHead>
      <ExtensionCard w={view.w} />
    </>
  );
}

/** One integration, in sections. Each section reuses an existing view, filtered to this integration. */
export default function IntegrationPage({ name, section }: { name: string; section: Section }) {
  const { integrations, isPageIntegration, navigate, incidents, threads } = useApp();
  const summary = integrations.find((i) => i.name === name);
  const state = summary?.state ?? "live";
  const page = isPageIntegration(name);
  const view = usePageView(name);
  const needsCount =
    incidents.filter((i) => i.integration === name && i.status === "open").length + threads.filter((t) => t.pending && threadBelongs(t, name)).length;
  const sections = SECTIONS.filter((s) => page || s.id !== "layouts");

  if (state === "draft" || state === "teaching") {
    return (
      <>
        <PageHead eyebrow="Integration" title={view.intent?.trim() || humanName(name)} right={<StateBadge state={state} />} />
        <Empty action={<button className="btn btn-primary" onClick={() => navigate({ page: "new", draft: name })}>Continue setup</button>}>
          This integration is not live yet. Pick up where you left off.
        </Empty>
      </>
    );
  }

  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <a href="#/" className="btn no-underline" onClick={(e) => { e.preventDefault(); navigate({ page: "home" }); }}>← Integrations</a>
        <span className="font-mono text-[15px]">{name}</span>
        <StateBadge state={state} />
        {view.destination && <span className="text-muted text-[13px]">→ {destinationLabel(view.destination)}</span>}
        {!page && <IntegrationBar name={name} d={view.destination} />}
      </div>
      <nav className="flex flex-wrap gap-1" aria-label="Integration sections">
        {sections.map((s) => {
          const active = s.id === section;
          const to = { page: "integration" as const, name, section: s.id };
          return (
            <a
              key={s.id}
              href={toHash(to)}
              onClick={(e) => { e.preventDefault(); navigate(to); }}
              aria-current={active ? "page" : undefined}
              className="px-3 py-2 rounded-md text-[15px] font-medium no-underline"
              style={{ color: active ? "var(--accent)" : "var(--ink)", background: active ? "var(--accent-soft)" : "transparent" }}
            >
              {s.label}
              {s.id === "needs" && needsCount > 0 && <span className="ml-2 inline-block w-2.5 h-2.5 rounded-full align-middle" style={{ background: "var(--bad)" }} title="Needs you" />}
            </a>
          );
        })}
      </nav>
      <div className="grid gap-6" key={`${name}/${section}`}>
        {section === "rows" && (page ? <TrackerTab /> : <RunsTab />)}
        {section === "needs" && <Needs name={name} page={page} />}
        {section === "layouts" && (page ? <LayoutsPanel integration={name} /> : <Empty>Layouts belong to integrations that read pages.</Empty>)}
        {section === "versions" && <IntegrationTab />}
        {section === "activity" && <AgentTab integration={name} />}
        {section === "setup" && <Setup name={name} page={page} />}
      </div>
    </>
  );
}
