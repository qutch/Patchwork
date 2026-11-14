// @vitest-environment node
// Part 3 client calls (contracts V13-V15: page integrations, generic tracker rows, destinations, credentials)
// against the real mock control plane (web/mock/server.mjs), started on a free port with no pacing.
import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { api, setApiBase, type TrackerRow } from "./api";
import { defaultIntegration, destinationLabel, missingCredentials, pageIntegrations, specInfo } from "./lib/integrations";
import { fieldColumns, fieldValue, isLegacyColumns } from "./lib/tracker";

const HERE = dirname(fileURLToPath(import.meta.url));
let proc: ChildProcess;

beforeAll(async () => {
  proc = spawn(process.execPath, [join(HERE, "..", "mock", "server.mjs"), "--port", "0", "--pace", "0"], { stdio: ["ignore", "pipe", "inherit"] });
  const port = await new Promise<number>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("mock did not start")), 5000);
    proc.stdout!.on("data", (b: Buffer) => {
      const m = String(b).match(/127\.0\.0\.1:(\d+)/);
      if (m) {
        clearTimeout(t);
        resolve(Number(m[1]));
      }
    });
  });
  setApiBase(`http://127.0.0.1:${port}`);
});
afterAll(() => {
  proc?.kill();
  setApiBase("");
});

const PRODUCTS = "track_product_prices";
const PAPERS = "papers_to_reading_list";
const until = async <T>(fn: () => Promise<T | null | undefined | false>, what: string): Promise<T> => {
  for (let i = 0; i < 200; i++) {
    const v = await fn();
    if (v) return v;
    await new Promise((r) => setTimeout(r, 20));
  }
  throw new Error(`timeout: ${what}`);
};
const rowFor = (integration: string, pageId: string, ok: (r: TrackerRow) => boolean) =>
  until(async () => {
    const r = (await api.tracker({ integration })).rows.find((x) => x.page_id === pageId);
    return r && ok(r) ? r : null;
  }, `row ${pageId}`);
const settled = (r: TrackerRow) => r.status !== "pending" && r.status !== "running";
const page = (url: string, title: string) => ({ url, title, html: `<html><head><title>${title}</title></head><body><h1>${title}</h1></body></html>` });

describe("Part 3 client against the mock", () => {
  it("lists page integrations with intents and a default (V15 item 2)", async () => {
    const ext = await api.extension();
    const list = pageIntegrations(ext, await api.integrations());
    expect(list.map((i) => i.name)).toEqual(["jobs_to_tracker", PRODUCTS, PAPERS]);
    expect(list.find((i) => i.name === PRODUCTS)?.intent).toBe("Track product prices");
    expect(defaultIntegration(ext, list)).toBe("jobs_to_tracker");
  });

  it("jobs rows keep title/company and gain fields (V15 item 3), so the jobs demo looks the same", async () => {
    const t = await api.tracker();
    expect(isLegacyColumns(fieldColumns(t))).toBe(true);
    const code = t.rows.find((r) => r.label === "code")!;
    expect(fieldValue(code, "title")).toBe(code.title);
    expect(fieldValue(code, "company")).toBe(code.company);
  });

  it("another integration's tracker and layouts come back with ?integration= (V15 item 4)", async () => {
    const t = await api.tracker({ integration: PRODUCTS });
    expect(t.field_names).toEqual(["product", "price", "store"]);
    expect(new Set(t.rows.map((r) => r.label))).toEqual(new Set(["code", "learned_now", "duplicate", "held"]));
    const ok = t.rows.find((r) => r.label === "code")!;
    expect(Object.keys(ok.fields ?? {})).toEqual(["product", "price", "store"]);
    const layouts = await api.layouts(PRODUCTS);
    expect(layouts.length).toBeGreaterThan(0);
    expect(layouts.every((l) => l.integration === PRODUCTS)).toBe(true);
    expect(Object.keys(layouts[0].fields)).toEqual(["product", "price", "store"]);
    // the jobs tracker is untouched by the other integration
    expect((await api.tracker()).rows.every((r) => !r.fields || !("product" in r.fields))).toBe(true);
  });

  it("each integration's active spec names its destination", async () => {
    const dest = async (name: string) => {
      const s = (await api.integrations()).find((i) => i.name === name)!;
      return specInfo((await api.version(name, s.active_version!)).spec_yaml);
    };
    expect(destinationLabel((await dest("jobs_to_tracker")).destination)).toBe("Google Form");
    expect(destinationLabel((await dest(PRODUCTS)).destination)).toBe("web form");
    const papers = await dest(PAPERS);
    expect(destinationLabel(papers.destination)).toBe("Airtable");
    expect(papers.destination?.credential).toBe("AIRTABLE_TOKEN");
    expect(papers.intent).toBe("Save papers to my reading list");
  });

  it("a page sent to an integration is read by its reader; the same page again is a duplicate", async () => {
    await api.v2Reset(PRODUCTS);
    expect((await api.tracker({ integration: PRODUCTS })).rows).toEqual([]);
    const { token } = await api.extension();
    const a = await api.postPage({ ...page("https://www.bestbuy.com/site/apple-airpods-pro-2/6447382.p", "Apple AirPods Pro 2 - Best Buy"), integration: PRODUCTS }, token);
    expect(a.integration).toBe(PRODUCTS);
    const ra = await rowFor(PRODUCTS, a.id, settled);
    expect(ra.label).toBe("code");
    expect(ra.fields).toEqual({ product: "Apple AirPods Pro 2", price: 189.99, store: "Best Buy" });
    const b = await api.postPage({ ...page("https://www.bestbuy.com/site/apple-airpods-pro-2/6447382.p?ref=x", "Apple AirPods Pro 2 - Best Buy"), integration: PRODUCTS }, token);
    expect((await rowFor(PRODUCTS, b.id, settled)).label).toBe("duplicate");
    // never in the jobs tracker
    expect((await api.tracker()).rows.some((r) => r.page_id === a.id)).toBe(false);
  });

  it("credentials are names only; a missing one fails the run, setting it and replaying writes the row (V13)", async () => {
    const creds = await api.credentials();
    expect(creds).toEqual([{ name: "AIRTABLE_TOKEN", set: false, used_by: [PAPERS] }]);
    expect(missingCredentials(creds, PAPERS)).toEqual(["AIRTABLE_TOKEN"]);
    expect(JSON.stringify(creds)).not.toMatch(/value|secret/i);
    const failed = (await api.tracker({ integration: PAPERS })).rows.find((r) => r.status === "failed")!;
    expect(failed.detail).toBe("credential AIRTABLE_TOKEN is not set: add AIRTABLE_TOKEN=... to .env");
    // mock only: pretend the value was added to .env
    const res = await fetch(`${(await api.extension()).control_url}/api/demo/mock/credential`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name: "AIRTABLE_TOKEN", set: true }),
    });
    expect(res.ok).toBe(true);
    expect(missingCredentials(await api.credentials(), PAPERS)).toEqual([]);
    await api.replay(PAPERS);
    const done = await rowFor(PAPERS, failed.page_id, (r) => r.status === "success");
    expect(fieldValue(done, "title")).toBe("Language Models are Few-Shot Learners");
    expect(fieldValue(done, "year")).toBe("2020");
  });
});
