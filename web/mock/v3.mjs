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
//   POST /api/credentials {name, value}              201 {name, set: true, used_by}; the value is never kept or echoed (Task 1)
//   POST /api/integrations/new, GET .../{name}/draft  drafts (Task 2); GET /api/integrations gains a state per entry
//   POST /api/pages {integration: <draft>}            kept for the teach step, never run
//   PUT  .../{name}/destination, GET /api/destinations/airtable/tables|api/operations   (Task 3)
//   POST .../{name}/teach {page_ids}                  a build-shaped thread ending in request_deploy + review (Tasks 4-5)
//                                                     an example page titled "... (no price)" makes the teach ask_user first
//   POST /api/agent/{id}/resume {answer} | {decision, edits}  answers the question / approve makes the draft live
//   DELETE /api/integrations/{name}?delete_rows=      removes a draft or live integration
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
  const drafts = new Map(); // name -> draft (Task 2), until it goes live
  const draftPages = new Map(); // page id -> page, for pages sent to a draft
  const threads = new Map(); // teach threads
  let pageSeq = 900; // page ids distinct from v2's
  const I = {};
  const baselines = {};

  const credOf = (def) => def.sink.auth?.credential ?? null;
  const liveNames = () => Object.keys(I);
  const openDrafts = () => [...drafts.values()].filter((d) => d.state !== "live");
  const calls = (n) => (h.settings().agent === "deep" ? n : 0);
  const readerOf = (def, pid) => def.readers[pid] ?? def.learned[pid];
  const usedBy = (name) => [...liveNames().filter((n) => credOf(I[n].def) === name), ...openDrafts().filter((d) => d.destination?.credential === name).map((d) => d.name)];

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
    if (st.def.valuesFor) return st.def.valuesFor(page);
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

  // ---------------------------------------------------------------- new integration wizard (Tasks 1-5)
  const slug = (intent) => String(intent).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").split("_").slice(0, 5).join("_") || "integration";
  const CRED_NAME = /^[A-Z][A-Z0-9_]{1,63}$/;
  const REFUSED_CRED = new Set(["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PATH", "HOME", "SHELL", "USER", "PYTHONPATH"]);
  const APP_ID = /app[A-Za-z0-9]{14,}/;

  const TABLES = [{ id: "tblProducts000001", name: "Products" }, { id: "tblReadingList0001", name: "Reading list" }];
  const TABLE_FIELDS = {
    Products: [{ name: "Product", type: "text", required: true }, { name: "Price", type: "number", required: false },
      { name: "Store", type: "text", required: false }, { name: "Link", type: "url", required: false }, { name: "Date Added", type: "date", required: false }],
    "Reading list": [{ name: "Title", type: "text", required: true }, { name: "Authors", type: "text", required: false },
      { name: "Year", type: "number", required: false }, { name: "Link", type: "url", required: false }, { name: "Added", type: "date", required: false }],
  };
  const OPERATIONS = [
    { method: "POST", path: "/papers", summary: "Add a paper", fields: ["title", "venue.name", "year", "url"] },
    { method: "PUT", path: "/papers/{id}", summary: "Replace a paper", fields: ["title", "venue.name", "year", "url"] },
    { method: "PATCH", path: "/papers/{id}", summary: "Update a paper", fields: ["title", "year"] },
  ];
  const typeOf = (n) => (/year|count|price/i.test(n) ? "number" : /date|added|seen/i.test(n) ? "date" : /url|link/i.test(n) ? "url" : "text");

  class Refused extends Error {}
  /** Task 3 `control.destinations.check`: what the destination wants, or what to do next. */
  function checkDestination(d) {
    if (d.kind === "airtable") {
      const baseId = d.base_id ?? String(d.base_url ?? "").match(APP_ID)?.[0];
      if (!baseId) throw new Refused("Couldn't find a base id (app...) in what was entered. Paste the base's URL or id.");
      if (!d.credential) throw new Refused("Choose the credential Airtable should use.");
      if (!credentials.get(d.credential)) throw new Refused(`Airtable rejected the credential ${d.credential}: it is not set. Add it under Credentials, then check again.`);
      const t = TABLES.find((x) => x.name === d.table || x.id === d.table);
      if (!t) throw new Refused(`The table ${d.table} wasn't found in base ${baseId}. Tables there: ${TABLES.map((x) => x.name).join(", ")}.`);
      return { dest: { ...d, base_id: baseId, base_url: undefined }, check: { kind: "airtable", title: `${t.name} (Airtable)`, fields: h.clone(TABLE_FIELDS[t.name]) } };
    }
    if (d.kind === "form") {
      if (!/^https?:\/\//.test(d.url ?? "") || String(d.url).includes("nowhere.invalid")) throw new Refused(`Couldn't reach ${d.url}. Check the address and try again.`);
      return { dest: d, check: { kind: "form", title: "Web form", fields: TABLE_FIELDS.Products.map((f) => ({ ...f, name: f.name === "Date Added" ? "Date seen" : f.name })) } };
    }
    if (d.kind === "api") {
      if (!/^https?:\/\//.test(d.base_url ?? "")) throw new Refused(`Couldn't reach ${d.base_url}. Check the address and try again.`);
      const op = OPERATIONS.find((o) => o.method === d.operation?.method && o.path === d.operation?.path);
      if (!op) throw new Refused("Choose which operation Patchwork should call.");
      return { dest: d, check: { kind: "api", title: `${op.method} ${op.path}`, fields: op.fields.map((n) => ({ name: n, type: typeOf(n), required: n === "title" })), operations: h.clone(OPERATIONS) } };
    }
    if (d.kind === "sqlite") {
      if (!d.path || !d.table) throw new Refused("Enter the database file and the table.");
      return { dest: d, check: { kind: "sqlite", title: `${d.table} (SQLite)`, fields: [{ name: "title", type: "text", required: true }, { name: "url", type: "text", required: false }, { name: "added", type: "date", required: false }] } };
    }
    throw new Refused(`Unknown destination kind ${d.kind}.`);
  }

  // ---- the teach thread: a real agent thread's shape (todos, subagents, tools, then a review)
  const threadStatus = (t) => ({ thread_id: t.id, mode: "build", subject: t.subject, integration: t.draft, state: t.state, pending: t.pending, last_seq: t.events.length, created_at: t.created_at });
  function emit(t, ev) {
    const e = { ...ev, seq: t.events.length + 1, t: new Date().toISOString(), thread_id: t.id };
    t.events.push(e);
    if (e.type === "status") t.state = e.state;
    h.broadcast(t.listeners, { id: e.seq, event: e.type, data: e });
    return e;
  }
  const PRODUCT_NAME = /name|product|title|item/i, PRICE = /price|cost|amount/i, LINK = /link|url/i, DATE = /date|added|seen/i, STORE = /store|shop|seller|merchant/i;
  const productValues = (page) => {
    const n = [...page.canonical_url].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 9973, 7);
    const t = page.title.split(/ [-|:] /)[0].trim() || page.title;
    return { product: t, price: Math.round(n) / 100 + 9, store: page.site.split(".")[0].replace(/^./, (c) => c.toUpperCase()) };
  };
  /** Task 5 `request_deploy.review[]` for the examples and the destination's fields. */
  function buildReview(draft, pages) {
    const byPage = (f) => Object.fromEntries(pages.map((p) => [p.id, f(p)]));
    // the destination field's type, required flag and allowed values ride on every row (as the backend sends them)
    const meta = (f) => ({ type: f.type ?? "text", required: !!f.required, ...(f.values?.length ? { values_allowed: f.values } : {}) });
    return draft.destination.fields.map((f) => ({ ...rowFor(f), ...meta(f) }));
    function rowFor(f) {
      const n = f.name;
      if (STORE.test(n))
        return { dest_field: n, source: "constant: Acme", values: byPage(() => "Acme"), ai_proposed: true, status: "needs_you",
          alternatives: ["reader: offers.seller.name", "the site's name"] };
      if (PRODUCT_NAME.test(n))
        return { dest_field: n, source: "reader: h1.product-title", values: byPage((p) => productValues(p).product), ai_proposed: true, status: "checked",
          alternatives: ["reader: og:title", "reader: jsonld name"] };
      if (PRICE.test(n))
        return { dest_field: n, source: "reader: jsonld offers.price", values: byPage((p) => String(productValues(p).price)), ai_proposed: true, status: "checked",
          alternatives: ["reader: .price"] };
      if (LINK.test(n)) return { dest_field: n, source: "the page's canonical URL", values: byPage((p) => p.canonical_url), ai_proposed: false, status: "checked", alternatives: [] };
      if (DATE.test(n)) return { dest_field: n, source: "today's date", values: byPage(() => new Date().toISOString().slice(0, 10)), ai_proposed: false, status: "checked", alternatives: [] };
      return { dest_field: n, source: "skipped", values: byPage(() => null), ai_proposed: false, status: "checked", alternatives: [] };
    }
  }
  /** Why a typed value does not fit the row's field (the backend's V15 rule checks, as ReviewTable's constProblem), or null. */
  function constProblem(row, v) {
    if (!String(v).trim()) return "a constant must not be empty";
    if (row.type === "one_of" && row.values_allowed?.length) return row.values_allowed.includes(String(v)) ? null : `not one of ${row.values_allowed.join(", ")}`;
    const s = String(v).trim();
    if (row.type === "number" || row.type === "integer") {
      const t = s.replace(/,/g, "").replace(/(\d)_(?=\d)/g, "$1");
      if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(t) || !Number.isFinite(Number(t))) return `expected a number, got '${s}'`;
    }
    if (row.type === "date") {
      const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
      const d = m && +m[1] >= 1 ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
      if (d && m) d.setUTCFullYear(+m[1]);
      if (!m || !d || d.getUTCFullYear() !== +m[1] || d.getUTCMonth() !== +m[2] - 1 || d.getUTCDate() !== +m[3]) return `expected a date YYYY-MM-DD, got '${s}'`;
    }
    if (row.type === "url") {
      let host = "";
      try { host = /^https?:\/\/\S+$/i.test(s) ? new URL(s).hostname : ""; } catch { host = ""; }
      if (!host) return `expected an absolute http(s) URL, got '${s}'`;
    }
    return null;
  }
  function startTeach(draft) {
    const id = `build-${Math.random().toString(16).slice(2, 12)}`;
    const pages = draft.example_page_ids.map((pid) => draftPages.get(pid)).filter(Boolean);
    const sites = [...new Set(pages.map((p) => p.site))];
    const t = { id, draft: draft.name, subject: `draft-${draft.name}`, state: "running", pending: null, events: [], created_at: h.nowIso(), listeners: new Set(), pages, review: null };
    threads.set(id, t);
    const A = (agent) => (ev) => ({ agent, ...ev });
    const steps = [
      { type: "status", state: "running", mode: "build" },
      { type: "todos", todos: [
        { content: "Map each destination field to what the example pages show", status: "in_progress" },
        { content: "Check every reader on every example site", status: "pending" },
        { content: "Compile, test and ask you to approve", status: "pending" }] },
      { type: "subagent_start", name: "mapper", task: `Map ${draft.destination.fields.length} destination fields to the first example` },
      A("mapper")({ type: "tool_call", tool: "propose_mapping", args: { fields: draft.destination.fields.map((f) => f.name) } }),
      A("mapper")({ type: "tool_result", tool: "propose_mapping", ok: true, summary: "OK: every reader found its value on the first example" }),
      { type: "subagent_end", name: "mapper", summary: "Proposed a reader for each field; each value checked by code." },
      { type: "todos", todos: [
        { content: "Map each destination field to what the example pages show", status: "completed" },
        { content: "Check every reader on every example site", status: "in_progress" },
        { content: "Compile, test and ask you to approve", status: "pending" }] },
      { type: "subagent_start", name: "learner", task: `Check the readers on ${sites.length} example site${sites.length === 1 ? "" : "s"}` },
      ...sites.slice(1).flatMap((s) => [
        A("learner")({ type: "tool_call", tool: "add_site_parser", args: { page_id: pages.find((p) => p.site === s).id } }),
        A("learner")({ type: "tool_result", tool: "add_site_parser", ok: true, summary: `OK: a reader for ${s} (never a catch-all)` }),
      ]),
      { type: "subagent_end", name: "learner", summary: "Every reader matches its own site only." },
      { type: "subagent_start", name: "builder", task: "Compile the spec and run the tests" },
      A("builder")({ type: "tool_call", tool: "compile_spec", args: {} }),
      A("builder")({ type: "tool_result", tool: "compile_spec", ok: true, summary: "compiled v1" }),
      A("builder")({ type: "tool_call", tool: "run_tests", args: { version: 1 } }),
      A("builder")({ type: "tool_result", tool: "run_tests", ok: true, summary: "12 passed" }),
      { type: "subagent_end", name: "builder", summary: "v1 compiled, 12 tests passed." },
      { type: "message", agent: "supervisor", text: "Everything below is checked by code. Rows marked AI were proposed by the model; confirm the ones that need you." },
    ];
    let i = 0;
    // as the real mapper does when a page shows no value for a field: ask the person before proposing
    const noPrice = pages.find((p) => /\(no price\)/i.test(p.title ?? ""));
    t.ask = noPrice ? { type: "ask_user", interrupt_id: `int-ask-${id.slice(6, 12)}`, dest_field: "Price",
      question: `${noPrice.site} shows no readable price. What should Price be for pages like it?`,
      options: ["Leave it blank for review", "Read it from the page title"] } : null;
    const next = () => {
      if (i < steps.length) {
        emit(t, steps[i++]);
        return h.timer(i < 3 ? 250 : 200, next);
      }
      if (t.ask) {
        const ev = emit(t, t.ask);
        t.ask = null;
        t.pending = ev;
        t.resumeAsk = next;
        emit(t, { type: "status", state: "waiting", mode: "build" });
        return;
      }
      t.review = buildReview(draft, pages);
      const ev = emit(t, { type: "request_deploy", interrupt_id: `int-${id.slice(6, 12)}`, name: draft.name, version: 1,
        summary: `${draft.name} v1: ${pages.length} example page${pages.length === 1 ? "" : "s"}, ${sites.length} site${sites.length === 1 ? "" : "s"}`,
        spec_diff: `--- /dev/null\n+++ spec.yaml\n+name: ${draft.name}\n`, code_diff: "", tests: { passed: true, summary: "12 passed" }, review: t.review });
      t.pending = ev;
      emit(t, { type: "status", state: "waiting", mode: "build" });
    };
    h.timer(150, next);
    return t;
  }

  /** Approve with edits (Task 5 `apply_review_edits`): only an offered alternative, a constant or a skip. */
  function applyEdits(review, edits) {
    const out = h.clone(review);
    for (const e of edits ?? []) {
      const row = out.find((r) => r.dest_field === e.dest_field);
      if (!row) throw new Refused(`no row ${e.dest_field}`);
      const c = e.choice;
      if (c && typeof c === "object" && "const" in c && constProblem(row, c.const)) throw new Refused(`${e.dest_field}: the constant '${c.const}' does not fit the field (${constProblem(row, c.const)})`);
      if (c && typeof c === "object" && c.skip === true && row.required) throw new Refused(`${e.dest_field} is required by the destination; it cannot be skipped`);
      if (c && typeof c === "object" && "const" in c) Object.assign(row, { source: `constant: ${c.const}`, values: Object.fromEntries(Object.keys(row.values).map((k) => [k, String(c.const)])), status: "checked", ai_proposed: false });
      else if (c && typeof c === "object" && c.skip === true) Object.assign(row, { source: "skipped", values: Object.fromEntries(Object.keys(row.values).map((k) => [k, null])), status: "checked", ai_proposed: false });
      else if (row.alternatives.some((a) => JSON.stringify(a) === JSON.stringify(c))) Object.assign(row, { source: c, status: "checked" });
      else throw new Refused(`${e.dest_field}: ${JSON.stringify(c)} is not one of the offered alternatives`);
    }
    return out;
  }

  /** The approved draft becomes a live page integration with its example pages written (2 rows). */
  function goLive(t, draft, review) {
    const pages = t.pages;
    const sinkFields = {};
    const fieldNames = [];
    const vals = new Map(); // page id -> {field: value}
    for (const r of review) {
      const n = r.dest_field;
      if (r.source === "skipped") continue;
      if (r.source === "the page's canonical URL") sinkFields[n] = { from: "record.canonical_url" };
      else if (r.source === "today's date") sinkFields[n] = { from: "record.captured_date" };
      else if (String(r.source).startsWith("constant: ")) sinkFields[n] = { const: String(r.source).slice(10) };
      else {
        const f = n.toLowerCase().replace(/[^a-z0-9]+/g, "_");
        fieldNames.push(f);
        sinkFields[n] = { from: f };
      }
      for (const [pid, v] of Object.entries(r.values)) vals.set(pid, { ...(vals.get(pid) ?? {}), [n]: v });
    }
    const dest = draft.destination;
    const sink = dest.kind === "airtable" ? { kind: "airtable", base_id: dest.base_id, table: dest.table, auth: { scheme: "bearer", credential: dest.credential } }
      : dest.kind === "form" ? { kind: "form", url: dest.url, submit: "Save" }
      : dest.kind === "api" ? { kind: "api", base_url: dest.base_url, operation: dest.operation, ...(dest.credential ? { auth: { scheme: "bearer", credential: dest.credential } } : {}) }
      : { kind: "sqlite", path: dest.path, table: dest.table };
    const readers = {};
    for (const p of pages) {
      const pid = p.site.split(".")[0];
      readers[pid] ??= { site: p.site, fields: Object.fromEntries(fieldNames.map((f) => [f, { css: `[itemprop="${f}"]` }])) };
    }
    const def = { intent: draft.intent, field_names: fieldNames, sink, sink_fields: sinkFields, readers, learned: {}, pages: {},
      valuesFor: (page) => {
        const src = pages.find((x) => x.canonical_url === page.canonical_url);
        const row = src ? vals.get(src.id) : null;
        const base = productValues(page);
        return Object.fromEntries(fieldNames.map((f) => {
          const dn = Object.keys(sinkFields).find((k) => sinkFields[k].from === f);
          const v = row?.[dn] ?? base[f] ?? base[Object.keys(base).find((b) => f.includes(b)) ?? ""] ?? null;
          return [f, /^[0-9.]+$/.test(String(v)) && PRICE.test(f) ? Number(v) : v];
        }));
      } };
    DEFS[draft.name] = def;
    const st = fresh(draft.name);
    I[draft.name] = st;
    baselines[draft.name] = h.clone({ layouts: st.layouts, versions: st.versions, active: st.active });
    for (const p of pages.slice(0, 2)) {
      const page = ingest(st, { url: p.url, title: p.title, html: p.html });
      processRow(st, st.rows.get(page.id));
    }
    draft.state = "live"; // the draft record stays (state live), as the control plane keeps it
    return st;
  }

  function resumeTeach(t, body) {
    if (!t.pending || t.pending.interrupt_id !== body.interrupt_id) return [409, `no pending interrupt ${body.interrupt_id} on ${t.id}`];
    if (t.pending.type === "ask_user") {
      if (typeof body.answer !== "string" || !body.answer.trim()) return [422, "answer the question"];
      const asked = t.pending;
      t.pending = null;
      emit(t, { type: "resumed", interrupt_id: asked.interrupt_id, payload: { answer: body.answer.trim() } });
      emit(t, { type: "status", state: "running", mode: "build" });
      h.timer(150, t.resumeAsk);
      return [200, threadStatus(t)];
    }
    if (!["approve", "reject"].includes(body.decision)) return [422, "decision must be approve or reject"];
    const draft = drafts.get(t.draft);
    let review = t.review;
    if (body.decision === "approve") {
      try {
        review = applyEdits(t.review, body.edits);
      } catch (e) {
        return [422, e.message];
      }
    }
    const pending = t.pending;
    t.pending = null;
    emit(t, { type: "resumed", interrupt_id: pending.interrupt_id, payload: { decision: body.decision, comment: body.comment ?? "", ...(body.edits ? { edits: body.edits } : {}) } });
    emit(t, { type: "status", state: "running", mode: "build" });
    h.timer(200, () => {
      if (body.decision === "approve" && draft) {
        goLive(t, draft, review);
        emit(t, { type: "deployed", name: t.draft, version: 1, previous_version: null });
        emit(t, { type: "done", result: { deployed: true, version: 1 } });
      } else {
        if (draft) Object.assign(draft, { state: "draft", thread_id: null });
        emit(t, { type: "done", result: { deployed: false, version: 1 } });
      }
      emit(t, { type: "status", state: "done", mode: "build" });
    });
    return [200, threadStatus(t)];
  }

  /** Everything the wizard calls. Returns true when it answered. */
  function handleWizard(req, res, url, M, p, body) {
    const { send, fail } = h;
    let mm;
    if (p === "/api/credentials" && M === "POST") {
      const name = String(body.name ?? "");
      if (!CRED_NAME.test(name) || REFUSED_CRED.has(name)) return fail(res, 422, `${name || "(empty)"} is not an allowed credential name: use capitals, digits and underscores (not a model key or process variable)`), true;
      if (typeof body.value !== "string" || body.value.length < 8) return fail(res, 422, "the value is too short to be a credential"), true;
      credentials.set(name, true); // the value is never kept: a mock has nothing to store it for
      return send(res, 201, { name, set: true, used_by: usedBy(name) }), true;
    }
    if (p === "/api/integrations/new" && M === "POST") {
      const intent = String(body.intent ?? "").trim();
      if (!intent) return fail(res, 422, "intent is required"), true;
      const name = body.name ? String(body.name) : slug(intent);
      if (drafts.has(name) || I[name] || name === JOBS || name === "po_to_stockroom") return fail(res, 409, `an integration named ${name} already exists`), true;
      const d = { name, intent, state: "draft", destination: null, example_page_ids: [], thread_id: null, created_at: h.nowIso() };
      drafts.set(name, d);
      return send(res, 201, d), true;
    }
    if (p === "/api/destinations/airtable/tables" && M === "GET") {
      const cred = url.searchParams.get("credential");
      if (!cred || !credentials.get(cred)) return fail(res, 422, `Airtable rejected the credential ${cred}: it is not set. Add it under Credentials, then try again.`), true;
      return send(res, 200, TABLES), true;
    }
    if (p === "/api/destinations/api/operations" && M === "GET") {
      if (!/^https?:\/\//.test(url.searchParams.get("base_url") ?? "")) return fail(res, 422, "Couldn't reach that address. Check it and try again."), true;
      return send(res, 200, OPERATIONS), true;
    }
    if ((mm = p.match(/^\/api\/integrations\/([^/]+)\/(draft|destination|teach)$/))) {
      const name = decodeURIComponent(mm[1]);
      const d = drafts.get(name);
      if (!d) return false; // a live integration: the older handlers answer
      if (mm[2] === "draft" && M === "GET") return send(res, 200, d), true;
      if (mm[2] === "destination" && M === "PUT") {
        try {
          const { dest, check } = checkDestination(body);
          d.destination = { ...JSON.parse(JSON.stringify(dest)), fields: check.fields };
          return send(res, 200, check), true;
        } catch (e) {
          if (e instanceof Refused) return fail(res, 422, e.message), true;
          throw e;
        }
      }
      if (mm[2] === "teach" && M === "POST") {
        if (d.state !== "draft") return fail(res, 409, `${name} is ${d.state}: it can only be taught once`), true;
        const ids = Array.isArray(body.page_ids) ? body.page_ids : [];
        if (!d.destination) return fail(res, 422, "choose a destination first"), true;
        if (!ids.length || ids.some((x) => !draftPages.has(x))) return fail(res, 422, "send at least one example page first"), true;
        if (!h.settings().ai_enabled) return fail(res, 409, "AI is off. Turn AI on to let Patchwork propose a mapping."), true;
        d.example_page_ids = ids;
        const t = startTeach(d);
        Object.assign(d, { state: "teaching", thread_id: t.id });
        return send(res, 200, { thread_id: t.id }), true;
      }
    }
    if (M === "DELETE" && (mm = p.match(/^\/api\/integrations\/([^/]+)$/))) {
      const name = decodeURIComponent(mm[1]);
      if (!drafts.has(name) && !I[name]) return false;
      const rows = I[name] ? [...I[name].rows.values()].filter((r) => r.status === "success").length : 0;
      drafts.delete(name);
      delete I[name];
      for (const [id, pg] of draftPages) if (pg.integration === name) draftPages.delete(id);
      return send(res, 200, { deleted: name, rows_deleted: url.searchParams.get("delete_rows") === "true" ? rows : 0 }), true;
    }
    if ((mm = p.match(/^\/api\/agent\/([^/]+)(\/[a-z]+)?$/))) {
      const t = threads.get(decodeURIComponent(mm[1]));
      if (!t) return false;
      if (M === "GET" && !mm[2]) return send(res, 200, threadStatus(t)), true;
      if (M === "GET" && mm[2] === "/stream")
        return h.sse(req, res, url, {
          listeners: t.listeners,
          items: () => t.events.map((e) => ({ id: e.seq, event: e.type, data: e })),
          terminal: (x) => x.event === "status" && (x.data.state === "done" || x.data.state === "failed"),
          isOver: () => t.state === "done" || t.state === "failed",
        }), true;
      if (M === "POST" && mm[2] === "/resume") {
        const [code, out] = resumeTeach(t, body);
        return (code === 200 ? send(res, 200, out) : fail(res, code, out)), true;
      }
    }
    return false;
  }

  /** A page sent to a draft is kept for the teach step and never run (Task 2). */
  function ingestForDraft(d, body) {
    const seq = ++pageSeq;
    const id = `pg-${String(seq).padStart(6, "0")}`;
    const at = new Date().toISOString().replace(/\.\d+Z$/, "+00:00");
    const page = { id, seq, url: body.url, canonical_url: canonicalUrl(body.url), site: siteOf(body.url), title: String(body.title ?? ""), html: String(body.html ?? ""),
      text: "", captured_at: at, captured_date: at.slice(0, 10), integration: d.name };
    draftPages.set(id, page);
    d.example_page_ids.push(id);
    return page;
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
  const summary = (st) => ({ name: st.name, state: "live", intent: st.def.intent, active_version: st.active, latest_version: st.versions.at(-1).version, open_incidents: 0 });
  const which = (url, body) => {
    const n = url.searchParams.get("integration") ?? body?.integration;
    return n && I[n] ? I[n] : null;
  };

  function handle(req, res, url, M, p, body) {
    const { send, fail } = h;
    let mm;
    if (handleWizard(req, res, url, M, p, body)) return true;
    if (p === "/api/extension" && M === "GET")
      return send(res, 200, { token: TOKEN, control_url: h.base(), default_integration: JOBS,
        integrations: [{ name: JOBS, intent: JOBS_INTENT }, ...liveNames().map((n) => ({ name: n, intent: I[n].def.intent })),
          ...openDrafts().map((d) => ({ name: d.name, intent: d.intent }))] }), true;
    if (p === "/api/credentials" && M === "GET")
      return send(res, 200, [...credentials].map(([name, set]) => ({ name, set, used_by: usedBy(name) }))), true;
    if (p === "/api/demo/mock/credential" && M === "POST") {
      if (!credentials.has(body.name)) return fail(res, 404, `unknown credential ${body.name}`), true;
      credentials.set(body.name, !!body.set);
      return send(res, 200, { name: body.name, set: !!body.set }), true;
    }
    if (p === "/api/pages" && M === "POST" && openDrafts().some((d) => d.name === body.integration)) {
      if (req.headers["x-patchwork-token"] !== TOKEN) return fail(res, 401, "bad or missing X-Patchwork-Token"), true;
      if (typeof body.url !== "string" || typeof body.html !== "string") return fail(res, 422, "url, title and html are required"), true;
      const page = ingestForDraft(drafts.get(body.integration), body);
      return send(res, 201, { id: page.id, seq: page.seq, site: page.site, canonical_url: page.canonical_url, integration: page.integration, run_id: null, queued: false }), true;
    }
    if (p === "/api/pages" && M === "GET" && openDrafts().some((d) => d.name === url.searchParams.get("integration")))
      return send(res, 200, [...draftPages.values()].filter((x) => x.integration === url.searchParams.get("integration")).map(({ html, ...pg }) => pg)), true; // eslint-disable-line no-unused-vars
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

  return {
    handle,
    integrations: () => [...liveNames().map((n) => summary(I[n])),
      ...openDrafts().map((d) => ({ name: d.name, state: d.state, intent: d.intent, active_version: null, latest_version: 0, open_incidents: 0 }))],
    threads: () => [...threads.values()].map(threadStatus),
  };
}
