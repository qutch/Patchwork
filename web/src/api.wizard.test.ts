// @vitest-environment node
// The wizard's client calls (Tasks 1-5) against the mock: shapes and refusals.
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { api, ApiError } from "./api";
import { startMock, type Mock } from "./test/harness";

let mock: Mock;
beforeAll(async () => {
  mock = await startMock();
});
afterAll(() => mock.stop());

const status = async (p: Promise<unknown>) => {
  try {
    await p;
    return 200;
  } catch (e) {
    return e instanceof ApiError ? e.status : -1;
  }
};

describe("wizard API (mock)", () => {
  it("adds a credential: 201, set, never the value; refuses model and process names", async () => {
    const c = await api.addCredential("SHOP_TOKEN", "sentinel-cred-123456");
    expect(c).toEqual({ name: "SHOP_TOKEN", set: true, used_by: [] });
    expect(JSON.stringify(await api.credentials())).not.toContain("sentinel-cred-123456");
    for (const bad of ["ANTHROPIC_API_KEY", "PATH", "lower"]) expect(await status(api.addCredential(bad, "x".repeat(12)))).toBe(422);
  });

  it("creates drafts (slug name, 409 on a repeat) and lists them with a state", async () => {
    const d = await api.newIntegration("Track upcoming events");
    expect(d).toMatchObject({ name: "track_upcoming_events", state: "draft", destination: null, example_page_ids: [], thread_id: null });
    expect(await status(api.newIntegration("Track upcoming events"))).toBe(409);
    expect((await api.integrations()).find((i) => i.name === d.name)?.state).toBe("draft");
    expect((await api.extension()).integrations?.some((i) => i.name === d.name)).toBe(true);
    expect(await api.draft(d.name)).toMatchObject({ name: d.name });
  });

  it("checks destinations, tells the person what to do, and lists write operations only", async () => {
    const d = await api.newIntegration("Collect papers");
    expect(await status(api.putDestination(d.name, { kind: "airtable", base_id: "appTEST00000000001", table: "Products", credential: "AIRTABLE_TOKEN" }))).toBe(422);
    await mock.call("POST", "/api/demo/mock/credential", { name: "AIRTABLE_TOKEN", set: true });
    const ok = await api.putDestination(d.name, { kind: "airtable", base_url: "https://airtable.com/appTEST00000000001/shrX", table: "Products", credential: "AIRTABLE_TOKEN" });
    expect(ok.fields[1]).toMatchObject({ name: "Price", type: "number" });
    expect((await api.draft(d.name)).destination).toMatchObject({ kind: "airtable", base_id: "appTEST00000000001", table: "Products" });
    expect(await status(api.putDestination(d.name, { kind: "airtable", base_id: "appTEST00000000001", table: "Nope", credential: "AIRTABLE_TOKEN" }))).toBe(422);
    expect((await api.airtableTables("appTEST00000000001", "AIRTABLE_TOKEN")).map((t) => t.name)).toContain("Products");
    const ops = await api.apiOperations("https://api.example.com");
    expect(ops.every((o) => ["POST", "PUT", "PATCH"].includes(o.method))).toBe(true);
    expect(ops.find((o) => o.method === "POST")!.fields).toEqual(expect.arrayContaining(["title", "venue.name", "year"]));
  });

  it("teach: 422 without a destination or pages, 409 twice, 409 with AI off; delete removes it", async () => {
    const d = await api.newIntegration("Bookmark recipes");
    expect(await status(api.teach(d.name, ["pg-x"]))).toBe(422);
    await api.putDestination(d.name, { kind: "form", url: "https://forms.example.com/price-log" });
    expect(await status(api.teach(d.name, []))).toBe(422);
    const { token } = await api.extension();
    const sent = await api.postPage({ url: "https://shop-a.example.com/p/1", title: "Mug - Shop A", html: "<h1>x</h1>", integration: d.name }, token);
    expect(sent).toMatchObject({ queued: false });
    expect((await api.pages({ integration: d.name })).map((p) => p.id)).toEqual([sent.id]);
    await api.putSettings({ ai_enabled: false });
    expect(await status(api.teach(d.name, [sent.id]))).toBe(409);
    await api.putSettings({ ai_enabled: true });
    const { thread_id } = await api.teach(d.name, [sent.id]);
    expect((await api.draft(d.name)).state).toBe("teaching");
    expect(await status(api.teach(d.name, [sent.id]))).toBe(409);
    expect((await api.threads()).find((t) => t.thread_id === thread_id)).toMatchObject({ integration: d.name });
    await api.deleteIntegration(d.name, false);
    expect((await api.integrations()).some((i) => i.name === d.name)).toBe(false);
  });

  it("an edit that is not an offered alternative is refused", async () => {
    const d = await api.newIntegration("Remember gadgets");
    await api.putDestination(d.name, { kind: "form", url: "https://forms.example.com/price-log" });
    const { token } = await api.extension();
    const p = await api.postPage({ url: "https://shop-a.example.com/p/2", title: "Gadget - Shop A", html: "<h1>x</h1>", integration: d.name }, token);
    const { thread_id } = await api.teach(d.name, [p.id]);
    let pending = null;
    for (let i = 0; i < 100 && !pending; i++) {
      pending = (await api.thread(thread_id)).pending;
      if (!pending) await new Promise((r) => setTimeout(r, 20));
    }
    expect(pending?.type).toBe("request_deploy");
    expect(await status(api.resume(thread_id, { interrupt_id: pending!.interrupt_id, decision: "approve", edits: [{ dest_field: "Product", choice: { jsonld: "bogus" } }] }))).toBe(422);

    // every row says its field's type and whether it is required; a skip of a required field or a typed value that
    // does not fit is refused, and the review stays open
    const review = (pending as { review: { dest_field: string; type?: string; required?: boolean }[] }).review;
    expect(review.every((r) => typeof r.type === "string" && typeof r.required === "boolean")).toBe(true);
    const required = review.find((r) => r.required)!;
    const numeric = review.find((r) => r.type === "number")!;
    const resume = (edits: unknown) => api.resume(thread_id, { interrupt_id: pending!.interrupt_id, decision: "approve", edits: edits as never });
    expect(await status(resume([{ dest_field: required.dest_field, choice: { skip: true } }]))).toBe(422);
    expect(await status(resume([{ dest_field: numeric.dest_field, choice: { const: "about ten" } }]))).toBe(422);
    expect(await status(resume([{ dest_field: numeric.dest_field, choice: { const: "0x10" } }]))).toBe(422);
    const dated = review.find((r) => r.type === "date");
    if (dated) expect(await status(resume([{ dest_field: dated.dest_field, choice: { const: "2026-02-30" } }]))).toBe(422);
    const linked = review.find((r) => r.type === "url");
    if (linked) expect(await status(resume([{ dest_field: linked.dest_field, choice: { const: "http://:80" } }]))).toBe(422);
    expect((await api.thread(thread_id)).pending?.interrupt_id).toBe(pending!.interrupt_id);
    expect(await status(resume([{ dest_field: numeric.dest_field, choice: { const: "1,299.50" } }]))).toBe(200);
  });
});
