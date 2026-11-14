import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AppProvider } from "../state";
import NewIntegration from "./NewIntegration";
import CredentialsDrawer from "../components/CredentialsDrawer";
import { startMock, JOB_WORDS, type Mock } from "../test/harness";

let mock: Mock;
beforeAll(async () => {
  mock = await startMock();
});
afterAll(() => mock.stop());
afterEach(() => {
  cleanup();
  location.hash = "";
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
});

const mount = () => render(<AppProvider><NewIntegration /><CredentialsDrawer /></AppProvider>);
const UI_TIMEOUT = { timeout: 4000 };

async function sendPage(integration: string, url: string, title: string) {
  const ext = await mock.call<{ token: string }>("GET", "/api/extension");
  const r = await fetch(`${mock.base}/api/pages`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-patchwork-token": ext.token },
    body: JSON.stringify({ url, title, html: `<title>${title}</title>`, integration }),
  });
  expect(r.status).toBe(201);
}

describe("New integration wizard", () => {
  it("walks all four steps for a products draft, and approve sends exactly the edit", async () => {
    location.hash = "#/new";
    mount();

    // 1. What
    expect(screen.getByText("One page is one record.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("What should it do"), { target: { value: "Track products I like" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByText("Where should it write?", undefined, UI_TIMEOUT);
    expect(location.hash).toBe("#/new?draft=track_products_i_like");

    // 2. Where: the credential is not set yet, so the check says what to do
    fireEvent.change(screen.getByLabelText("Base"), { target: { value: "https://airtable.com/appTEST00000000001/shrX" } });
    fireEvent.change(screen.getByLabelText("Table"), { target: { value: "Products" } });
    fireEvent.click(screen.getByRole("button", { name: /Check the connection/ }));
    expect((await screen.findByRole("alert", undefined, UI_TIMEOUT)).textContent).toContain("rejected the credential AIRTABLE_TOKEN");
    await mock.call("POST", "/api/demo/mock/credential", { name: "AIRTABLE_TOKEN", set: true });
    fireEvent.click(screen.getByRole("button", { name: /Check the connection/ }));
    await screen.findByText(/Connected to Products/, undefined, UI_TIMEOUT);
    expect(localStorage.getItem("pw-last-airtable-base")).toContain("appTEST00000000001");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    // 3. Examples: pages arrive from the extension
    await screen.findByText("Show it a few pages", undefined, UI_TIMEOUT);
    await sendPage("track_products_i_like", "https://shop-a.example.com/p/headphones", "Studio Headphones - Shop A");
    await sendPage("track_products_i_like", "https://lumen-supply.example.org/item/lamp", "Desk Lamp | Lumen Supply");
    await screen.findByText("Studio Headphones - Shop A", undefined, UI_TIMEOUT);
    const teach = await screen.findByRole("button", { name: "Teach from 2 pages" }, UI_TIMEOUT);
    fireEvent.click(teach);

    // 4. Review: the thread streams, then a review with AI rows and one that needs the person
    await screen.findByText("✓ Approve & make it live", undefined, { timeout: 8000 });
    expect(screen.getAllByText("AI").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("needs you")).toBeTruthy();
    expect(screen.getByText("shop-a.example.com")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(JOB_WORDS);
    fireEvent.change(screen.getByLabelText("Source for Store"), { target: { value: "const" } });
    fireEvent.change(screen.getByLabelText("Value for Store"), { target: { value: "Acme Outlet" } });
    let approvedFlash = false;
    const watch = new MutationObserver(() => {
      if (document.body.textContent?.includes("Nothing was made live")) approvedFlash = true;
    });
    watch.observe(document.body, { childList: true, subtree: true, characterData: true });
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    await screen.findByText("It's live", undefined, { timeout: 8000 });
    watch.disconnect();
    expect(approvedFlash).toBe(false);

    const draft = await mock.call<{ state: string; thread_id: string }>("GET", "/api/integrations/track_products_i_like/draft");
    expect(draft.state).toBe("live");
    const events = (await (await fetch(`${mock.base}/api/agent/${draft.thread_id}/stream`)).text());
    const resumed = events.split("\n\n").find((b) => b.includes("event: resumed"))!;
    const payload = JSON.parse(resumed.split("data: ")[1]).payload;
    expect(payload.edits).toEqual([{ dest_field: "Store", choice: { const: "Acme Outlet" } }]);
    expect(payload.decision).toBe("approve");
    // live, with the two example rows written
    const tracker = await mock.call<{ rows: { status: string }[] }>("GET", "/api/tracker?integration=track_products_i_like");
    expect(tracker.rows.filter((r) => r.status === "success")).toHaveLength(2);
    expect((await mock.call<{ name: string; state: string }[]>("GET", "/api/integrations")).find((i) => i.name === "track_products_i_like")?.state).toBe("live");
  }, 30000);

  it("resumes a draft that already has a destination at step 3", async () => {
    const d = await mock.call<{ name: string }>("POST", "/api/integrations/new", { intent: "Save recipes to read later" });
    await mock.call("PUT", `/api/integrations/${d.name}/destination`, { kind: "form", url: "https://forms.example.com/price-log" });
    location.hash = `#/new?draft=${d.name}`;
    mount();
    await screen.findByText("Show it a few pages", undefined, UI_TIMEOUT);
    expect(document.body.textContent).not.toMatch(JOB_WORDS);
  });

  it("pre-fills the last Airtable base", async () => {
    localStorage.setItem("pw-last-airtable-base", "https://airtable.com/appLAST0000000001/x");
    const d = await mock.call<{ name: string }>("POST", "/api/integrations/new", { intent: "Log trips" });
    location.hash = `#/new?draft=${d.name}`;
    mount();
    await screen.findByText("Where should it write?", undefined, UI_TIMEOUT);
    expect((screen.getByLabelText("Base") as HTMLInputElement).value).toContain("appLAST0000000001");
  });

  it("a teach retry that fails for another reason says why", async () => {
    const d = await mock.call<{ name: string }>("POST", "/api/integrations/new", { intent: "Gather gift ideas" });
    await mock.call("PUT", `/api/integrations/${d.name}/destination`, { kind: "form", url: "https://forms.example.com/price-log" });
    await sendPage(d.name, "https://shop-a.example.com/p/card", "Card - Shop A");
    await mock.call("PUT", "/api/settings", { ai_enabled: false });
    try {
      location.hash = `#/new?draft=${d.name}`;
      mount();
      fireEvent.click(await screen.findByRole("button", { name: "Teach from 1 page" }, UI_TIMEOUT));
      await screen.findByText("Turn AI on to let Patchwork propose a mapping.", undefined, UI_TIMEOUT);
      fireEvent.click(screen.getAllByRole("switch", { name: "AI on or off" })[0]);
      const ids = (await mock.call<{ example_page_ids: string[] }>("GET", `/api/integrations/${d.name}/draft`)).example_page_ids;
      await mock.call("POST", `/api/integrations/${d.name}/teach`, { page_ids: ids }); // taught elsewhere meanwhile
      const now = await screen.findByRole("button", { name: "Teach now" }, UI_TIMEOUT);
      await waitFor(() => expect((now as HTMLButtonElement).disabled).toBe(false), UI_TIMEOUT);
      fireEvent.click(now);
      expect((await screen.findByRole("alert", undefined, UI_TIMEOUT)).textContent).toMatch(/can only be taught once/);
    } finally {
      await mock.call("PUT", "/api/settings", { ai_enabled: true });
    }
  }, 30000);

  it("with AI off, step 4 says to turn AI on and shows the switch, never a dead end", async () => {
    const d = await mock.call<{ name: string }>("POST", "/api/integrations/new", { intent: "Collect reading material" });
    await mock.call("PUT", `/api/integrations/${d.name}/destination`, { kind: "form", url: "https://forms.example.com/price-log" });
    await sendPage(d.name, "https://shop-a.example.com/p/mug", "Blue Mug - Shop A");
    await mock.call("PUT", "/api/settings", { ai_enabled: false });
    try {
      location.hash = `#/new?draft=${d.name}`;
      mount();
      fireEvent.click(await screen.findByRole("button", { name: "Teach from 1 page" }, UI_TIMEOUT));
      await screen.findByText("Turn AI on to let Patchwork propose a mapping.", undefined, UI_TIMEOUT);
      expect(screen.getAllByRole("switch", { name: "AI on or off" }).length).toBeGreaterThanOrEqual(1);
      expect(document.body.textContent).not.toMatch(JOB_WORDS);
      // turning it on enables the retry, which teaches
      fireEvent.click(screen.getAllByRole("switch", { name: "AI on or off" })[0]);
      const now = await screen.findByRole("button", { name: "Teach now" }, UI_TIMEOUT);
      await waitFor(() => expect((now as HTMLButtonElement).disabled).toBe(false), UI_TIMEOUT);
      fireEvent.click(now);
      await screen.findByText("✓ Approve & make it live", undefined, { timeout: 8000 });
    } finally {
      await mock.call("PUT", "/api/settings", { ai_enabled: true });
    }
  }, 30000);
});
