// v2 of the mock control plane: page inbox, jobs_to_tracker runs, LEARN and form REPAIR threads,
// layouts and bench v2 (docs/contracts.md Part 2, V9/V10/V12). Mounted by server.mjs.
//
// What it simulates (shapes are the contract's; the behaviour is a stand-in for W4a + W1 + W3):
//   POST /api/pages  -> a run on arrival (one at a time, presses during a run are queued)
//   known site or fingerprint  -> success, label "code", 0 AI calls
//   same canonical URL again   -> duplicate
//   unknown layout, AI on      -> a LEARN thread (evidence + learned events), then "learned_now"
//   unknown layout, AI off     -> held PARSE_NO_MATCH (replay after switching AI on learns it)
//   form question renamed      -> held CONTRACT_CHANGED before any write, incident, REPAIR -> approve -> replay
//
// It starts "seeded": mid-demo, with a row for every label and an open form incident.
// POST /api/demo/v2/reset goes back to the baseline (three known layouts, empty tracker).

const NAME = "jobs_to_tracker";
const DEFAULT_LABELS = ["Email", "Job Title", "Company", "Link", "Date Applied"];
export const TOKEN = "mock-extension-token";
/** The demo Google Form (mock: nothing is ever sent to it). */
const FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSmockTrackerForm/viewform";

// ------------------------------------------------------------------------------ pages
const TRACKING = /^(utm_.*|lever-source|lever-origin|gh_src|source|ref)$/i;
export function siteOf(url) {
  try {
    return new URL(url).hostname.toLowerCase().replace(/^www\./, "");
  } catch {
    return "";
  }
}
export function canonicalUrl(url) {
  try {
    const u = new URL(url);
    u.hash = "";
    u.hostname = u.hostname.toLowerCase().replace(/^www\./, "");
    for (const k of [...u.searchParams.keys()]) if (TRACKING.test(k)) u.searchParams.delete(k);
    u.port = "";
    return u.toString().replace(/\?$/, "");
  } catch {
    return url;
  }
}
const visibleText = (html) =>
  String(html).replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

const COMPANY = { palantir: "Palantir Technologies", airbnb: "Airbnb", ramp: "Ramp", acme: "Acme Analytics", figma: "Figma", notion: "Notion" };
const companyFrom = (url, site) => {
  if (site === "careers.airbnb.com") return "Airbnb";
  const slug = (() => {
    try {
      return new URL(url).pathname.split("/").filter(Boolean)[0] ?? "";
    } catch {
      return "";
    }
  })();
  return COMPANY[slug] ?? (slug ? slug[0].toUpperCase() + slug.slice(1) : null);
};
/** The posting title inside a document.title, per platform (Lever puts the company first). */
const titleFrom = (t, site = "") => {
  const s = String(t || "").replace(/^Job Application for /, "").replace(/ at [A-Z][\w]*$/, "");
  const parts = s.split(/ [-|@–] /);
  return (site === "jobs.lever.co" && parts.length > 1 ? parts.slice(1).join(" - ") : parts[0]).trim() || null;
};

/** Canned postings for mock/press.mjs (what the extension would send). */
export const POSTINGS = {
  lever: [
    ["https://jobs.lever.co/palantir/0ccbe620-1b2a?lever-source=LinkedIn", "Palantir Technologies - Forward Deployed Engineer"],
    ["https://jobs.lever.co/palantir/7f3e9a10-c4d1", "Palantir Technologies - Software Engineer, Defense"],
    ["https://jobs.lever.co/figma/41aa77c2-0e19", "Figma - Product Designer, Editor"],
  ],
  greenhouse: [
    ["https://boards.greenhouse.io/airbnb/jobs/6011223", "Job Application for Senior Software Engineer, Payments at Airbnb"],
    ["https://boards.greenhouse.io/notion/jobs/5544120", "Job Application for Data Engineer at Notion"],
  ],
  branded: [["https://careers.airbnb.com/positions/6100457/?gh_jid=6100457", "Staff Data Scientist, Pricing - Airbnb Careers"]],
  ashby: [
    ["https://jobs.ashbyhq.com/ramp/9d6c1f3e-77b0", "Software Engineer, Backend @ Ramp"],
    ["https://jobs.ashbyhq.com/ramp/02b4e8aa-1c55", "Product Designer @ Ramp"],
  ],
  workable: [
    ["https://apply.workable.com/acme/j/4F2A9C1B7E/", "Data Analyst - Acme Analytics"],
    ["https://apply.workable.com/acme/j/9B1D44E0A2/", "Analytics Engineer - Acme Analytics"],
  ],
};
export function cannedHtml(url, title) {
  const site = siteOf(url);
  const t = titleFrom(title, site);
  const gh = site === "careers.airbnb.com" ? `<script src="https://boards.greenhouse.io/embed/job_board/js?for=airbnb"></script>` : "";
  return `<html><head><title>${title}</title>${gh}</head><body><div class="cookie">We use cookies. Accept</div><h1>${t}</h1><a>Apply for this job</a></body></html>`;
}

