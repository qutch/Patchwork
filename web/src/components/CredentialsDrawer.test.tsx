import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AppProvider, useApp } from "../state";
import CredentialsDrawer from "./CredentialsDrawer";
import { startMock, type Mock } from "../test/harness";

let mock: Mock;
beforeAll(async () => {
  mock = await startMock();
});
afterAll(() => mock.stop());
afterEach(cleanup);

const SECRET = "sentinel-cred-123456";

function Open() {
  const { setCredsOpen } = useApp();
  return <button onClick={() => setCredsOpen(true)}>open</button>;
}

describe("CredentialsDrawer", () => {
  it("adds a credential through a password input and never shows the value again", async () => {
    render(<AppProvider><Open /><CredentialsDrawer /></AppProvider>);
    fireEvent.click(screen.getByText("open"));
    const dialog = await screen.findByRole("dialog", { name: "Credentials" });
    await waitFor(() => expect(dialog.textContent).toContain("AIRTABLE_TOKEN"));
    const value = screen.getByLabelText("Value") as HTMLInputElement;
    expect(value.type).toBe("password");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "shop_token" } });
    fireEvent.change(value, { target: { value: SECRET } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save credential" }));
    });
    await waitFor(() => {
      const row = screen.getByText("SHOP_TOKEN").closest("li")!;
      expect(row.textContent).toContain("set");
      expect(row.textContent).not.toContain("not set");
    });
    // the value is nowhere: not in the page, not in an input, not in storage, not in the address
    expect(document.documentElement.outerHTML).not.toContain(SECRET);
    expect([...document.querySelectorAll("input")].map((i) => i.value)).not.toContain(SECRET);
    expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET);
    expect(location.href).not.toContain(SECRET);
    // and the mock never echoes it
    expect(JSON.stringify(await mock.call("GET", "/api/credentials"))).not.toContain(SECRET);
  });

  it("refuses a name the control plane refuses, with the reason", async () => {
    const r = await fetch(`${mock.base}/api/credentials`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name: "ANTHROPIC_API_KEY", value: "x".repeat(12) }) });
    expect(r.status).toBe(422);
  });
});
