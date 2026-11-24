import "../styles/panels.css"; import "../styles/management.css";
import { useEffect, useState } from "react";
import { requestJson } from "../lib/api";

export type ScaleLevel = { level: number; valve_percent: number; minutes: number; air: number | null; lift: number | null;
  step_k: number | null; return_temperature: number | null; flow_temperature: number | null };
export type ScaleFan = { fan_percent: number; minutes: number; levels: ScaleLevel[] };
export type ScaleResult = { hours: number; settle_minutes: number; min_points: number; before_source: string | null; fans: ScaleFan[] };

const PERIODS: [number, string][] = [[24, "24 timer"], [72, "3 døgn"], [168, "7 døgn"], [720, "30 døgn"]];
const SOURCES: Record<string, string> = {
  measured: "målt med 1-Wire-føleren før fladen (T2)",
  estimated: "anslået ud fra ude-, udsugnings- og afkasttemperatur (T1, T3, T4)",
  mixed: "dels målt (T2), dels anslået (T1, T3, T4)",
};
const k = (n: number | null, digits = 1, unit = "") => n === null ? "–" : `${n.toLocaleString("da-DK", { minimumFractionDigits: digits, maximumFractionDigits: digits })}${unit}`;

/** View-only: what one HAC1 level (10 % valve) is worth in kelvin at each fan level, measured in the stored history. */
export function AfterheatScale() {
  const [hours, setHours] = useState(168);
  const [result, setResult] = useState<ScaleResult | null>(null);
  const [fan, setFan] = useState<number | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setError("");
    requestJson<ScaleResult>(`/api/afterheat/scale?hours=${hours}`, { timeoutMs: 20000 })
      .then(value => { if (active) setResult(value); })
      .catch(e => { if (active) { setResult(null); setError(e instanceof Error ? e.message : "Kunne ikke hente analysen"); } });
    return () => { active = false; };
  }, [hours]);
  const fans = result?.fans ?? [];
  const shown = fans.find(item => item.fan_percent === fan) ?? fans[0];
  const rows = shown ? shown.levels.filter(level => level.minutes > 0) : [];
  return <article className="surface panel-card" aria-label="Eftervarmens trinstørrelse">
    <div className="pro-card-head compact"><div><h2>Hvad giver ét trin på eftervarmen?</h2>
      <p>Løft over varmefladen og returvand pr. HAC1-trin (0–10 = 0–100 % ventil), målt i historikken ved hver blæserhastighed.</p></div></div>
    <div className="diag-actions">
      <label>Periode<select value={hours} onChange={e => setHours(Number(e.target.value))}>
        {PERIODS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      {fans.length > 0 && <label>Blæser (indblæsning)<select aria-label="Blæserhastighed" value={shown?.fan_percent} onChange={e => setFan(Number(e.target.value))}>
        {fans.map(item => <option key={item.fan_percent} value={item.fan_percent}>{item.fan_percent} % · {item.minutes} min</option>)}
      </select></label>}
    </div>
    {error && <p role="alert">{error}</p>}
    {result && !rows.length && <p>Ingen trin har været holdt længe nok i perioden. Vælg en længere periode.</p>}
    {rows.length > 0 && <div className="settings-table-wrap"><table className="settings-table" aria-label="Trinstørrelse pr. trin">
      <thead><tr><th>Trin</th><th>Ventil</th><th>Minutter</th><th>Luft efter fladen</th><th>Løft</th><th>Trinstørrelse</th><th>Returvand</th><th>Fremløb</th></tr></thead>
      <tbody>{rows.map(level => <tr key={level.level}>
        <td>{level.level}</td><td>{level.valve_percent} %</td><td>{level.minutes}</td>
        <td>{k(level.air, 1, " °C")}</td><td>{k(level.lift, 1, " K")}</td>
        <td>{level.step_k === null ? "–" : `${level.step_k > 0 ? "+" : ""}${k(level.step_k, 1, " K/trin")}`}</td>
        <td>{k(level.return_temperature, 1, " °C")}</td><td>{k(level.flow_temperature, 1, " °C")}</td>
      </tr>)}</tbody>
    </table></div>}
    {result && <p className="hx-note">Kun minutter, hvor trin og blæser har været uændret i mindst {result.settle_minutes} minutter, og trin med mindst {result.min_points} sådanne minutter. Luften før fladen er {SOURCES[result.before_source ?? ""] ?? "ukendt, så løftet kan ikke beregnes"}. Trinstørrelsen er løftets ændring fra nærmeste lavere trin med data. Fladen er ikke altid faldet helt til ro efter få minutter, og fremløbet svinger, så store spring kan vises for små. Ændres ventiler eller RLV, så vælg en periode efter ændringen.</p>}
    <details className="hx-note">
      <summary>Reference: RLV-skala målt i november 2026</summary>
      <p>Målt på et HCH5 med Danfoss RLV 15 på returen og helt åbne kuglehaner, fremløb ca. 40–50 °C og ude 0–6 °C. Løft = luft efter fladen minus luft før fladen.</p>
      <ul>
        <li>Fuld blæser (65 %), trin 10: ¼ omgang ca. 2,7 K · 1 omgang ca. 5,3 K · 2 omgange ca. 5,5 K · 3 omgange ca. 7,4 K · 3¼ omgang ca. 7,8 K.</li>
        <li>Normal blæser (36 %), 3¼ omgang: trin 0→1 ca. 2 K, trin 1→2 ca. 8–10 K, returvand 27–29 °C, udsving 1,6–2,5 K.</li>
        <li>Kuglehanerne droslet i stedet: trin 3 ≈ 21 °C og trin 5 ≈ 23,4 °C ved 36 % blæser (udsving 0,3–0,4 K, returvand ca. 21 °C), men ved 50 % blæser skulle der trin 9–10 til.</li>
        <li>Ventilen er hurtigt åbnende: næsten al varmen kommer i de første trin. 2 omgange blev valgt som kompromis (mindre spring, ca. 5,5 K ved fuld blæser).</li>
      </ul>
    </details>
  </article>;
}
