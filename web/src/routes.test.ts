import { describe, expect, it } from "vitest";
import { parseHash, toHash, type Route } from "./routes";

const ROUTES: Route[] = [
  { page: "home" },
  { page: "new" },
  { page: "new", mode: "show" },
  { page: "new", draft: "track_products_i_like" },
  { page: "bench" },
  ...(["rows", "needs", "layouts", "versions", "activity", "setup"] as const).map((section) => ({ page: "integration" as const, name: "track_product_prices", section })),
];

describe("routes", () => {
  it("round-trips every route", () => {
    for (const r of ROUTES) expect(parseHash(toHash(r))).toEqual(r);
  });

  it("redirects each old tab hash", () => {
    expect(parseHash("#tracker")).toEqual({ page: "integration", name: "jobs_to_tracker", section: "rows" });
    expect(parseHash("#agent", "track_product_prices")).toEqual({ page: "integration", name: "track_product_prices", section: "activity" });
    expect(parseHash("#review")).toEqual({ page: "integration", name: "po_to_stockroom", section: "setup" });
    expect(parseHash("#integration")).toEqual({ page: "integration", name: "po_to_stockroom", section: "versions" });
    expect(parseHash("#runs")).toEqual({ page: "integration", name: "po_to_stockroom", section: "rows" });
    expect(parseHash("#bench")).toEqual({ page: "bench" });
    expect(parseHash("#record")).toEqual({ page: "new", mode: "show" });
  });

  it("falls back to home, and to rows for an unknown section", () => {
    expect(parseHash("")).toEqual({ page: "home" });
    expect(parseHash("#/nonsense")).toEqual({ page: "home" });
    expect(parseHash("#/integrations/x")).toEqual({ page: "integration", name: "x", section: "rows" });
    expect(parseHash("#/integrations/x/zzz")).toEqual({ page: "integration", name: "x", section: "rows" });
  });
});
