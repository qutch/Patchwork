import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReviewRow } from "../api";
import ReviewTable, { sourceLabel } from "./ReviewTable";
import { JOB_WORDS } from "../test/harness";

afterEach(cleanup);

const ROWS: ReviewRow[] = [
  { dest_field: "Product", source: "reader: h1.product-title", values: { p1: "Sony Headphones", p2: "Kindle" }, ai_proposed: true, status: "checked", alternatives: ["reader: og:title"] },
  { dest_field: "Price", source: "reader: jsonld offers.price", values: { p1: "328", p2: "149.99" }, ai_proposed: true, status: "checked", alternatives: [] },
  { dest_field: "Store", source: "constant: Acme", values: { p1: "Acme", p2: "Acme" }, ai_proposed: true, status: "needs_you", alternatives: ["reader: offers.seller.name"] },
  { dest_field: "Link", source: "the page's canonical URL", values: { p1: "https://a.example/1", p2: "https://b.example/2" }, ai_proposed: false, status: "checked", alternatives: [] },
  { dest_field: "Date Added", source: "today's date", values: { p1: "2026-11-14", p2: "2026-11-14" }, ai_proposed: false, status: "checked", alternatives: [] },
];
const PAGES = { p1: "shop-a.example.com", p2: "lumen-supply.example.org" };

describe("ReviewTable", () => {
  it("labels AI-proposed rows, shows a column per page and a needs-you status", () => {
    const { container } = render(<ReviewTable rows={ROWS} pages={PAGES} onDecide={() => {}} />);
    expect(screen.getAllByText("AI")).toHaveLength(3);
    expect(screen.getByText("shop-a.example.com")).toBeTruthy();
    expect(screen.getByText("lumen-supply.example.org")).toBeTruthy();
    expect(screen.getByText("needs you")).toBeTruthy();
    expect(screen.getByText("Sony Headphones")).toBeTruthy();
    expect(container.textContent).not.toMatch(JOB_WORDS);
  });

  it("blocks approval until the needs-you row is chosen, then sends exactly the one edit", () => {
    const onDecide = vi.fn();
    render(<ReviewTable rows={ROWS} pages={PAGES} onDecide={onDecide} />);
    const approve = screen.getByRole("button", { name: /Approve/ }) as HTMLButtonElement;
    expect(approve.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Source for Store"), { target: { value: "const" } });
    expect(approve.disabled).toBe(true); // a typed value must not be empty
    fireEvent.change(screen.getByLabelText("Value for Store"), { target: { value: "Acme" } });
    expect(approve.disabled).toBe(false);
    fireEvent.click(approve);
    expect(onDecide).toHaveBeenCalledTimes(1);
    expect(onDecide).toHaveBeenCalledWith("approve", [{ dest_field: "Store", choice: { const: "Acme" } }]);
  });

  it("sends an offered alternative as-is, and a skip", () => {
    const onDecide = vi.fn();
    render(<ReviewTable rows={ROWS} pages={PAGES} onDecide={onDecide} />);
    fireEvent.change(screen.getByLabelText("Source for Store"), { target: { value: "keep" } });
    fireEvent.change(screen.getByLabelText("Source for Product"), { target: { value: "alt:0" } });
    fireEvent.change(screen.getByLabelText("Source for Price"), { target: { value: "skip" } });
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    expect(onDecide).toHaveBeenCalledWith("approve", [
      { dest_field: "Product", choice: "reader: og:title" },
      { dest_field: "Price", choice: { skip: true } },
    ]);
  });

  it("shows a picked alternative's own preview, or says it is recomputed, never the old value", () => {
    const rows: ReviewRow[] = [{ ...ROWS[0], alternatives: [{ label: "Open Graph title", values: { p1: "OG Sony", p2: "OG Kindle" } }, "reader: jsonld name"] }];
    render(<ReviewTable rows={rows} pages={PAGES} onDecide={() => {}} />);
    expect(screen.getByText("Sony Headphones")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Source for Product"), { target: { value: "alt:0" } });
    expect(screen.getByText("OG Sony")).toBeTruthy();
    expect(screen.queryByText("Sony Headphones")).toBeNull();
    fireEvent.change(screen.getByLabelText("Source for Product"), { target: { value: "alt:1" } });
    expect(screen.queryByText("Sony Headphones")).toBeNull();
    expect(screen.getAllByText("recomputed on approve")).toHaveLength(2);
  });

  it("prefers a label over the raw source", () => {
    expect(sourceLabel({ css: "h1", label: "the page heading" })).toBe("the page heading");
  });

  it("approves with no edits when nothing changed and nothing needs you", () => {
    const onDecide = vi.fn();
    render(<ReviewTable rows={ROWS.filter((r) => r.status === "checked")} pages={PAGES} onDecide={onDecide} />);
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    expect(onDecide).toHaveBeenCalledWith("approve", []);
  });

  it("describes object sources in words", () => {
    expect(sourceLabel({ css: "h1" })).toBe("reader: h1");
    expect(sourceLabel({ record: "canonical_url" })).toBe("the page's canonical URL");
    expect(sourceLabel({ today: true })).toBe("today's date");
  });
});
