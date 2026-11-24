import { useCallback, useEffect, useRef, useState } from "react";
import { requestJson } from "../lib/api";
import { usePullRefresh } from "../lib/pullRefresh";
import { InfoList } from "./InfoList";

type Update = { id: string; version: string; previous_version: string | null; at: number };
type Side = { start: number; end: number; window_minutes: number; context_minutes: number; supply_minutes: number;
  gap_minutes: number; coverage_percent: number; outdoor_min: number | null; outdoor_mean: number | null;
  outdoor_max: number | null; target_mean: number | null; cut_by_previous_update?: boolean; cut_by_next_update?: boolean; ongoing?: boolean };
type Metric = { key: string; label: string; unit: string; before: number | null; after: number | null;
  delta: number | null; verdict: string; verdict_text: string };
export type ComparisonResult = { updates: Update[]; comparison: null | { update: Update; enough: boolean;
  before: Side; after: Side; metrics: Metric[]; summary: string; note: string; settle_minutes: number; min_minutes: number };
  history?: { available?: boolean; read_error?: string | null; error?: string | null } };

const number = (value: number | null, unit = "") => value === null ? "–"
  : `${value.toLocaleString("da-DK", { maximumFractionDigits: 2 })}${unit ? ` ${unit}` : ""}`;
const when = (ts: number) => new Date(ts * 1000).toLocaleString("da-DK", { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });

/** Read-only comparison of recorded operation; never writes settings or starts a trial. */
export function UpdateComparison({ context }: { context: string }) {
  const [open, setOpen] = useState(false);
  const [update, setUpdate] = useState("");
  const [window, setWindow] = useState("24h");
  const [result, setResult] = useState<ComparisonResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const load = useCallback(async () => {
    if (!open) return;
    const request = ++generation.current;
    setBusy(true); setError("");
    const query = new URLSearchParams({ window, context });
    if (update) query.set("update", update);
    try {
      const response = await requestJson<ComparisonResult>(`/api/health/update-comparison?${query}`, { timeoutMs: 20000 });
      if (request === generation.current) setResult(response);
    } catch (e) {
      if (request === generation.current) { setResult(null); setError(e instanceof Error ? e.message : "Sammenligningen kunne ikke hentes"); }
    } finally { if (request === generation.current) setBusy(false); }
  }, [open, update, window, context]);
  useEffect(() => { void load(); return () => { generation.current += 1; }; }, [load]);
  usePullRefresh(load);
  const comparison = result?.comparison;
  const historyWarning = result?.history?.read_error ? "Historikken kunne ikke læses."
    : result?.history?.available === false ? "Historikken optager ikke lige nu; sammenligningen kan mangle data." : "";
  return <article className="surface panel-card" aria-label="Før og efter opdatering">
    <div className="pro-card-head compact"><div><h2>Før og efter opdatering</h2>
      <p>Sammenlign reguleringskvaliteten under den valgte driftstilstand. Vejr, brug og indstillinger kan også forklare forskellen.</p></div>
      <button type="button" className="secondary-action" aria-expanded={open} onClick={() => setOpen(value => !value)}>
        {open ? "Luk sammenligning" : "Vis sammenligning"}
      </button></div>
    {open && <div aria-busy={busy}>
      <div className="diag-actions">
        <label>Opdatering<select aria-label="Sammenlign opdatering" value={update || comparison?.update.id || ""}
          onChange={e => setUpdate(e.target.value)} disabled={!result?.updates?.length}>
          {!result?.updates?.length && <option value="">Ingen opdateringer endnu</option>}
          {(result?.updates ?? []).map(item => <option key={item.id} value={item.id}>
            {item.previous_version} → {item.version} · {when(item.at)}
          </option>)}
        </select></label>
        <label>Periode på hver side<select aria-label="Sammenlign periode" value={window} onChange={e => setWindow(e.target.value)}>
          <option value="24h">24 timer</option><option value="3d">3 døgn</option><option value="7d">7 døgn</option>
        </select></label>
        <button type="button" className="secondary-action" disabled={busy} onClick={() => void load()}>{busy ? "Henter…" : "Opdatér sammenligning"}</button>
      </div>
      {error && <p role="alert">{error}</p>}
      {historyWarning && <p role="alert">{historyWarning}</p>}
      {result && !comparison && !busy && <p>Der er endnu ingen registreret opdatering med en kendt tidligere version at sammenligne.</p>}
      {comparison && <>
        <p role="status">{comparison.summary}</p>
        <div className="panel-grid">
          {(["before", "after"] as const).map(key => {
            const side = comparison[key];
            return <section key={key} aria-label={key === "before" ? "Før opdateringen" : "Efter opdateringen"}>
              <h3>{key === "before" ? `Før · ${comparison.update.previous_version}` : `Efter · ${comparison.update.version}`}</h3>
              <InfoList rows={[
                { label: "Periode", value: `${when(side.start)} – ${when(side.end)}` },
                { label: "Målt mod målet", value: `${side.supply_minutes.toLocaleString("da-DK")} min` },
                { label: "Dækning i driftstilstanden", value: number(side.coverage_percent, "%") },
                { label: "Huller i historikken", value: `${side.gap_minutes.toLocaleString("da-DK")} min` },
                { label: "Ude: min / gennemsnit / maks", value: `${number(side.outdoor_min)} / ${number(side.outdoor_mean)} / ${number(side.outdoor_max, "°C")}` },
                { label: "Gennemsnitligt mål", value: number(side.target_mean, "°C") },
              ]}/>
              {(side.cut_by_previous_update || side.cut_by_next_update) && <p className="hx-note">Perioden er afgrænset af en anden opdatering.</p>}
              {side.ongoing && <p className="hx-note">Perioden efter opdateringen samler stadig data.</p>}
            </section>;
          })}
        </div>
        <div className="settings-table-wrap"><table className="settings-table" aria-label="Sammenligning af reguleringskvalitet">
          <thead><tr><th>Måling</th><th>Før</th><th>Efter</th><th>Ændring</th><th>Vurdering</th></tr></thead>
          <tbody>{comparison.metrics.map(item => <tr key={item.key}>
            <td>{item.label}</td><td>{number(item.before, item.unit)}</td><td>{number(item.after, item.unit)}</td>
            <td>{number(item.delta, item.unit)}</td><td>{item.verdict_text}</td>
          </tr>)}</tbody>
        </table></div>
        <p className="hx-note">{comparison.note} Der kræves mindst {comparison.min_minutes / 60} timer målt mod målet på hver side. Sammenligningen ændrer ingen indstillinger.</p>
      </>}
    </div>}
  </article>;
}
