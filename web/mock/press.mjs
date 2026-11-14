// Stand-in for the extension's hotkey: POST a canned job posting to /api/pages, exactly as the
// extension would (token from GET /api/extension). Works against the mock or the real control plane.
//
//   node mock/press.mjs lever|greenhouse|branded|ashby|workable|dup [--n 0] [--base http://127.0.0.1:5182]
//   node mock/press.mjs rename            # mock/fixture form: rename "Company" -> "Employer"
//   node mock/press.mjs reset             # POST /api/demo/v2/reset
//   node mock/press.mjs amazon --integration track_product_prices   # v3: amazon|bestbuy|target|etsy (or any URL kind below)
//   node mock/press.mjs arxiv --integration papers_to_reading_list  # v3: arxiv|openreview|acl
//   node mock/press.mjs token --set 1     # mock only: pretend AIRTABLE_TOKEN was added to .env (--set 0 removes it)
import { POSTINGS, cannedHtml } from "./v2.mjs";
import { V3_PAGES } from "./v3.mjs";

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const BASE = arg("base", process.env.PATCHWORK_API || "http://127.0.0.1:5182").replace(/\/$/, "");
const what = process.argv[2] ?? "lever";
const n = Number(arg("n", 0));

const j = async (method, path, body, headers = {}) => {
  const r = await fetch(BASE + path, { method, headers: { "content-type": "application/json", ...headers }, body: body ? JSON.stringify(body) : undefined });
  const d = await r.json().catch(() => null);
  if (!r.ok) throw new Error(`${method} ${path}: ${r.status} ${d?.detail ?? ""}`);
  return d;
};

const integration = arg("integration", null);
const iq = integration ? `?integration=${encodeURIComponent(integration)}` : "";

if (what === "reset") console.log(await j("POST", `/api/demo/v2/reset${iq}`));
else if (what === "token") console.log(await j("POST", "/api/demo/mock/credential", { name: arg("name", "AIRTABLE_TOKEN"), set: arg("set", "1") !== "0" }));
else if (integration && V3_PAGES[integration]) {
  const pages = V3_PAGES[integration][what];
  if (!pages) throw new Error(`unknown page kind ${what} for ${integration}; one of ${Object.keys(V3_PAGES[integration]).join(", ")}`);
  const [url, title] = pages[n % pages.length];
  const { token } = await j("GET", "/api/extension");
  console.log(await j("POST", "/api/pages", { url, title, html: `<html><head><title>${title}</title></head><body><h1>${title}</h1></body></html>`, integration }, { "X-Patchwork-Token": token }));
}
else if (what === "rename") console.log(await j("POST", "/api/demo/form/rename", { from: arg("from", "Company"), to: arg("to", "Employer") }));
else {
  const [url, title] = what === "dup"
    ? [POSTINGS.lever[0][0].replace(/\?.*$/, "?utm_source=newsletter#apply"), POSTINGS.lever[0][1]]
    : (POSTINGS[what] ?? (() => { throw new Error(`unknown posting kind ${what}; one of ${Object.keys(POSTINGS).join(", ")}, dup`); })())[n % POSTINGS[what].length];
  const { token } = await j("GET", "/api/extension");
  console.log(await j("POST", "/api/pages", { url, title, html: cannedHtml(url, title) }, { "X-Patchwork-Token": token }));
}
