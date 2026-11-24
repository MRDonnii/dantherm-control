import { useState } from "react";
import { postJson } from "../lib/api";
import { useSession } from "../lib/session";
import { HistoryChart } from "./HistoryChart";
type Result = { points: Record<string, number>[]; covered_minutes: number; different_minutes: number; humidity_available: boolean; note: string };
export function ControlReplay({ co2Default, rhDefault, csrf }: { co2Default: number; rhDefault: number; csrf: string }) {
  const { can } = useSession();
  const [co2, setCo2] = useState<string | null>(null), [rh, setRh] = useState<string | null>(null);
  const [range, setRange] = useState("24h"), [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setError(""); setResult(null);
    try { setResult(await postJson<Result>("/api/controller/replay", { range, settings: { co2_setpoint: Number(co2 ?? co2Default), rh_setpoint: Number(rh ?? rhDefault) } }, csrf)); }
    catch (e) { setError(e instanceof Error ? e.message : "Prøvekørsel mislykkedes"); }
    finally { setBusy(false); }
  };
  return <article className="surface panel-card" aria-label="Prøvekør Local Auto">
    <div className="pro-card-head compact"><div><h2>Prøvekør Local Auto</h2><p>Sammenlign grænser på gemte målinger, før du ændrer styringen.</p></div></div>
    <div className="diag-actions">
      <label>CO₂-grænse (ppm)<input type="number" min="400" max="2000" step="50" disabled={busy} value={co2 ?? co2Default} onChange={e => { setCo2(e.target.value); setResult(null); }}/></label>
      <label>Fugtgrænse (%)<input type="number" min="20" max="80" disabled={busy} value={rh ?? rhDefault} onChange={e => { setRh(e.target.value); setResult(null); }}/></label>
      <label>Periode<select value={range} disabled={busy} onChange={e => { setRange(e.target.value); setResult(null); }}><option value="1h">1 time</option><option value="6h">6 timer</option><option value="24h">24 timer</option></select></label>
      <button className="primary-action" disabled={busy || !csrf || !can("configure") || !Number(co2 ?? co2Default) || !Number(rh ?? rhDefault)} onClick={() => void run()}>{busy ? "Prøvekører…" : "Sammenlign uden at ændre"}</button>
    </div>
    {error && <p role="alert">{error}</p>}
    {result && <><p>{result.covered_minutes.toLocaleString("da-DK")} min med data · forskelligt trin i {result.different_minutes.toLocaleString("da-DK")} min.</p>
      {result.points.length > 0 ? <HistoryChart title="Local Auto: nuværende mod forslag" samples={result.points} unit=" trin" minSpan={3} series={[{ key: "current_level", label: "Nuværende indstillinger", slot: 1 }, { key: "proposed_level", label: "Forslag", slot: 2 }]}/> : <p>Ingen egnede historikpunkter i perioden.</p>}
      {!result.humidity_available && <p>Perioden mangler gemt fugtighed. Sammenligningen bruger kun CO₂.</p>}
      <p className="hx-note">{result.note} Starten af perioden antager normaltrin; korte udsving kan være skjult i gemte gennemsnit.</p></>}
  </article>;
}
