// The page integration picker, where the chosen integration writes, and credential status (contracts V13-V15).
// Credentials are shown by name only: the API never sends a value and this UI never asks for one.
import type { DestinationInfo } from "../api";
import { destinationLabel, destinationWhere, humanName, isJobs, missingCredentials } from "../lib/integrations";
import { useApp } from "../state";

/** One page integration at a time. Hidden when there is only one to choose from. */
export function IntegrationPicker({ big = false, compact = false }: { big?: boolean; compact?: boolean }) {
  const { pageIntegrations, pageIntegration, setPageIntegration } = useApp();
  if (pageIntegrations.length < 2) return null;
  return (
    <label className="flex items-center gap-2 m-0 min-w-0">
      {!compact && <span className={`font-mono ${big ? "text-[14px]" : "text-xs"} text-muted`}>integration</span>}
      <select
        className={`input w-auto ${compact ? "max-w-[230px]" : "max-w-[300px]"} ${big ? "text-[15px]" : "text-[13px]"}`}
        value={pageIntegration}
        onChange={(e) => setPageIntegration(e.target.value)}
        aria-label="Page integration"
        title={pageIntegration}
      >
        {pageIntegrations.map((i) => (
          <option key={i.name} value={i.name}>{i.intent?.trim() || humanName(i.name)}</option>
        ))}
      </select>
    </label>
  );
}

/** "→ Airtable · table Applications · base app…", with the credential's status when the sink names one. */
export function DestinationBadge({ name, d, big = false }: { name: string; d: DestinationInfo | null; big?: boolean }) {
  const { credentials } = useApp();
  if (!d) return null;
  const where = destinationWhere(d);
  const missing = missingCredentials(credentials, name, d);
  const cred = d.credential;
  const credKnown = !!cred && !!credentials?.some((c) => c.name === cred);
  return (
    <span className="pill" style={{ color: "var(--ink)", background: "var(--sunken)", fontSize: big ? 15 : undefined, whiteSpace: "normal" }}
      title={`Writes to ${destinationLabel(d)}${where ? ` (${where})` : ""}${cred ? `; credential ${cred}` : ""}`}>
      <span aria-hidden>→</span> <b>{destinationLabel(d)}</b>
      {where && <span className="font-mono text-muted" style={{ fontSize: big ? 13 : 11.5 }}>{where}</span>}
      {cred && (
        <span className="font-mono" style={{ fontSize: big ? 13 : 11.5, color: missing.includes(cred) ? "var(--bad)" : "var(--muted)" }}>
          · {cred}{missing.includes(cred) ? " not set" : credKnown ? " set" : ""}
        </span>
      )}
    </span>
  );
}

/** True unless it is the jobs demo alone on its form, which looks exactly as it always did. */
export function useShowIntegration(name: string, d: DestinationInfo | null): boolean {
  const { pageIntegrations } = useApp();
  return !(pageIntegrations.length < 2 && isJobs(name) && (!d || d.kind === "form"));
}

/** The picker and the destination together, for page heads. */
export function IntegrationBar({ name, d, big = false }: { name: string; d: DestinationInfo | null; big?: boolean }) {
  if (!useShowIntegration(name, d)) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <IntegrationPicker big={big} />
      <DestinationBadge name={name} d={d} big={big} />
    </span>
  );
}

/** A credential this integration uses is not set: say which, and how to fix it (never a value). */
export function CredentialWarning({ name, d, big = false }: { name: string; d: DestinationInfo | null; big?: boolean }) {
  const { credentials } = useApp();
  const missing = missingCredentials(credentials, name, d);
  if (!missing.length) return null;
  const list = missing.join(", ");
  return (
    <section className="rounded-md border-l-4 px-4 py-3 grid gap-1" role="alert"
      style={{ borderColor: "var(--bad)", background: "var(--bad-soft)", fontSize: big ? 19 : 14 }}>
      <div className="font-mono font-bold uppercase tracking-wider" style={{ color: "var(--bad)", fontSize: big ? 14 : 12 }}>
        Credential missing
      </div>
      <div>
        <b className="font-mono">{list}</b> {missing.length === 1 ? "is" : "are"} not set, so runs of <span className="font-mono">{name}</span>
        {d ? <> can't write to {destinationLabel(d)}</> : <> can't start</>}. Add{" "}
        {missing.map((m, i) => (
          <span key={m}>{i > 0 && ", "}<code className="font-mono">{m}=…</code></span>
        ))}{" "}
        to <code className="font-mono">.env</code> and restart the control plane. Patchwork never shows credential values.
      </div>
    </section>
  );
}
