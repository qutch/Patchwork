import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import App from "../App";
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

const T = { timeout: 4000 };

describe("App routing", () => {
  it("shows each section of a page integration", async () => {
    for (const [section, text] of [
      ["rows", "Pages sent with the hotkey"],
      ["needs", "Waiting for a person"],
      ["layouts", "Learned layouts"],
      ["versions", "Integration"],
      ["activity", "Agent Activity"],
      ["setup", "Extension setup"],
    ] as const) {
      location.hash = `#/integrations/track_product_prices/${section}`;
      const { unmount } = render(<App />);
      expect((await screen.findAllByText(text, { exact: false }, T)).length).toBeGreaterThan(0);
      unmount();
    }
  });

  it("redirects an old tab hash and rewrites the address", async () => {
    location.hash = "#tracker";
    render(<App />);
    await screen.findByText("Pages sent with the hotkey", undefined, T);
    expect(location.hash).toBe("#/integrations/jobs_to_tracker/rows");
  });

  it("rewrites an old hash typed mid-session too", async () => {
    location.hash = "#/";
    render(<App />);
    await screen.findByText("Your integrations", undefined, T);
    location.hash = "#tracker";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await screen.findByText("Pages sent with the hotkey", undefined, T);
    expect(location.hash).toBe("#/integrations/jobs_to_tracker/rows");
  });

  it("#record still shows the Record view under New integration", async () => {
    location.hash = "#record";
    render(<App />);
    await screen.findByText(/Start a recording/, undefined, T);
    expect(location.hash).toBe("#/new?mode=show");
  });

  it("the header and a non-job integration's page carry no job words", async () => {
    location.hash = "#/integrations/papers_to_reading_list/setup";
    const { container } = render(<App />);
    await screen.findByText("Extension setup", undefined, T);
    expect(container.querySelector("header")!.textContent).not.toMatch(JOB_WORDS);
  });
});
