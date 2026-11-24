import "../styles/management.css";
import { useState, type ReactNode } from "react";
import { postJson } from "../lib/api";
import { HistoryChart } from "./HistoryChart";

type Metrics = { level_changes: number; level_changes_per_hour: number | null; mean_abs_error: number | null;
  in_band_percent: number | null; max_over: number | null; max_under: number | null };
type Point = { ts: number; target: number; proposed_target: number; actual_air: number; actual_level: number;
  current_air: number; current_level: number; proposed_air: number; proposed_level: number };
export type AfterheatReplayResult = { scope: "afterheat"; points: Point[]; covered_minutes: number; frost_minutes: number;
  summary: { actual: Metrics; current: Metrics; proposed: Metrics }; live_mode: string;
  model: { steps_k: number[]; step_sources: string[]; reference_fan_percent: number }; note: string };

const SOURCE: Record<string, string> = { learned: "lært", history: "historik", prior: "standard" };
const da = (n: number | null, digits: number, unit: string) => n === null ? "–" : `${n.toLocaleString("da-DK", { minimumFractionDigits: digits, maximumFractionDigits: digits })}${unit}`;
const ROWS: [keyof Metrics, string, (n: number | null) => string][] = [
  ["level_changes_per_hour", "Trinskift pr. time", n => da(n, 1, "")],
  ["mean_abs_error", "Gns. afvigelse fra målet", n => da(n, 2, " K")],
  ["in_band_percent", "Tid inden for ±0,5 °C", n => da(n, 0, " %")],
  ["max_over", "Største oversving", n => da(n, 1, " K")],
  ["max_under", "Største undersving", n => da(n, 1, " K")],
];

/** Pure simulation of the Pi afterheat control with a proposed target, comfort band and dwell; nothing is saved or sent. */
export function AfterheatReplay({ csrf, allowed, band, dwell, liveMode, modeSelect }: { csrf: string; allowed: boolean; band: number;
  dwell: number; liveMode: string; modeSelect: ReactNode }) {
  const [offset, setOffset] = useState("0");
  const [bandDraft, setBand] = useState<string | null>(null), [dwellDraft, setDwell] = useState<string | null>(null);
  const [range, setRange] = useState("6h");
  const [result, setResult] = useState<AfterheatReplayResult | null>(null);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const proposal = { afterheat_target_offset: Number(offset), afterheat_pi_band: Number(bandDraft ?? band),
    afterheat_pi_min_dwell_ticks: Number(dwellDraft ?? dwell) };
  const valid = Number.isFinite(proposal.afterheat_target_offset) && Math.abs(proposal.afterheat_target_offset) <= 3
    && proposal.afterheat_pi_band >= 0.1 && proposal.afterheat_pi_band <= 1.5
    && Number.isInteger(proposal.afterheat_pi_min_dwell_ticks) && proposal.afterheat_pi_min_dwell_ticks >= 1 && proposal.afterheat_pi_min_dwell_ticks <= 10;
  const clear = () => setResult(null);
  const run = async () => {
    setBusy(true); setError(""); setResult(null);
    // The replay runs the control every few seconds of the period; on the Pi that can take a while.
    try { setResult(await postJson<AfterheatReplayResult>("/api/controller/replay", { mode: "afterheat", range, settings: proposal }, csrf, 120000)); }
    catch (e) { setError(e instanceof Error ? e.message : "Prøvekørsel mislykkedes"); }
    finally { setBusy(false); }
  };
  const offsetValue = Number(offset) || 0;
  return <>
    <div className="diag-actions">
      {modeSelect}
      <label>Ændring af målet (K)<input type="number" min="-3" max="3" step="0.5" disabled={busy} value={offset} onChange={e => { setOffset(e.target.value); clear(); }}/></label>
      <label>Komfortbånd (± K)<input type="number" min="0.1" max="1.5" step="0.1" disabled={busy} value={bandDraft ?? band} onChange={e => { setBand(e.target.value); clear(); }}/></label>
      <label>Mindste tid pr. trin (min)<input type="number" min="1" max="10" step="1" disabled={busy} value={dwellDraft ?? dwell} onChange={e => { setDwell(e.target.value); clear(); }}/></label>
      <label>Periode<select value={range} disabled={busy} onChange={e => { setRange(e.target.value); clear(); }}><option value="1h">1 time</option><option value="6h">6 timer</option><option value="12h">12 timer</option></select></label>
      <button className="primary-action" disabled={busy || !csrf || !allowed || !valid} onClick={() => void run()}>{busy ? "Prøvekører…" : "Sammenlign uden at ændre"}</button>
    </div>
    <p className="hx-note">Pi-styringen af eftervarmen køres i en model af varmefladen på gemte minutter, én gang med de nuværende indstillinger og én gang med forslaget. Intet gemmes, og intet sendes til anlægget. Prøven kan tage op til et halvt minut på Pi'en.</p>
    {error && <p role="alert">{error}</p>}
    {result && <>
      <p>{result.covered_minutes.toLocaleString("da-DK")} min med data{result.frost_minutes ? ` · ${result.frost_minutes.toLocaleString("da-DK")} min under frostsikring (mindst trin 1)` : ""}.</p>
      {result.live_mode !== "pi" && <p>Pi-styringen af eftervarmen er ikke slået til nu; prøven viser, hvordan den ville styre.</p>}
      {result.points.length > 0 ? <>
        <div className="settings-table-wrap"><table className="settings-table" aria-label="Eftervarme: faktisk, nuværende og forslag">
          <thead><tr><th></th><th>Faktisk</th><th>Nuværende (model)</th><th>Forslag (model)</th></tr></thead>
          <tbody>{ROWS.map(([key, label, show]) => <tr key={key}><td>{label}</td>
            <td>{show(result.summary.actual[key])}</td><td>{show(result.summary.current[key])}</td><td>{show(result.summary.proposed[key])}</td></tr>)}</tbody>
        </table></div>
        <HistoryChart title="Eftervarmens trin: faktisk, nuværende og forslag" samples={result.points} unit=" trin" minSpan={3} yDomain={{ lo: 0, hi: 10, step: 2 }}
          series={[{ key: "actual_level", label: "Faktisk", slot: 1, stepped: true }, { key: "current_level", label: "Nuværende (model)", slot: 2, stepped: true },
            { key: "proposed_level", label: "Forslag (model)", slot: 3, stepped: true }]}/>
        <HistoryChart title="Indblæsning: faktisk, nuværende og forslag" samples={result.points} unit="°C" minSpan={3}
          reference={{ key: "target", label: "Mål nu" }} command={offsetValue ? { key: "proposed_target", label: "Mål i forslaget" } : undefined}
          series={[{ key: "actual_air", label: "Faktisk", slot: 1 }, { key: "current_air", label: "Nuværende (model)", slot: 2 },
            { key: "proposed_air", label: "Forslag (model)", slot: 3 }]}/>
        <p className="hx-note">Varme pr. trin i modellen ved {result.model.reference_fan_percent} % blæser: {result.model.steps_k.map((step, index) =>
          `${index}→${index + 1} ${da(step, 1, " K")} (${SOURCE[result.model.step_sources[index]] ?? "?"})`).join(" · ")}.</p>
      </> : <p>Ingen egnede minutter i perioden: eftervarmen skal være tændt, og slukket eftervarme, køl, forsøg og ekstern master udelades.</p>}
      <p className="hx-note">{result.note}</p>
    </>}
  </>;
}
