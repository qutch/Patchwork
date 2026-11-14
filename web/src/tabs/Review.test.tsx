import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { Candidates } from "../api";
import data from "../../mock/data.json";
import { MappingTable, fieldClass } from "./Review";

describe("MappingTable", () => {
  it("highlights each mapping's provenance in the source text", () => {
    const { container } = render(<MappingTable c={data.candidates as unknown as Candidates} />);
    const marks = [...container.querySelectorAll("mark")].map((m) => [m.className, m.textContent]);
    expect(marks).toEqual([
      ["f-po", "PO-1022"],
      ["f-sku", "GAD-217"],
      ["f-qty", "20"],
    ]);
    expect(screen.getByText("Qty:\\s*(\\d+) → int")).toBeTruthy();
    expect(screen.getByText("Purchase Order PO-1022")).toBeTruthy();
  });

  it("flags unexplained destination values", () => {
    const c = { ...(data.candidates as unknown as Candidates), unexplained: [{ dest_field: "warehouse_id", value: "BOS" }] };
    render(<MappingTable c={c} />);
    expect(screen.getByText("warehouse_id")).toBeTruthy();
    expect(screen.getByText(/wasn't found anywhere in the source/)).toBeTruthy();
  });

  it("colours fields by meaning", () => {
    expect(fieldClass("quantity_delta", 0)).toBe("f-qty");
    expect(fieldClass("sku", 0)).toBe("f-sku");
    expect(fieldClass("reference", 0)).toBe("f-po");
  });
});
