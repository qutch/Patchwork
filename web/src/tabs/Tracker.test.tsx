import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import type { Settings, TrackerRow } from "../api";
import { wordingFor } from "../lib/integrations";
import { TrackerTable } from "./Tracker";

const row = (over: Partial<TrackerRow> = {}): TrackerRow => ({
  page_id: "pg-000001", seq: 1, captured_at: "2026-11-14T14:02:11+00:00", site: "jobs.lever.co", url: "https://jobs.lever.co/x/1",
  title: "American Tech Fellowship", company: "Palantir Technologies", status: "success", label: "code", reason: null, detail: null,
  parser: "lever", version: 3, routed_by: "match", parser_origin: { kind: "taught", at: null, thread_id: null, approved_by: "human" },
  ai_calls: 0, run_id: 7, learn_thread_id: null, incident_id: null, ...over,
});
const settings: Settings = { ai_enabled: true, agent: "scripted", model: "scripted", learn_policy: "auto" };
const headers = (c: HTMLElement) => [...c.querySelectorAll("thead th")].map((th) => th.textContent);

describe("TrackerTable", () => {
  it("shows the jobs demo as before: one Posting cell with title, company and site", () => {
    const { container } = render(<TrackerTable rows={[row()]} settings={settings} />);
    expect(headers(container)).toEqual(["Time", "Posting", "Read by", "Result", "AI calls"]);
    expect(screen.getByText("American Tech Fellowship")).toBeTruthy();
    expect(screen.getByText(/Palantir Technologies/)).toBeTruthy();
    expect(screen.getByText("code · 0 AI calls")).toBeTruthy();
  });

  it("renders one column per field for other integrations", () => {
    const rows = [
      row({ page_id: "pg-2", site: "amazon.com", title: "Kindle : Amazon.com", company: null, fields: { product: "Kindle Paperwhite", price: 149.99, store: "Amazon" } }),
      row({ page_id: "pg-3", seq: 2, site: "walmart.com", title: "Ninja Air Fryer - Walmart.com", company: null, fields: {}, status: "held", label: "held", reason: "PARSE_NO_MATCH", parser: null }),
    ];
    const w = wordingFor("track_product_prices", { kind: "form", url: "https://forms.example.com/x" });
    const { container } = render(<TrackerTable rows={rows} settings={settings} columns={["product", "price", "store"]} w={w} />);
    expect(headers(container)).toEqual(["Time", "Product", "Price", "Store", "Read by", "Result", "AI calls"]);
    const [r1, r2] = container.querySelectorAll("tbody tr");
    expect(within(r1 as HTMLElement).getByText("Kindle Paperwhite")).toBeTruthy();
    expect(within(r1 as HTMLElement).getByText("149.99")).toBeTruthy();
    expect(within(r1 as HTMLElement).getByText("amazon.com")).toBeTruthy();
    // not read yet: the page title, marked as such
    expect(within(r2 as HTMLElement).getByText(/Ninja Air Fryer/).textContent).toMatch(/page title, not read yet/);
    expect(within(r2 as HTMLElement).getByText("held: unknown layout")).toBeTruthy();
  });

  it("keeps big (presenter) tables to three fields and words the empty state per integration", () => {
    const { container } = render(<TrackerTable rows={[row({ fields: { a: 1, b: 2, c: 3, d: 4 } })]} settings={settings} big columns={["a", "b", "c", "d"]} />);
    expect(headers(container)).toEqual(["Time", "A", "B", "C", "Result", "AI calls"]);
    const w = wordingFor("papers", { kind: "airtable" });
    render(<TrackerTable rows={[]} settings={settings} w={w} />);
    expect(screen.getByText(/on a page with the Patchwork extension/)).toBeTruthy();
  });
});
