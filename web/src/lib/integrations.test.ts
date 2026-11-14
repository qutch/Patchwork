import { describe, expect, it } from "vitest";
import type { ExtensionInfo, IntegrationSummary } from "../api";
import {
  defaultIntegration, destinationLabel, destinationWhere, eyebrowFor, humanName, JOBS_WORDING, missingCredentials,
  pageIntegrations, pickIntegration, sourceText, specInfo, wordingFor,
} from "./integrations";

const sum = (name: string, over: Partial<IntegrationSummary> = {}): IntegrationSummary => ({ name, active_version: 1, latest_version: 1, open_incidents: 0, ...over });
const ext = (over: Partial<ExtensionInfo> = {}): ExtensionInfo => ({ token: "t", control_url: "http://x", ...over });

describe("pageIntegrations", () => {
  it("uses /api/extension's list (V15) and enriches it from /api/integrations", () => {
    const list = pageIntegrations(
      ext({ integrations: [{ name: "jobs_to_tracker", intent: "Jobs" }, { name: "track_product_prices" }] }),
      [sum("po_to_stockroom"), sum("track_product_prices", { intent: "Track product prices", destination: { kind: "airtable" } })],
    );
    expect(list.map((i) => i.name)).toEqual(["jobs_to_tracker", "track_product_prices"]);
    expect(list[1].intent).toBe("Track product prices");
    expect(list[1].destination?.kind).toBe("airtable");
  });
  it("falls back to every integration but v1's, jobs first, when the extension has no list (older backends)", () => {
    expect(pageIntegrations(ext(), [sum("po_to_stockroom"), sum("papers"), sum("jobs_to_tracker")]).map((i) => i.name)).toEqual(["jobs_to_tracker", "papers"]);
    expect(pageIntegrations(ext({ integrations: [] }), [sum("jobs_to_tracker")]).map((i) => i.name)).toEqual(["jobs_to_tracker"]);
  });
  it("is never empty", () => {
    expect(pageIntegrations(null, null).map((i) => i.name)).toEqual(["jobs_to_tracker"]);
    expect(pageIntegrations(ext(), [sum("po_to_stockroom")]).map((i) => i.name)).toEqual(["jobs_to_tracker"]);
  });
});

describe("choosing an integration", () => {
  const list = [{ name: "jobs_to_tracker" }, { name: "track_product_prices" }];
  it("defaults to default_integration, else jobs_to_tracker, else the first", () => {
    expect(defaultIntegration(ext({ default_integration: "track_product_prices" }), list)).toBe("track_product_prices");
    expect(defaultIntegration(ext({ default_integration: "gone" }), list)).toBe("jobs_to_tracker");
    expect(defaultIntegration(null, [{ name: "papers" }])).toBe("papers");
  });
  it("keeps a remembered choice only while it exists", () => {
    expect(pickIntegration("track_product_prices", list, "jobs_to_tracker")).toBe("track_product_prices");
    expect(pickIntegration("deleted_one", list, "jobs_to_tracker")).toBe("jobs_to_tracker");
    expect(pickIntegration(null, list, "jobs_to_tracker")).toBe("jobs_to_tracker");
  });
  it("names integrations for people", () => {
    expect(humanName("track_product_prices")).toBe("Track product prices");
  });
});

describe("specInfo", () => {
  it("reads a Google Form sink", () => {
    const s = specInfo(`spec_version: 1
name: jobs_to_tracker
intent: Add the job posting I'm looking at to my application tracker
sink:
  kind: form
  url: https://docs.google.com/forms/d/e/abc/viewform
  fields:
    Email: {const: demo@example.com}
    Job Title: {from: title}
`);
    expect(s.intent).toMatch(/job posting/);
    expect(s.destination).toMatchObject({ kind: "form", url: "https://docs.google.com/forms/d/e/abc/viewform", credential: null });
    expect(s.sinkFields.map((f) => f.label)).toEqual(["Email", "Job Title"]);
    expect(destinationLabel(s.destination)).toBe("Google Form");
    expect(destinationWhere(s.destination)).toBe("docs.google.com");
  });
  it("reads an Airtable sink and its credential name (never a value)", () => {
    const s = specInfo(`intent: Save papers
sink:
  kind: airtable
  base_id: appXXXXXXXXXXXXXX
  table: Applications
  auth: {scheme: bearer, credential: AIRTABLE_TOKEN}
  fields:
    Title: {from: title}
`);
    expect(s.destination).toMatchObject({ kind: "airtable", base_id: "appXXXXXXXXXXXXXX", table: "Applications", credential: "AIRTABLE_TOKEN" });
    expect(destinationLabel(s.destination)).toBe("Airtable");
    expect(destinationWhere(s.destination)).toBe("table Applications · base appXXXXXXXXXXXXXX");
  });
  it("reads an API sink's body and a plain web form", () => {
    const api = specInfo("sink:\n  kind: api\n  base_url: http://127.0.0.1:8002\n  body: {sku: {from: sku}}\n");
    expect(destinationLabel(api.destination)).toBe("API");
    expect(destinationWhere(api.destination)).toBe("127.0.0.1:8002");
    expect(api.sinkFields).toEqual([{ label: "sku", source: { from: "sku" } }]);
    expect(destinationLabel(specInfo("sink: {kind: form, url: 'http://127.0.0.1:18010/', fields: {A: {from: a}}}").destination)).toBe("web form");
  });
  it("returns nulls for missing or broken specs", () => {
    expect(specInfo(null)).toEqual({ intent: null, destination: null, sinkFields: [] });
    expect(specInfo("sink: [unclosed")).toEqual({ intent: null, destination: null, sinkFields: [] });
    expect(specInfo("intent: x\n").destination).toBeNull();
    expect(destinationLabel(null)).toBe("destination");
    expect(destinationLabel({ kind: "sql" })).toBe("sql");
  });
});

