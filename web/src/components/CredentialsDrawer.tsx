import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useApp } from "../state";

const NAME = /^[A-Z][A-Z0-9_]{1,63}$/;

/**
 * Credentials: the names Patchwork knows and whether each is set, plus a form to add one. The value goes to the
 * control plane once, through a password input, and is dropped from this component the moment it is sent. It is
 * never rendered back, and never kept in localStorage or the URL: afterwards the row only says "set".
 */
export default function CredentialsDrawer() {
  const { credsOpen, setCredsOpen, credentials, act, refresh, notify } = useApp();
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (credsOpen) nameRef.current?.focus();
    else setValue("");
  }, [credsOpen]);
  useEffect(() => {
    if (!credsOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setCredsOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [credsOpen, setCredsOpen]);
  if (!credsOpen) return null;

  const valid = NAME.test(name) && value.length >= 8;
  const save = async () => {
    const n = name;
    const v = value;
    setValue(""); // the value leaves this component before the request does
    setBusy(true);
    const r = await act(() => api.addCredential(n, v));
    setBusy(false);
    if (r) {
      setName("");
      notify("info", `${r.name} is set`);
      refresh();
    }
  };

  return (
    <div className="fixed inset-0 z-40" role="presentation">
      <div className="absolute inset-0" style={{ background: "rgba(0,0,0,.35)" }} onClick={() => setCredsOpen(false)} aria-hidden />
      <aside
        role="dialog"
        aria-label="Credentials"
        aria-modal="true"
        className="absolute right-0 top-0 bottom-0 w-[min(440px,100vw)] bg-surface border-l border-line p-5 overflow-auto grid gap-5 content-start"
      >
        <header className="flex items-start justify-between gap-3">
          <div className="grid gap-1">
            <div className="eyebrow">Credentials</div>
            <h2 className="text-[20px] font-extrabold leading-tight m-0">Keys for where Patchwork writes</h2>
          </div>
          <button className="btn" onClick={() => setCredsOpen(false)} aria-label="Close credentials">Close</button>
        </header>
        <p className="m-0 text-muted text-[13.5px]">
          A value is stored on the machine running Patchwork and never shown again. Only the name and whether it is set appear here.
        </p>

        <ul className="m-0 p-0 list-none grid gap-2" aria-label="Known credentials">
          {(credentials ?? []).length === 0 && <li className="text-muted text-sm">No credentials yet.</li>}
          {(credentials ?? []).map((c) => (
            <li key={c.name} className="flex items-center justify-between gap-3 border border-line rounded-md px-3 py-2">
              <span className="grid min-w-0">
                <span className="font-mono text-[13px] font-semibold">{c.name}</span>
                {c.used_by.length > 0 && <span className="text-[12px] text-muted truncate">used by {c.used_by.join(", ")}</span>}
              </span>
              <span className="pill" style={c.set ? { color: "var(--ok)", background: "var(--ok-soft)" } : { color: "var(--bad)", background: "var(--bad-soft)" }}>
                {c.set ? "set" : "not set"}
              </span>
            </li>
          ))}
        </ul>

        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (valid && !busy) void save();
          }}
          autoComplete="off"
        >
          <div className="eyebrow">Add or replace one</div>
          <label className="grid gap-1">
            <span className="label m-0">Name</span>
            <input ref={nameRef} className="input font-mono" value={name} placeholder="SHOP_TOKEN" autoComplete="off" spellCheck={false}
              onChange={(e) => setName(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_"))} />
          </label>
          <label className="grid gap-1">
            <span className="label m-0">Value</span>
            <input className="input font-mono" type="password" value={value} autoComplete="new-password" spellCheck={false}
              onChange={(e) => setValue(e.target.value)} />
          </label>
          <button className="btn btn-primary justify-self-start" type="submit" disabled={!valid || busy}>Save credential</button>
        </form>
      </aside>
    </div>
  );
}
