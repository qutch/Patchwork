// v3 of the mock control plane: page integrations that are not about jobs, destinations and credentials
// (docs/contracts.md Part 3: V13 credentials, V14 Airtable sink, V15 generalization). Mounted by server.mjs
// before v2, so it only answers for its own integrations (and the shared v3 endpoints below).
//
//   track_product_prices     product / price / store  -> a labelled web form
//   papers_to_reading_list   title / authors / year   -> Airtable (credential AIRTABLE_TOKEN, not set at start)
//
// What it simulates (shapes are the contract's; the behaviour is a stand-in):
//   GET  /api/extension                 + integrations [{name, intent}], default_integration (V15 item 2)
//   GET  /api/credentials               [{name, set, used_by}] (V13; names only, never values)
//   POST /api/pages {integration}       a run for that integration: known site -> code, same page -> duplicate,
//                                       new site -> learned now (AI on) or held (AI off), credential missing -> failed
//   GET  /api/tracker?integration=      rows with `fields` + top-level `field_names` (V15 item 3)
//   GET  /api/layouts?integration=, POST /api/layouts/{id}/revert {integration}
//   GET  /api/integrations/{name}[/versions/{v}] (spec_yaml names the sink), POST .../replay
//   POST /api/demo/v2/reset|save-baseline?integration=
//   POST /api/demo/mock/credential {name, set}   mock only: pretend the value was added to (or removed from) .env

import { canonicalUrl, siteOf, TOKEN } from "./v2.mjs";

const JOBS = "jobs_to_tracker";
const JOBS_INTENT = "Add the job posting I'm looking at to my application tracker";