// ------------------------------------------------------------------------------ readers (what each layout extracts)
const READERS = {
  lever: { site: "jobs.lever.co", fingerprint: ".posting-headline", fields: { title: { css: ".posting-headline h2" }, company: { css: ".main-header-logo img", attr: "alt" } } },
  greenhouse: { site: "boards.greenhouse.io", fingerprint: '[src*=".greenhouse.io/"]', fields: { title: { css: "h1.app-title" }, company: { css: "span.company-name", regex: "at (.+)" } } },
  ashby: { site: "jobs.ashbyhq.com", fingerprint: '[class*="ashby-job-posting"]', fields: { title: { jsonld: "title", jsonld_type: "JobPosting" }, company: { jsonld: "hiringOrganization.name", jsonld_type: "JobPosting" } } },
  workable: { site: "apply.workable.com", fingerprint: '[data-ui="job-title"]', fields: { title: { css: 'h1[data-ui="job-title"]' }, company: { css: 'meta[property="og:site_name"]', attr: "content" } } },
};
const readerFor = (site) => Object.entries(READERS).find(([, r]) => r.site === site);
const parserIdFor = (site) => readerFor(site)?.[0] ?? site.split(".").filter((p) => !["www", "jobs", "apply", "boards", "careers", "com", "io", "co"].includes(p))[0] ?? "site";

