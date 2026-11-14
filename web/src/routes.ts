// Hash routes: the Integrations home, one page per integration (sections), the New integration wizard.
// The old seven tab hashes still work: `parseHash` redirects each to its new route.
import { TRACKER } from "./api";

export const V1 = "po_to_stockroom";

export type Section = "rows" | "needs" | "layouts" | "versions" | "activity" | "setup";
export const SECTIONS: { id: Section; label: string }[] = [
  { id: "rows", label: "Rows" },
  { id: "needs", label: "Needs you" },
  { id: "layouts", label: "Layouts" },
  { id: "versions", label: "Versions" },
  { id: "activity", label: "Activity" },
  { id: "setup", label: "Setup" },
];
const SECTION_IDS = new Set<string>(SECTIONS.map((s) => s.id));

export type Route =
  | { page: "home" }
  /** `mode: "show"` is Plan B's "Show it once" (until then the old Record view); `draft` resumes a wizard */
  | { page: "new"; mode?: "show"; draft?: string }
  | { page: "integration"; name: string; section: Section }
  | { page: "bench" };

/** Old tab hash -> new route. `current` is the integration the old Agent tab followed. */
function legacy(id: string, current: string): Route | null {
  switch (id) {
    case "tracker": return { page: "integration", name: TRACKER, section: "rows" };
    case "agent": return { page: "integration", name: current, section: "activity" };
    case "review": return { page: "integration", name: V1, section: "setup" };
    case "integration": return { page: "integration", name: V1, section: "versions" };
    case "runs": return { page: "integration", name: V1, section: "rows" };
    case "bench": return { page: "bench" };
    case "record": return { page: "new", mode: "show" };
    default: return null;
  }
}

export function parseHash(h: string, current: string = TRACKER): Route {
  const raw = h.replace(/^#/, "");
  const old = legacy(raw, current);
  if (old) return old;
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map((s) => {
    try { return decodeURIComponent(s); } catch { return s; }
  });
  if (parts[0] === "new") {
    const q = new URLSearchParams(query);
    const draft = q.get("draft");
    return { page: "new", ...(q.get("mode") === "show" ? { mode: "show" as const } : {}), ...(draft ? { draft } : {}) };
  }
  if (parts[0] === "bench") return { page: "bench" };
  if (parts[0] === "integrations" && parts[1]) {
    const section = SECTION_IDS.has(parts[2]) ? (parts[2] as Section) : "rows";
    return { page: "integration", name: parts[1], section };
  }
  return { page: "home" };
}

export function toHash(r: Route): string {
  switch (r.page) {
    case "home": return "#/";
    case "bench": return "#/bench";
    case "new": {
      const q = new URLSearchParams();
      if (r.mode) q.set("mode", r.mode);
      if (r.draft) q.set("draft", r.draft);
      const s = q.toString();
      return `#/new${s ? `?${s}` : ""}`;
    }
    case "integration": return `#/integrations/${encodeURIComponent(r.name)}/${r.section}`;
  }
}