// ------------------------------------------------------------------------------ integrations
const DEFS = {
  track_product_prices: {
    intent: "Track product prices",
    field_names: ["product", "price", "store"],
    sink: { kind: "form", url: "https://forms.example.com/price-log", submit: "Save" },
    sink_fields: { Product: { from: "product" }, Price: { from: "price" }, Store: { from: "store" }, Link: { from: "record.canonical_url" }, "Date seen": { from: "record.captured_date" } },
    readers: {
      amazon: { site: "amazon.com", fields: { product: { css: "#productTitle" }, price: { css: ".a-price .a-offscreen", cast: "number" }, store: { const: "Amazon" } } },
      bestbuy: { site: "bestbuy.com", fields: { product: { css: "h1.sku-title" }, price: { css: '[data-testid="customer-price"] span', cast: "number" }, store: { const: "Best Buy" } } },
      target: { site: "target.com", fields: { product: { jsonld: "name", jsonld_type: "Product" }, price: { jsonld: "offers.price", jsonld_type: "Product", cast: "number" }, store: { jsonld: "offers.seller.name", jsonld_type: "Product" } } },
    },
    learned: { etsy: { site: "etsy.com", fields: { product: { jsonld: "name", jsonld_type: "Product" }, price: { jsonld: "offers.price", jsonld_type: "Product", cast: "number" }, store: { css: "a.shop-name" } } } },
    pages: {
      amazon: [["https://www.amazon.com/dp/B0C1XYZ123?ref=deal", "Sony WH-1000XM5 Wireless Headphones : Amazon.com", { product: "Sony WH-1000XM5 Wireless Headphones", price: 328, store: "Amazon" }],
        ["https://www.amazon.com/dp/B0BSHF7WHW", "Kindle Paperwhite (16 GB) : Amazon.com", { product: "Kindle Paperwhite (16 GB)", price: 149.99, store: "Amazon" }]],
      bestbuy: [["https://www.bestbuy.com/site/apple-airpods-pro-2/6447382.p", "Apple AirPods Pro 2 - Best Buy", { product: "Apple AirPods Pro 2", price: 189.99, store: "Best Buy" }]],
      target: [["https://www.target.com/p/lego-icons-orchids/-/A-82345678", "LEGO Icons Orchid 10311 : Target", { product: "LEGO Icons Orchid 10311", price: 39.99, store: "Target" }]],
      etsy: [["https://www.etsy.com/listing/1234567/ceramic-pour-over-coffee-dripper", "Ceramic Pour Over Coffee Dripper - Etsy", { product: "Ceramic Pour Over Coffee Dripper", price: 42, store: "ClayHouseStudio" }]],
    },
  },
  papers_to_reading_list: {
    intent: "Save papers to my reading list",
    field_names: ["title", "authors", "year"],
    sink: { kind: "airtable", base_id: "appR3adingL1st00", table: "Reading list", auth: { scheme: "bearer", credential: "AIRTABLE_TOKEN" } },
    sink_fields: { Title: { from: "title" }, Authors: { from: "authors" }, Year: { from: "year" }, Link: { from: "record.canonical_url" }, Added: { from: "record.captured_date" } },
    readers: {
      arxiv: { site: "arxiv.org", fields: { title: { css: "h1.title" }, authors: { css: "div.authors" }, year: { css: ".dateline", regex: "(\\d{4})", cast: "number" } } },
      openreview: { site: "openreview.net", fields: { title: { css: 'meta[name="citation_title"]', attr: "content" }, authors: { css: 'meta[name="citation_author"]', attr: "content" }, year: { css: 'meta[name="citation_publication_date"]', attr: "content", regex: "(\\d{4})" } } },
    },
    learned: { acl: { site: "aclanthology.org", fields: { title: { css: "h2#title" }, authors: { css: "p.lead" }, year: { css: 'meta[name="citation_publication_date"]', attr: "content", regex: "(\\d{4})" } } } },
    pages: {
      arxiv: [["https://arxiv.org/abs/1706.03762", "[1706.03762] Attention Is All You Need", { title: "Attention Is All You Need", authors: "Vaswani, Shazeer, Parmar, et al.", year: 2017 }],
        ["https://arxiv.org/abs/2005.14165v4", "[2005.14165] Language Models are Few-Shot Learners", { title: "Language Models are Few-Shot Learners", authors: "Brown, Mann, Ryder, et al.", year: 2020 }]],
      openreview: [["https://openreview.net/forum?id=nZeVKeeFYf9", "Denoising Diffusion Implicit Models | OpenReview", { title: "Denoising Diffusion Implicit Models", authors: "Song, Meng, Ermon", year: 2021 }]],
      acl: [["https://aclanthology.org/N19-1423/", "BERT: Pre-training of Deep Bidirectional Transformers - ACL Anthology", { title: "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding", authors: "Devlin, Chang, Lee, Toutanova", year: 2019 }]],
    },
  },
};
export const V3_NAMES = Object.keys(DEFS);
/** Canned pages for mock/press.mjs: `press --integration track_product_prices amazon`. */
export const V3_PAGES = Object.fromEntries(V3_NAMES.map((n) => [n, DEFS[n].pages]));

/** A spec's YAML (JSON flow values are valid YAML), enough for the UI to read intent and sink. */
function specYaml(name, def, parsers) {
  const j = (x) => JSON.stringify(x);
  const lines = [
    "spec_version: 1",
    `name: ${name}`,
    `intent: ${j(def.intent)}`,
    "source: {kind: api, base_url: \"patchwork://control\", list: {method: GET, path: /api/pages}, params: {since: \"{cursor}\", integration: " + name + "}}",
    "extract:",
    "  parsers:",
    ...parsers.map((pid) => `    - {id: ${pid}, match: {site: ${(def.readers[pid] ?? def.learned[pid]).site}}, fields: ${j((def.readers[pid] ?? def.learned[pid]).fields)}}`),
    "dedupe: {key: record.canonical_url}",
    "sink:",
    `  kind: ${def.sink.kind}`,
    ...Object.entries(def.sink).filter(([k]) => k !== "kind").map(([k, v]) => `  ${k}: ${j(v)}`),
    "  fields:",
    ...Object.entries(def.sink_fields).map(([k, v]) => `    ${j(k)}: ${j(v)}`),
  ];
  return lines.join("\n") + "\n";
}