// ------------------------------------------------------------------------------ module
export function createV2(h) {
  // h: { send, fail, sse, broadcast, timer, nowIso, clone, settings: () => settings, base: () => url }
  let V;
  let baseline;

  const layoutOf = (pid) => V.layouts.find((l) => l.parser_id === pid);
  const activeParsers = () => V.versions.find((v) => v.version === V.active)?.parsers ?? [];
  const specCompany = () => V.versions.find((v) => v.version === V.active)?.companyLabel ?? "Company";
  const calls = (n) => (h.settings().agent === "deep" ? n : 0);

  function addVersion(parsers, companyLabel) {
    const version = (V.versions.at(-1)?.version ?? 0) + 1;
    V.versions.push({ version, parsers: [...parsers], companyLabel, created_at: h.nowIso(), deployed_at: null, tests_passed: true, tests_summary: `${10 + parsers.length * 4} passed` });
    return version;
  }
  function deployVersion(version) {
    const prev = V.active;
    V.active = version;
    const v = V.versions.find((x) => x.version === version);
    v.deployed_at = h.nowIso();
    for (const l of V.layouts) l.active = v.parsers.includes(l.parser_id);
    return prev;
  }
  function addLayout(pid, origin, version) {
    const r = READERS[pid] ?? { site: null, fingerprint: null, fields: {} };
    const existing = layoutOf(pid);
    if (existing) Object.assign(existing, { origin, introduced_in: version });
    else V.layouts.push({ integration: NAME, parser_id: pid, site: r.site, fingerprint: r.fingerprint, fields: h.clone(r.fields), origin, introduced_in: version, active: false });
  }

  // ---------------------------------------------------------------- state
  function baseState() {
    V = {
      seq: 0, pages: [], rows: new Map(), queue: [], busy: false, runId: 0,
      layouts: [], versions: [], active: null,
      form: [...DEFAULT_LABELS],
      threads: new Map(), incidents: [], incidentId: 100,
    };
    const today = new Date();
    const at = (h_, m) => new Date(today.getFullYear(), today.getMonth(), today.getDate(), h_, m).toISOString().replace(/\.\d+Z$/, "+00:00");
    const v1 = addVersion(["lever"], "Company");
    addLayout("lever", { kind: "taught", at: at(9, 12), thread_id: "build-1a2b3c4d5e", approved_by: "human" }, v1);
    const v2 = addVersion(["lever", "greenhouse"], "Company");
    addLayout("greenhouse", { kind: "learned", at: at(10, 41), thread_id: "learn-77e0c2a9b1", approved_by: "human" }, v2);
    const v3 = addVersion(["lever", "greenhouse", "ashby"], "Company");
    addLayout("ashby", { kind: "learned", at: at(11, 5), thread_id: "learn-3f9a2c1b0d", approved_by: "auto" }, v3);
    deployVersion(v3);
  }
  const snapshot = () => h.clone({ layouts: V.layouts, versions: V.versions, active: V.active });
  function reset() {
    for (const t of V.threads.values()) t.listeners.clear();
    const keep = baseline;
    V = { ...V, seq: 0, pages: [], rows: new Map(), queue: [], busy: false, form: [...DEFAULT_LABELS], threads: new Map(), incidents: [] };
    Object.assign(V, h.clone(keep));
  }

  // ---------------------------------------------------------------- rows
  function ingest({ url, title, html }, { at } = {}) {
    const seq = ++V.seq;
    const id = `pg-${String(seq).padStart(6, "0")}`;
    const captured_at = at ?? new Date().toISOString().replace("Z", "+00:00");
    const d = new Date(captured_at);
    const page = {
      id, seq, url, canonical_url: canonicalUrl(url), site: siteOf(url), title: String(title ?? ""), html: String(html ?? ""),
      text: visibleText(html), captured_at, captured_date: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`,
    };
    V.pages.push(page);
    V.rows.set(id, {
      page_id: id, seq, captured_at, site: page.site, url, title: page.title, company: null,
      status: "pending", label: null, reason: null, detail: null, parser: null, version: null, routed_by: null,
      parser_origin: null, ai_calls: 0, run_id: null, learn_thread_id: null, incident_id: null,
    });
    return page;
  }
  const pageOf = (id) => V.pages.find((p) => p.id === id);

  function route(page) {
    const active = activeParsers();
    const bySite = active.find((pid) => layoutOf(pid)?.site === page.site);
    if (bySite) return [bySite, "match"];
    const fp = active.find((pid) => pid === "greenhouse" && page.html.includes(".greenhouse.io/"));
    if (fp) return [fp, "fingerprint"];
    return [null, null];
  }
  const written = (canon) => [...V.rows.values()].some((r) => r.status === "success" && pageOf(r.page_id)?.canonical_url === canon);

  function openFormIncident(row) {
    let inc = V.incidents.find((i) => i.status === "open");
    if (!inc) {
      const missing = specCompany();
      const added = V.form.find((l) => !DEFAULT_LABELS.includes(l) && l !== missing) ?? "Employer";
      inc = {
        id: ++V.incidentId, integration: NAME, version: V.active, run_id: row.run_id, type: "CONTRACT_CHANGED",
        reason_codes: ["CONTRACT_CHANGED"], status: "open", created_at: h.nowIso(),
        summary: `The tracker form changed: the question "${missing}" is gone and "${added}" is new. Blocked before any write.`,
        detail: {
          contract_errors: [`body.${missing}: not accepted by destination (no such property)`],
          sink_call: { method: "POST", path: "/form" },
        },
        samples: null,
        old_openapi: { properties: Object.fromEntries(DEFAULT_LABELS.map((l) => [l, { type: "string" }])) },
        new_openapi: { properties: Object.fromEntries(V.form.map((l) => [l, { type: "string" }])) },
        renamed: { from: missing, to: added },
      };
      V.incidents.push(inc);
    }
    return inc;
  }

  /** One record through the pipeline; returns the final row. */
  function processRow(row) {
    const page = pageOf(row.page_id);
    row.run_id = V.runId;
    Object.assign(row, { status: "running", reason: null, detail: null, label: null });
    // preflight: the form must still have every label the spec writes
    const want = ["Email", "Job Title", specCompany(), "Link", "Date Applied"];
    const gone = want.filter((l) => !V.form.includes(l));
    if (gone.length) {
      const inc = openFormIncident(row);
      return Object.assign(row, { status: "held", label: "held", reason: "CONTRACT_CHANGED", incident_id: inc.id,
        detail: `Preflight: the tracker form no longer has ${gone.map((g) => `"${g}"`).join(", ")}. Nothing was written.` });
    }
    const [pid, routed_by] = route(page);
    if (!pid) {
      const learning = [...V.threads.values()].find((t) => t.mode === "learn" && t.site === page.site && t.state !== "done" && t.state !== "failed");
      if (learning) {
        learning.waiting.add(row.page_id);
        return Object.assign(row, { status: "learning", label: null, reason: "PARSE_NO_MATCH",
          detail: `Waiting for the reader being learned for ${page.site}.` });
      }
      if (h.settings().ai_enabled) {
        const tid = startLearn(page);
        return Object.assign(row, { status: "learning", label: null, reason: "PARSE_NO_MATCH", learn_thread_id: tid,
          detail: `no parser for site=${page.site}; no fingerprint matched. Learning this layout.` });
      }
      return Object.assign(row, { status: "held", label: "held", reason: "PARSE_NO_MATCH", learn_thread_id: null,
        detail: `no parser for site=${page.site}; no fingerprint matched. AI is off, so nothing was learned or written.` });
    }
    const layout = layoutOf(pid);
    Object.assign(row, {
      parser: pid, version: V.active, routed_by, parser_origin: h.clone(layout.origin),
      title: titleFrom(page.title, page.site), company: companyFrom(page.url, page.site),
    });
    if (written(page.canonical_url)) return Object.assign(row, { status: "duplicate", label: "duplicate", reason: null, detail: `already in the tracker (${page.canonical_url})`, incident_id: null });
    const learnedNow = row.learn_thread_id && layout.origin?.thread_id === row.learn_thread_id;
    return Object.assign(row, { status: "success", label: learnedNow ? "learned_now" : "code", incident_id: null, reason: null, detail: null });
  }

  function enqueue(ids) {
    for (const id of ids) {
      if (V.queue.includes(id)) continue;
      V.queue.push(id);
      const r = V.rows.get(id);
      if (r && r.status !== "running") r.status = "pending";
    }
    pump();
  }
  function pump() {
    if (V.busy || !V.queue.length) return;
    V.busy = true;
    V.runId++;
    const id = V.queue.shift();
    const row = V.rows.get(id);
    h.timer(150, () => {
      row.status = "running";
      h.timer(800, () => {
        processRow(row);
        V.busy = false;
        pump();
      });
    });
  }

  // ---------------------------------------------------------------- threads
  function newThread(mode, subject, extra = {}) {
    const id = `${mode}-${Math.random().toString(16).slice(2, 12)}`;
    const t = { id, mode, subject, state: "running", pending: null, events: [], created_at: h.nowIso(), listeners: new Set(), v2: true, waiting: new Set(), ...extra };
    V.threads.set(id, t);
    return t;
  }
  function emit(t, ev) {
    const e = { ...ev, seq: t.events.length + 1, t: new Date().toISOString(), thread_id: t.id };
    t.events.push(e);
    if (e.type === "status") t.state = e.state;
    h.broadcast(t.listeners, { id: e.seq, event: e.type, data: e });
    return e;
  }
  /** Emit a script of [delayMs, event | fn] steps, then call `then`. */
  function play(t, steps, then) {
    let i = 0;
    const next = () => {
      if (i >= steps.length) return then?.();
      const [ms, ev] = steps[i++];
      h.timer(ms, () => {
        if (typeof ev === "function") ev();
        else emit(t, ev);
        if (ev?.type === "ask_user" || ev?.type === "request_deploy") return; // paused until resume
        next();
      });
    };
    next();
  }

  function evidenceFor(page, pid) {
    const r = READERS[pid] ?? { fields: { title: { css: "h1" }, company: { css: 'meta[property="og:site_name"]', attr: "content" } } };
    const title = titleFrom(page.title, page.site);
    const company = companyFrom(page.url, page.site);
    const where = (f) => (r.fields[f]?.jsonld ? "jsonld" : r.fields[f]?.attr ? "html" : "text");
    return [
      { name: "title", value: title, ok: true, where: where("title"), extractor: r.fields.title },
      { name: "company", value: company, ok: true, where: where("company"), extractor: r.fields.company },
    ];
  }

  function learnSteps(t, page, pid, version, fields) {
    const nCalls = calls(5);
    return [
      [100, { type: "status", state: "running", mode: "learn" }],
      [200, { type: "todos", todos: [
        { content: `Read the page outline for ${page.site}`, status: "in_progress" },
        { content: "Propose a reader for title and company", status: "pending" },
        { content: "Check every value against the page (code)", status: "pending" },
        { content: "Compile, test, deploy or ask", status: "pending" },
      ] }],
      [250, { type: "subagent_start", name: "learner", task: `Learn a reader for ${page.site} from ${page.id}` }],
      [300, { type: "tool_call", agent: "learner", tool: "get_page", args: { page_id: page.id } }],
      [400, { type: "tool_result", agent: "learner", tool: "get_page", ok: true, summary: `outline: 38 elements, ${fields.some((f) => f.where === "jsonld") ? "1 JSON-LD JobPosting" : "no JSON-LD"}, title "${page.title}"` }],
      [450, { type: "tool_call", agent: "learner", tool: "test_parser", args: { fields: Object.fromEntries(fields.map((f) => [f.name, f.extractor])) } }],
      [350, { type: "tool_result", agent: "learner", tool: "test_parser", ok: true, summary: fields.map((f) => `${f.name}=${JSON.stringify(f.value)}`).join(", ") }],
      [300, { type: "tool_call", agent: "learner", tool: "check_evidence", args: { page_id: page.id } }],
      [250, { type: "tool_result", agent: "learner", tool: "check_evidence", ok: true, summary: `${fields.length}/${fields.length} values found on the page and corroborated by its title or metadata; nothing ambiguous` }],
      [150, { type: "evidence", page_id: page.id, parser_id: pid, fields, ok: true }],
      [200, { type: "subagent_end", name: "learner", summary: `Reader for ${page.site}: ${fields.map((f) => `${f.name} from ${f.where}`).join(", ")}` }],
      [200, { type: "subagent_start", name: "builder", task: `Compile ${NAME} v${version} with parser ${pid} and run its tests` }],
      [250, { type: "tool_call", agent: "builder", tool: "compile_spec", args: { name: NAME, version } }],
      [350, { type: "tool_result", agent: "builder", tool: "compile_spec", ok: true, summary: `compiled ${NAME} v${version}` }],
      [250, { type: "tool_call", agent: "builder", tool: "run_tests", args: { name: NAME, version } }],
      [400, { type: "tool_result", agent: "builder", tool: "run_tests", ok: true, summary: V.versions.find((v) => v.version === version)?.tests_summary ?? "passed" }],
      [150, { type: "subagent_end", name: "builder", summary: `v${version}: tests passed` }],
      [0, () => { t.modelCalls = nCalls; }],
    ];
  }

  function startLearn(page) {
    const pid = parserIdFor(page.site);
    const t = newThread("learn", page.id, { site: page.site, pageId: page.id, parserId: pid, modelCalls: 0, startedMs: Date.now() });
    t.waiting.add(page.id);
    const version = addVersion([...activeParsers(), pid], specCompany());
    t.version = version;
    const fields = evidenceFor(page, pid);
    const auto = (h.settings().learn_policy ?? "auto") === "auto";
    const steps = learnSteps(t, page, pid, version, fields);
    if (auto) {
      steps.push([200, { type: "learned", integration: NAME, parser_id: pid, site: page.site, version, deployed: true, approval: "auto",
        reason: "Every value was found on the page and corroborated by its title or metadata, nothing was ambiguous, the full suite passed and the change only adds one parser." }]);
      steps.push([150, () => finishLearn(t, "auto")]);
    } else {
      steps.push([200, { type: "learned", integration: NAME, parser_id: pid, site: page.site, version, deployed: false, approval: "pending",
        reason: "Learn policy is \"approve\": a person decides before this reader is used." }]);
      steps.push([150, () => {
        const e = emit(t, { type: "request_deploy", interrupt_id: `int-${t.id}`, name: NAME, version,
          summary: `Use the learned reader for ${page.site} (parser ${pid}) from v${version}?`,
          spec_diff: `+  - id: ${pid}\n+    match: {site: ${page.site}}\n+    origin: {kind: learned, thread_id: ${t.id}}\n` + fields.map((f) => `+    ${f.name}: ${JSON.stringify(f.extractor)}`).join("\n") + "\n",
          code_diff: "", tests: { passed: true, summary: V.versions.find((v) => v.version === version)?.tests_summary ?? "passed" },
          spec_patch: [{ op: "add", path: "/extract/parsers/-", value: { id: pid, match: { site: page.site } } }] });
        t.pending = e;
        emit(t, { type: "status", state: "waiting", mode: "learn" });
      }]);
    }
    play(t, steps);
    return t.id;
  }

  function finishLearn(t, approvedBy) {
    const prev = deployVersion(t.version);
    addLayout(t.parserId, { kind: "learned", at: h.nowIso(), thread_id: t.id, approved_by: approvedBy }, t.version);
    layoutOf(t.parserId).active = true;
    emit(t, { type: "deployed", name: NAME, version: t.version, previous_version: prev });
    // the page that started the thread carries its model calls; pages that waited for it are plain code rows
    const first = V.rows.get(t.pageId);
    if (first) first.ai_calls = t.modelCalls;
    play(t, [
      [200, { type: "message", agent: "supervisor", text: `Replaying ${t.waiting.size} held page${t.waiting.size === 1 ? "" : "s"} on v${t.version}.` }],
      [0, () => enqueue([...t.waiting])],
      [200, { type: "done", result: { model_calls: t.modelCalls, seconds: Math.round((Date.now() - t.startedMs) / 100) / 10, page_id: t.pageId, parser_id: t.parserId, site: t.site, version: t.version, deployed: true, approval: approvedBy } }],
      [50, { type: "status", state: "done", mode: "learn" }],
    ]);
  }

  function startRepair(inc) {
    const t = newThread("repair", String(inc.id), { incidentId: inc.id, modelCalls: calls(4), startedMs: Date.now() });
    const { from, to } = inc.renamed;
    const version = addVersion(activeParsers(), to);
    t.version = version;
    play(t, [
      [100, { type: "status", state: "running", mode: "repair" }],
      [200, { type: "todos", todos: [
        { content: "Read the incident and the live form", status: "in_progress" },
        { content: "Map the changed question", status: "pending" },
        { content: "Compile, test, ask for approval", status: "pending" },
      ] }],
      [250, { type: "subagent_start", name: "repairer", task: `Incident #${inc.id}: CONTRACT_CHANGED on the tracker form` }],
      [300, { type: "tool_call", agent: "repairer", tool: "get_incident", args: { incident_id: inc.id } }],
      [300, { type: "tool_result", agent: "repairer", tool: "get_incident", ok: true, summary: inc.summary }],
      [300, { type: "tool_call", agent: "repairer", tool: "fetch_contract", args: { name: NAME } }],
      [400, { type: "tool_result", agent: "repairer", tool: "fetch_contract", ok: true, summary: `form questions now: ${V.form.join(", ")}` }],
      [400, { type: "message", agent: "repairer", text: `"${from}" was renamed to "${to}": same position, same short-answer type. Proposing to send company to "${to}".` }],
      [200, { type: "subagent_end", name: "repairer", summary: `move sink.fields.${from} -> sink.fields.${to}; golden examples migrated` }],
      [200, { type: "subagent_start", name: "builder", task: `Compile ${NAME} v${version} and run its tests` }],
      [250, { type: "tool_call", agent: "builder", tool: "compile_spec", args: { name: NAME, version } }],
      [300, { type: "tool_result", agent: "builder", tool: "compile_spec", ok: true, summary: `compiled ${NAME} v${version}` }],
      [250, { type: "tool_call", agent: "builder", tool: "run_tests", args: { name: NAME, version } }],
      [350, { type: "tool_result", agent: "builder", tool: "run_tests", ok: true, summary: V.versions.find((v) => v.version === version).tests_summary }],
      [150, { type: "subagent_end", name: "builder", summary: `v${version}: tests passed against the live form` }],
      [200, () => {
        const e = emit(t, { type: "request_deploy", interrupt_id: `int-${t.id}`, name: NAME, version,
          summary: `Send company to the form question "${to}" instead of "${from}" (v${version}).`,
          spec_diff: `--- spec.yaml (active v${V.active})\n+++ spec.yaml (v${version})\n@@ -14,7 +14,7 @@\n   fields:\n     Email:        {const: demo@example.com}\n     Job Title:    {from: title}\n-    ${from}:      {from: company}\n+    ${to}:     {from: company}\n     Link:         {from: record.canonical_url}\n     Date Applied: {from: record.captured_date}\n`,
          code_diff: "", tests: { passed: true, summary: V.versions.find((v) => v.version === version).tests_summary },
          spec_patch: [{ op: "move", from: `/sink/fields/${from}`, path: `/sink/fields/${to}` }] });
        t.pending = e;
        emit(t, { type: "status", state: "waiting", mode: "repair" });
      }],
    ]);
    return t.id;
  }

  function resume(t, body) {
    const pending = t.pending;
    if (!pending || pending.interrupt_id !== body.interrupt_id) return [409, `no pending interrupt ${body.interrupt_id} on ${t.id}`];
    if (!["approve", "reject"].includes(body.decision)) return [422, "request_deploy needs a decision: approve or reject"];
    t.pending = null;
    emit(t, { type: "resumed", interrupt_id: pending.interrupt_id, payload: { decision: body.decision, comment: body.comment ?? "" } });
    emit(t, { type: "status", state: "running", mode: t.mode });
    if (body.decision !== "approve") {
      play(t, [
        [200, { type: "message", agent: "supervisor", text: `Not deployed: ${body.comment || "rejected by a person"}. The held pages stay held.` }],
        [100, { type: "done", result: { model_calls: t.modelCalls, deployed: false, version: pending.version, approval: "human" } }],
        [50, { type: "status", state: "done", mode: t.mode }],
      ]);
      return [200, status(t)];
    }
    if (t.mode === "learn") {
      h.timer(300, () => finishLearn(t, "human"));
      return [200, status(t)];
    }
    // form repair
    h.timer(300, () => {
      const prev = deployVersion(t.version);
      const inc = V.incidents.find((i) => i.id === t.incidentId);
      if (inc) inc.status = "resolved";
      emit(t, { type: "deployed", name: NAME, version: t.version, previous_version: prev });
      const held = [...V.rows.values()].filter((r) => r.reason === "CONTRACT_CHANGED" && r.status === "held").sort((a, b) => a.seq - b.seq).map((r) => r.page_id);
      play(t, [
        [200, { type: "message", agent: "supervisor", text: `Replaying ${held.length} held page${held.length === 1 ? "" : "s"} on v${t.version}.` }],
        [0, () => enqueue(held)],
        [200, { type: "done", result: { model_calls: t.modelCalls, deployed: true, version: t.version, approval: "human" } }],
        [50, { type: "status", state: "done", mode: "repair" }],
      ]);
    });
    return [200, status(t)];
  }
  const status = (t) => ({ thread_id: t.id, mode: t.mode, subject: t.subject, state: t.state, pending: t.pending, last_seq: t.events.length, created_at: t.created_at });

  // ---------------------------------------------------------------- seed: mid-demo, every label on screen
  function seed() {
    const today = new Date();
    const at = (hh, mm) => new Date(today.getFullYear(), today.getMonth(), today.getDate(), hh, mm).toISOString().replace(/\.\d+Z$/, "+00:00");
    const P = POSTINGS;
    const press = ([url, title], hhmm) => {
      const p = ingest({ url, title, html: cannedHtml(url, title) }, { at: at(...hhmm) });
      return V.rows.get(p.id);
    };
    const ok = (row) => {
      V.runId++;
      processRow(row);
      return row;
    };
    ok(press(P.lever[0], [11, 2]));
    ok(press(P.greenhouse[0], [11, 3]));
    ok(press(P.branded[0], [11, 4]));
    // the Ashby layout was learned from this page (thread learn-3f9a2c1b0d, auto-approved)
    const ash = press(P.ashby[0], [11, 5]);
    const page = pageOf(ash.page_id);
    const t = newThread("learn", page.id, { site: page.site, pageId: page.id, parserId: "ashby", modelCalls: calls(5) });
    V.threads.delete(t.id);
    t.id = "learn-3f9a2c1b0d";
    t.created_at = at(11, 5);
    V.threads.set(t.id, t);
    const fields = evidenceFor(page, "ashby");
    for (const [, ev] of learnSteps(t, page, "ashby", 3, fields)) if (typeof ev !== "function") emit(t, ev);
    emit(t, { type: "learned", integration: NAME, parser_id: "ashby", site: page.site, version: 3, deployed: true, approval: "auto",
      reason: "Every value was found on the page and corroborated by its title or metadata, nothing was ambiguous, the full suite passed and the change only adds one parser." });
    emit(t, { type: "deployed", name: NAME, version: 3, previous_version: 2 });
    emit(t, { type: "done", result: { model_calls: t.modelCalls, seconds: 6.4, page_id: page.id, parser_id: "ashby", site: page.site, version: 3, deployed: true, approval: "auto" } });
    emit(t, { type: "status", state: "done", mode: "learn" });
    for (const e of t.events) e.t = at(11, 5).replace("+00:00", "Z");
    ash.learn_thread_id = t.id;
    ok(ash);
    ash.ai_calls = t.modelCalls;
    ok(press(P.ashby[1], [11, 6]));
    ok(press([P.lever[0][0].replace("LinkedIn", "Indeed") + "#apply", P.lever[0][1]], [11, 8]));
    // AI was off for this one: held, nothing learned
    const ai = h.settings().ai_enabled;
    h.settings().ai_enabled = false;
    ok(press(P.workable[0], [11, 10]));
    h.settings().ai_enabled = ai;
    // then someone renamed "Company" in the form
    V.form = V.form.map((l) => (l === "Company" ? "Employer" : l));
    ok(press(P.lever[1], [11, 14]));
  }

  baseState();
  baseline = snapshot();
  seed();

  // ---------------------------------------------------------------- bench v2 (illustrative; the real file wins)
  function benchV2() {
    const plan = [];
    const order = ["lever", "lever", "greenhouse", "lever", "greenhouse", "ashby", "lever", "ashby", "greenhouse", "lever", "branded", "ashby", "greenhouse", "lever", "ashby", "greenhouse", "workable"];
    for (let i = 0; i < 60; i++) plan.push(order[i] ?? ["lever", "greenhouse", "ashby", "branded", "workable"][(i * 7) % 5]);
    const seen = new Set();
    let pw = 0, ba = 0;
    const postings = [], pws = [], bas = [];
    plan.forEach((p, i) => {
      const site = { lever: "jobs.lever.co", greenhouse: "boards.greenhouse.io", ashby: "jobs.ashbyhq.com", branded: "careers.airbnb.com", workable: "apply.workable.com" }[p];
      const platform = p === "branded" ? "greenhouse" : p;
      const isNew = !seen.has(platform);
      seen.add(platform);
      const ai_calls = isNew ? [6, 5, 7, 6][seen.size - 1] ?? 6 : 0;
      pw += ai_calls;
      ba += 4;
      postings.push({ n: i + 1, file: `fixtures/postings/${platform}/${i + 1}.html.gz`, site, label: isNew ? "learned_now" : "code", ai_calls, seconds: isNew ? 8.6 : 1.7, correct: i !== 37 });
      pws.push(pw);
      bas.push(ba);
    });
    const src = "web/mock/v2.mjs (illustrative mock data, not a measurement)";
    return {
      mock: true,
      generated_at: h.nowIso(),
      postings,
      series: { patchwork_cumulative_ai_calls: pws, browser_agent_cumulative_ai_calls: bas },
      numbers: [
        { name: "Postings replayed", value: 60, unit: "postings", kind: "measured", source: src },
        { name: "Patchwork AI calls, total", value: pw, unit: "calls", kind: "measured", source: src },
        { name: "Patchwork AI calls after the 4th layout", value: 0, unit: "calls", kind: "measured", source: src },
        { name: "Accuracy vs answer key", value: 59 / 60, unit: "ratio", kind: "measured", source: src },
        { name: "Seconds per posting, known layout", value: 1.7, unit: "s", kind: "measured", source: src },
        { name: "Seconds per posting, learned now", value: 8.6, unit: "s", kind: "measured", source: src },
        { name: "Browser agent AI calls per posting", value: 4, unit: "calls", kind: "estimate", source: `${src}; assumption: read page + find fields + fill form + confirm` },
        { name: "Browser agent AI calls, total", value: ba, unit: "calls", kind: "estimate", source: src },
      ],
    };
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
  const noHtml = ({ html, ...p }) => p; // eslint-disable-line no-unused-vars

  /** Returns true when the request was handled. */
  function handle(req, res, url, M, p, body) {
    const { send, fail } = h;
    let mm;
    if (p === "/api/pages" && M === "POST") {
      if (req.headers["x-patchwork-token"] !== TOKEN) return fail(res, 401, "bad or missing X-Patchwork-Token"), true;
      if (typeof body.url !== "string" || typeof body.html !== "string") return fail(res, 422, "url, title and html are required"), true;
      if (body.html.length > 5 * 1024 * 1024) return fail(res, 413, "html is larger than 5 MB"), true;
      const page = ingest(body);
      const queued = V.busy;
      enqueue([page.id]);
      return send(res, 201, { id: page.id, seq: page.seq, site: page.site, canonical_url: page.canonical_url, integration: NAME, run_id: V.runId, queued }), true;
    }
    if (p === "/api/pages" && M === "GET") {
      const since = Number(url.searchParams.get("since") || 0);
      const limit = Number(url.searchParams.get("limit") || 100);
      return send(res, 200, V.pages.filter((x) => x.seq > since).slice(0, limit)), true;
    }
    if ((mm = p.match(/^\/api\/pages\/([^/]+)$/)) && M === "GET") {
      const pg = pageOf(decodeURIComponent(mm[1]));
      return pg ? send(res, 200, { page: noHtml(pg), row: V.rows.get(pg.id) }) : fail(res, 404, `page ${mm[1]} not found`), true;
    }
    if (p === "/api/extension" && M === "GET") return send(res, 200, { token: TOKEN, control_url: h.base() }), true;
    if (p === "/api/tracker" && M === "GET") {
      const limit = Number(url.searchParams.get("limit") || 50);
      const rows = [...V.rows.values()].sort((a, b) => b.seq - a.seq);
      // V15 item 3: `fields` per row (what the reader extracted; {} before a reader ran) and `field_names`
      const withFields = rows.slice(0, limit).map((r) => ({ ...r, fields: r.parser ? { title: r.title, company: r.company } : {} }));
      return send(res, 200, { rows: withFields, totals: totals(rows), field_names: ["title", "company"] }), true;
    }
    if (p === "/api/layouts" && M === "GET") return send(res, 200, V.layouts), true;
    if ((mm = p.match(/^\/api\/layouts\/([^/]+)\/revert$/)) && M === "POST") {
      const pid = decodeURIComponent(mm[1]);
      const active = activeParsers();
      if (!active.includes(pid)) return fail(res, 404, `parser ${pid} is not in the active version`), true;
      if (active.length === 1) return fail(res, 409, "refusing to remove the last parser"), true;
      const v = addVersion(active.filter((x) => x !== pid), specCompany());
      deployVersion(v);
      return send(res, 200, { version: v, deployed: true }), true;
    }
    if (p === "/api/demo/form/rename" && M === "POST") {
      if (!V.form.includes(body.from)) return fail(res, 422, `the form has no question "${body.from}"`), true;
      V.form = V.form.map((l) => (l === body.from ? String(body.to) : l));
      return send(res, 200, { labels: V.form }), true;
    }
    if (p === "/api/demo/v2/save-baseline" && M === "POST") {
      baseline = snapshot();
      return send(res, 200, { ok: true, active_version: V.active }), true;
    }
    if (p === "/api/demo/v2/reset" && M === "POST") {
      reset();
      return send(res, 200, { ok: true, active_version: V.active }), true;
    }
    if (p === "/api/bench/v2" && M === "GET") {
      const real = h.readRepoJson("bench/v2/results.json");
      return send(res, 200, real ?? benchV2()), true;
    }
    if ((mm = p.match(/^\/api\/integrations\/jobs_to_tracker(\/.*)?$/))) {
      const sub = mm[1] ?? "";
      if (M === "POST" && sub === "/replay") {
        const held = [...V.rows.values()].filter((r) => r.status === "held").sort((a, b) => a.seq - b.seq).map((r) => r.page_id);
        enqueue(held);
        return send(res, 200, { run_id: V.runId + 1 }), true;
      }
      if (M === "GET" && !sub)
        return send(res, 200, { name: NAME, active_version: V.active,
          versions: V.versions.map(({ version, created_at, deployed_at, tests_passed, tests_summary }) => ({ version, created_at, deployed_at, tests_passed, tests_summary })),
          recent_runs: [], open_incidents: V.incidents.filter((i) => i.status === "open") }), true;
      let vm;
      if (M === "GET" && (vm = sub.match(/^\/versions\/(\d+)$/))) {
        const v = V.versions.find((x) => x.version === Number(vm[1]));
        if (!v) return fail(res, 404, `${NAME} v${vm[1]} does not exist`), true;
        const spec_yaml = [
          "spec_version: 1", `name: ${NAME}`, "intent: \"Add the job posting I'm looking at to my application tracker\"",
          "sink:", "  kind: form", `  url: ${FORM_URL}`, "  fields:",
          "    Email: {const: demo@example.com}", "    Job Title: {from: title}", `    ${v.companyLabel}: {from: company}`,
          "    Link: {from: record.canonical_url}", "    Date Applied: {from: record.captured_date}", "",
        ].join("\n");
        return send(res, 200, { name: NAME, version: v.version, active: v.version === V.active, created_at: v.created_at, deployed_at: v.deployed_at,
          manifest: { name: NAME, version: v.version, parsers: v.parsers }, spec_yaml, files: {}, tests_passed: v.tests_passed, tests_summary: v.tests_summary }), true;
      }
      return fail(res, 404, `mock: ${M} ${p} is not simulated for ${NAME}`), true;
    }
    if ((mm = p.match(/^\/api\/incidents\/(\d+)(\/repair)?$/))) {
      const inc = V.incidents.find((i) => i.id === Number(mm[1]));
      if (!inc) return false;
      if (M === "GET" && !mm[2]) return send(res, 200, inc), true;
      if (M === "POST" && mm[2]) {
        if (!h.settings().ai_enabled) return fail(res, 409, "AI is off. Turn AI on to repair."), true;
        if (inc.status !== "open") return fail(res, 409, `incident ${inc.id} is ${inc.status}`), true;
        return send(res, 200, { thread_id: startRepair(inc) }), true;
      }
    }
    if ((mm = p.match(/^\/api\/agent\/([^/]+)(\/[a-z]+)?$/))) {
      const t = V.threads.get(decodeURIComponent(mm[1]));
      if (!t) return false;
      if (M === "GET" && !mm[2]) return send(res, 200, status(t)), true;
      if (M === "GET" && mm[2] === "/stream")
        return h.sse(req, res, url, {
          listeners: t.listeners,
          items: () => t.events.map((e) => ({ id: e.seq, event: e.type, data: e })),
          terminal: (x) => x.event === "status" && (x.data.state === "done" || x.data.state === "failed"),
          isOver: () => t.state === "done" || t.state === "failed",
        }), true;
      if (M === "POST" && mm[2] === "/resume") {
        const [code, out] = resume(t, body);
        return (code === 200 ? send(res, 200, out) : fail(res, code, out)), true;
      }
    }
    return false;
  }

  return {
    handle,
    threads: () => [...V.threads.values()].map(status),
    incidents: () => V.incidents,
    integration: () => ({ name: NAME, active_version: V.active, latest_version: V.versions.at(-1)?.version ?? null, open_incidents: V.incidents.filter((i) => i.status === "open").length }),
  };
}
