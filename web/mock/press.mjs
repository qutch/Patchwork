// Stand-in for the extension's hotkey: POST a canned job posting to /api/pages, exactly as the
// extension would (token from GET /api/extension). Works against the mock or the real control plane.
//
//   node mock/press.mjs lever|greenhouse|branded|ashby|workable|dup [--n 0] [--base http://127.0.0.1:5182]
//   node mock/press.mjs rename            # mock/fixture form: rename "Company" -> "Employer"
//   node mock/press.mjs reset             # POST /api/demo/v2/reset
import { POSTINGS, cannedHtml } from "./v2.mjs";

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

if (what === "reset") console.log(await j("POST", "/api/demo/v2/reset"));
else if (what === "rename") console.log(await j("POST", "/api/demo/form/rename", { from: arg("from", "Company"), to: arg("to", "Employer") }));
else {
  const [url, title] = what === "dup"
    ? [POSTINGS.lever[0][0].replace(/\?.*$/, "?utm_source=newsletter#apply"), POSTINGS.lever[0][1]]
    : (POSTINGS[what] ?? (() => { throw new Error(`unknown posting kind ${what}; one of ${Object.keys(POSTINGS).join(", ")}, dup`); })())[n % POSTINGS[what].length];
  const { token } = await j("GET", "/api/extension");
  console.log(await j("POST", "/api/pages", { url, title, html: cannedHtml(url, title) }, { "X-Patchwork-Token": token }));
}