// ------------------------------------------------------------------------------ module
export function createV3(h) {
  // h: { send, fail, nowIso, clone, timer, settings: () => settings, base: () => url }
  const credentials = new Map([["AIRTABLE_TOKEN", false]]);
  let pageSeq = 900; // page ids distinct from v2's
  const I = {};
  const baselines = {};

  const credOf = (def) => def.sink.auth?.credential ?? null;
  const calls = (n) => (h.settings().agent === "deep" ? n : 0);
  const readerOf = (def, pid) => def.readers[pid] ?? def.learned[pid];

  function fresh(name) {
    const def = DEFS[name];
    const st = { name, def, pages: [], rows: new Map(), layouts: [], versions: [], active: null, runId: 0 };
    const today = new Date();
    const at = (hh, mm) => new Date(today.getFullYear(), today.getMonth(), today.getDate(), hh, mm).toISOString().replace(/\.\d+Z$/, "+00:00");
    const base = Object.keys(def.readers);
    base.forEach((pid, k) => {
      st.layouts.push({ integration: name, parser_id: pid, site: def.readers[pid].site, fingerprint: null, fields: h.clone(def.readers[pid].fields),
        origin: k === 0 ? { kind: "taught", at: at(9, 30), thread_id: `build-${name.slice(0, 6)}`, approved_by: "human" } : { kind: "learned", at: at(10, 5 + k * 7), thread_id: `learn-${pid}0000`, approved_by: k === 1 ? "auto" : "human" },
        introduced_in: k + 1, active: true });
      st.versions.push({ version: k + 1, parsers: base.slice(0, k + 1), created_at: at(9 + k, 30), deployed_at: at(9 + k, 31), tests_passed: true, tests_summary: `${8 + k * 4} passed` });
    });
    st.active = st.versions.at(-1).version;
    return st;
  }
  const activeParsers = (st) => st.versions.find((v) => v.version === st.active)?.parsers ?? [];
  function addVersion(st, parsers) {
    const version = st.versions.at(-1).version + 1;
    st.versions.push({ version, parsers: [...parsers], created_at: h.nowIso(), deployed_at: h.nowIso(), tests_passed: true, tests_summary: `${8 + parsers.length * 4} passed` });
    st.active = version;
    for (const l of st.layouts) l.active = parsers.includes(l.parser_id);
    return version;
  }

  function ingest(st, { url, title, html }, at) {
    const seq = ++pageSeq;
    const id = `pg-${String(seq).padStart(6, "0")}`;
    const captured_at = at ?? new Date().toISOString().replace(/\.\d+Z$/, "+00:00");
    const d = new Date(captured_at);
    const page = { id, seq, url, canonical_url: canonicalUrl(url), site: siteOf(url), title: String(title ?? ""), html: String(html ?? ""),
      text: "", captured_at, captured_date: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`, integration: st.name };
    st.pages.push(page);
    st.rows.set(id, { page_id: id, seq, captured_at, site: page.site, url, title: page.title, company: null, fields: {},
      status: "pending", label: null, reason: null, detail: null, parser: null, version: null, routed_by: null,
      parser_origin: null, ai_calls: 0, run_id: null, learn_thread_id: null, incident_id: null });
    return page;
  }

  /** Values a reader would extract: the canned ones, else something plausible from the title. */
  function valuesFor(st, page) {
    for (const list of Object.values(st.def.pages)) {
      const hit = list.find(([u]) => canonicalUrl(u) === page.canonical_url);
      if (hit) return h.clone(hit[2]);
    }
    const t = page.title.split(/ [-|:] /)[0].replace(/^\[[^\]]+\]\s*/, "").trim() || page.title;
    if (st.name === "track_product_prices") {
      const n = [...page.canonical_url].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 9973, 7);
      return { product: t, price: Math.round(n) / 100 + 9, store: page.site.split(".")[0].replace(/^./, (c) => c.toUpperCase()) };
    }
    return { title: t, authors: "Unknown authors", year: new Date().getFullYear() };
  }

  function processRow(st, row, { seeded = false } = {}) {
    const page = st.pages.find((p) => p.id === row.page_id);
    row.run_id = ++st.runId;
    const cred = credOf(st.def);
    if (cred && !credentials.get(cred)) {
      return Object.assign(row, { status: "failed", label: null, reason: null, fields: {},
        detail: `credential ${cred} is not set: add ${cred}=... to .env` });
    }
    let pid = activeParsers(st).find((x) => readerOf(st.def, x)?.site === page.site) ?? null;
    let learnedNow = false;
    if (!pid) {
      const candidate = Object.entries(st.def.learned).find(([, r]) => r.site === page.site)?.[0] ?? page.site.split(".")[0];
      if (!h.settings().ai_enabled) {
        return Object.assign(row, { status: "held", label: "held", reason: "PARSE_NO_MATCH", fields: {},
          detail: `no parser for site=${page.site}; no fingerprint matched. AI is off, so nothing was learned or written.` });
      }
      if (!st.def.learned[candidate]) st.def.learned[candidate] = { site: page.site, fields: Object.fromEntries(st.def.field_names.map((f) => [f, { css: `[itemprop="${f}"]` }])) };
      const r = st.def.learned[candidate];
      const existing = st.layouts.find((l) => l.parser_id === candidate);
      const origin = { kind: "learned", at: seeded ? row.captured_at : h.nowIso(), thread_id: null, approved_by: "auto" };
      const version = addVersion(st, [...activeParsers(st), candidate]);
      if (existing) Object.assign(existing, { origin, introduced_in: version, active: true });
      else st.layouts.push({ integration: st.name, parser_id: candidate, site: r.site, fingerprint: null, fields: h.clone(r.fields), origin, introduced_in: version, active: true });
      pid = candidate;
      learnedNow = true;
      row.ai_calls = calls(6);
    }
    const layout = st.layouts.find((l) => l.parser_id === pid);
    const fields = valuesFor(st, page);
    Object.assign(row, { parser: pid, version: st.active, routed_by: "match", parser_origin: h.clone(layout.origin), fields,
      title: st.def.field_names.includes("title") ? fields.title : page.title });
    const dup = [...st.rows.values()].some((r) => r !== row && r.status === "success" && st.pages.find((p) => p.id === r.page_id)?.canonical_url === page.canonical_url);
    if (dup) return Object.assign(row, { status: "duplicate", label: "duplicate", reason: null, detail: `already sent (${page.canonical_url})` });
    return Object.assign(row, { status: "success", label: learnedNow ? "learned_now" : "code", reason: null, detail: null });
  }

  function run(st, ids) {
    for (const id of ids) {
      const row = st.rows.get(id);
      if (!row) continue;
      row.status = "pending";
      h.timer(150, () => {
        row.status = "running";
        h.timer(700, () => processRow(st, row));
      });
    }
  }

  // ---------------------------------------------------------------- seed
  function seed(name) {
    const st = fresh(name);
    I[name] = st;
    baselines[name] = h.clone({ layouts: st.layouts, versions: st.versions, active: st.active });
    const today = new Date();
    const at = (hh, mm) => new Date(today.getFullYear(), today.getMonth(), today.getDate(), hh, mm).toISOString().replace(/\.\d+Z$/, "+00:00");
    const P = st.def.pages;
    const press = ([url, title], hhmm, opts) => processRow(st, st.rows.get(ingest(st, { url, title, html: `<title>${title}</title>` }, at(...hhmm)).id), { seeded: true, ...opts });
    const keys = Object.keys(P);
    if (name === "papers_to_reading_list") {
      // the token was there this morning, then removed from .env: the last two presses failed to start
      credentials.set("AIRTABLE_TOKEN", true);
      press(P.arxiv[0], [10, 40]);
      press(P.openreview[0], [10, 52]);
      press(P.acl[0], [11, 1]);
      credentials.set("AIRTABLE_TOKEN", false);
      press(P.arxiv[1], [11, 20]);
      return;
    }
    press(P[keys[0]][0], [10, 15]);
    press(P[keys[1]][0], [10, 22]);
    press(P.etsy[0], [10, 31]);
    press([P[keys[0]][0][0].replace("?ref=deal", "?ref=wishlist"), P[keys[0]][0][1]], [10, 33]);
    press(P[keys[0]][1], [10, 47]);
    const ai = h.settings().ai_enabled;
    h.settings().ai_enabled = false;
    press(["https://www.walmart.com/ip/ninja-air-fryer/5101234", "Ninja Air Fryer AF101 - Walmart.com"], [11, 2]);
    h.settings().ai_enabled = ai;
  }
  for (const n of V3_NAMES) seed(n);

  function reset(name) {
    const st = I[name];
    const b = h.clone(baselines[name]);
    Object.assign(st, { pages: [], rows: new Map(), layouts: b.layouts, versions: b.versions, active: b.active });
  }

  // ---------------------------------------------------------------- routes
  const totals = (rows) => ({
    pages: rows.length,
    ai_calls: rows.reduce((a, r) => a + (r.ai_calls || 0), 0),
    code: rows.filter((r) => r.label === "code").length,
    learned_now: rows.filter((r) => r.label === "learned_now").length,
    held: rows.filter((r) => r.label === "held").length,
    duplicate: rows.filter((r) => r.label === "duplicate").length,
  });
  const summary = (st) => ({ name: st.name, intent: st.def.intent, active_version: st.active, latest_version: st.versions.at(-1).version, open_incidents: 0 });
  const which = (url, body) => {
    const n = url.searchParams.get("integration") ?? body?.integration;
    return n && I[n] ? I[n] : null;
  };

  function handle(req, res, url, M, p, body) {
    const { send, fail } = h;
    let mm;
    if (p === "/api/extension" && M === "GET")
      return send(res, 200, { token: TOKEN, control_url: h.base(), default_integration: JOBS,
        integrations: [{ name: JOBS, intent: JOBS_INTENT }, ...V3_NAMES.map((n) => ({ name: n, intent: DEFS[n].intent }))] }), true;
    if (p === "/api/credentials" && M === "GET")
      return send(res, 200, [...credentials].map(([name, set]) => ({ name, set, used_by: V3_NAMES.filter((n) => credOf(DEFS[n]) === name) }))), true;
    if (p === "/api/demo/mock/credential" && M === "POST") {
      if (!credentials.has(body.name)) return fail(res, 404, `unknown credential ${body.name}`), true;
      credentials.set(body.name, !!body.set);
      return send(res, 200, { name: body.name, set: !!body.set }), true;
    }
    if (p === "/api/pages" && M === "POST") {
      const st = which(url, body);
      if (!st) return false;
      if (req.headers["x-patchwork-token"] !== TOKEN) return fail(res, 401, "bad or missing X-Patchwork-Token"), true;
      if (typeof body.url !== "string" || typeof body.html !== "string") return fail(res, 422, "url, title and html are required"), true;
      const page = ingest(st, body);
      run(st, [page.id]);
      return send(res, 201, { id: page.id, seq: page.seq, site: page.site, canonical_url: page.canonical_url, integration: st.name, run_id: st.runId + 1, queued: false }), true;
    }
    if (p === "/api/pages" && M === "GET") {
      const st = which(url, body);
      if (!st) return false;
      const since = Number(url.searchParams.get("since") || 0);
      return send(res, 200, st.pages.filter((x) => x.seq > since)), true;
    }
    if ((mm = p.match(/^\/api\/pages\/([^/]+)$/)) && M === "GET") {
      const id = decodeURIComponent(mm[1]);
      const st = Object.values(I).find((s) => s.rows.has(id));
      if (!st) return false;
      const { html, ...page } = st.pages.find((x) => x.id === id); // eslint-disable-line no-unused-vars
      return send(res, 200, { page, row: st.rows.get(id) }), true;
    }
    if (p === "/api/tracker" && M === "GET") {
      const st = which(url, body);
      if (!st) return false;
      const limit = Number(url.searchParams.get("limit") || 50);
      const rows = [...st.rows.values()].sort((a, b) => b.seq - a.seq);
      return send(res, 200, { rows: rows.slice(0, limit), totals: totals(rows), field_names: st.def.field_names }), true;
    }
    if (p === "/api/layouts" && M === "GET") {
      const st = which(url, body);
      if (!st) return false;
      return send(res, 200, [...st.layouts].sort((a, b) => Number(b.active) - Number(a.active))), true;
    }
    if ((mm = p.match(/^\/api\/layouts\/([^/]+)\/revert$/)) && M === "POST") {
      const st = which(url, body);
      if (!st) return false;
      const pid = decodeURIComponent(mm[1]);
      const active = activeParsers(st);
      if (!active.includes(pid)) return fail(res, 404, `parser ${pid} is not in the active version`), true;
      if (active.length === 1) return fail(res, 409, "refusing to remove the last parser"), true;
      return send(res, 200, { version: addVersion(st, active.filter((x) => x !== pid)), deployed: true }), true;
    }
    if ((p === "/api/demo/v2/reset" || p === "/api/demo/v2/save-baseline") && M === "POST") {
      const st = which(url, body);
      if (!st) return false;
      if (p.endsWith("reset")) reset(st.name);
      else baselines[st.name] = h.clone({ layouts: st.layouts, versions: st.versions, active: st.active });
      return send(res, 200, { ok: true, integration: st.name, active_version: st.active }), true;
    }
    if ((mm = p.match(/^\/api\/integrations\/([^/]+)(\/.*)?$/))) {
      const st = I[decodeURIComponent(mm[1])];
      if (!st) return false;
      const sub = mm[2] ?? "";
      if (M === "GET" && !sub)
        return send(res, 200, { name: st.name, active_version: st.active,
          versions: st.versions.map(({ version, created_at, deployed_at, tests_passed, tests_summary }) => ({ version, created_at, deployed_at, tests_passed, tests_summary })),
          recent_runs: [], open_incidents: [] }), true;
      let vm;
      if (M === "GET" && (vm = sub.match(/^\/versions\/(\d+)$/))) {
        const v = st.versions.find((x) => x.version === Number(vm[1]));
        if (!v) return fail(res, 404, `${st.name} v${vm[1]} does not exist`), true;
        return send(res, 200, { name: st.name, version: v.version, active: v.version === st.active, created_at: v.created_at, deployed_at: v.deployed_at,
          manifest: { name: st.name, version: v.version, parsers: v.parsers }, spec_yaml: specYaml(st.name, st.def, v.parsers), files: {},
          tests_passed: v.tests_passed, tests_summary: v.tests_summary }), true;
      }
      if (M === "POST" && sub === "/replay") {
        const ids = [...st.rows.values()].filter((r) => r.status === "held" || r.status === "failed").sort((a, b) => a.seq - b.seq).map((r) => r.page_id);
        run(st, ids);
        return send(res, 200, { run_id: st.runId + 1 }), true;
      }
      return fail(res, 404, `mock: ${M} ${p} is not simulated for ${st.name}`), true;
    }
    return false;
  }

  return { handle, integrations: () => V3_NAMES.map((n) => summary(I[n])) };
}