describe("eyebrowFor", () => {
  it("keeps the jobs demo's eyebrow on its form", () => {
    expect(eyebrowFor("jobs_to_tracker", "whatever", null)).toBe("Job postings → tracker form");
    expect(eyebrowFor("jobs_to_tracker", null, { kind: "form", url: "https://docs.google.com/forms/x" })).toBe("Job postings → tracker form");
    expect(eyebrowFor("jobs_to_tracker", null, { kind: "airtable" })).toBe("Job postings → Airtable");
  });
  it("uses the intent and destination kind for other integrations", () => {
    expect(eyebrowFor("track_product_prices", "Track product prices", { kind: "form", url: "https://forms.example.com/x" })).toBe("Track product prices → web form");
    expect(eyebrowFor("papers", null, { kind: "airtable" })).toBe("Papers → Airtable");
    expect(eyebrowFor("papers", "", null)).toBe("Papers → destination");
  });
});

describe("missingCredentials", () => {
  const creds = [
    { name: "AIRTABLE_TOKEN", set: false, used_by: ["papers"] },
    { name: "STOCK_API_KEY", set: true, used_by: ["po_to_stockroom"] },
    { name: "OTHER", set: false, used_by: ["someone_else"] },
  ];
  it("lists the unset credentials an integration uses", () => {
    expect(missingCredentials(creds, "papers")).toEqual(["AIRTABLE_TOKEN"]);
    expect(missingCredentials(creds, "po_to_stockroom")).toEqual([]);
    expect(missingCredentials(creds, "jobs_to_tracker")).toEqual([]);
  });
  it("also matches the sink's credential by name, and tolerates a backend without /api/credentials", () => {
    expect(missingCredentials(creds, "new_one", { kind: "airtable", credential: "AIRTABLE_TOKEN" })).toEqual(["AIRTABLE_TOKEN"]);
    expect(missingCredentials(null, "papers")).toEqual([]);
  });
});

describe("wordingFor", () => {
  it("is exactly today's wording for the jobs demo on a form", () => {
    expect(wordingFor("jobs_to_tracker", null)).toBe(JOBS_WORDING);
    expect(wordingFor("jobs_to_tracker", { kind: "form", url: "https://docs.google.com/forms/x" })).toBe(JOBS_WORDING);
  });
  it("says page and names the destination elsewhere", () => {
    const w = wordingFor("track_product_prices", { kind: "airtable" });
    expect(w.noun).toBe("page");
    expect(w.item).toBe("Page");
    expect(w.writing).toBe("writing to Airtable…");
    expect(w.changed).toBe("table changed");
    expect(w.duplicate).toBe("already sent");
    expect(w.changedHint).toMatch(/^The Airtable table changed/);
    expect(JSON.stringify(w)).not.toMatch(/job|posting|title or company|Sheet/i);
    const jobsOnAirtable = wordingFor("jobs_to_tracker", { kind: "airtable" });
    expect(jobsOnAirtable.noun).toBe("job posting");
    expect(jobsOnAirtable.writing).toBe("writing to Airtable…");
  });
  it("words sink sources", () => {
    expect(sourceText({ from: "price" })).toBe("price read from the page");
    expect(sourceText({ from: "record.canonical_url" })).toBe("the page's canonical URL");
    expect(sourceText({ from: "record.captured_date" })).toBe("the day you pressed the hotkey");
    expect(sourceText({ const: "x" })).toBe("always the same value");
  });
});
