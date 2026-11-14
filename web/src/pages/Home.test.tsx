import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach } from "vitest";
import { AppProvider } from "../state";
import Home from "./Home";
import { startMock, JOB_WORDS, type Mock } from "../test/harness";

let mock: Mock;
beforeAll(async () => {
  mock = await startMock();
});
afterAll(() => mock.stop());
afterEach(() => {
  cleanup();
  location.hash = "";
});

describe("Home", () => {
  it("shows one card per integration with Draft / Live badges", async () => {
    await mock.call("POST", "/api/integrations/new", { intent: "Track upcoming events" });
    render(<AppProvider><Home /></AppProvider>);
    await waitFor(() => expect(document.querySelector('[data-integration="track_product_prices"]')).toBeTruthy());
    const live = await screen.findAllByText("Live");
    expect(live.length).toBeGreaterThanOrEqual(3);
    const draft = document.querySelector('[data-integration="track_upcoming_events"]')!;
    expect(draft.textContent).toContain("Draft");
    const names = [...document.querySelectorAll("[data-integration]")].map((e) => e.getAttribute("data-integration"));
    expect(names).toEqual(expect.arrayContaining(["jobs_to_tracker", "track_product_prices", "papers_to_reading_list", "track_upcoming_events"]));
  });

  it("opens an integration's rows, and a draft's wizard", async () => {
    render(<AppProvider><Home /></AppProvider>);
    const card = await waitFor(() => {
      const el = document.querySelector('[data-integration="track_product_prices"] a');
      expect(el).toBeTruthy();
      return el!;
    });
    fireEvent.click(card);
    expect(location.hash).toBe("#/integrations/track_product_prices/rows");
    fireEvent.click(document.querySelector('[data-integration="track_upcoming_events"] a')!);
    expect(location.hash).toBe("#/new?draft=track_upcoming_events");
  });

  it("uses no job words outside the jobs integration's own card", async () => {
    render(<AppProvider><Home /></AppProvider>);
    await waitFor(() => expect(document.querySelector('[data-integration="track_product_prices"]')).toBeTruthy());
    const copy = [...document.body.children].map((c) => c.cloneNode(true) as HTMLElement);
    for (const c of copy) c.querySelectorAll('[data-integration="jobs_to_tracker"]').forEach((e) => e.remove());
    expect(copy.map((c) => c.textContent).join(" ")).not.toMatch(JOB_WORDS);
  });
});
