import { useEffect, useState } from "react";
import { postJson, requestJson } from "../lib/api";
import { useSession } from "../lib/session";
import { HistoryChart } from "./HistoryChart";
type Result = { points: { ts: number; current_level: number; proposed_level: number; observed_level?: number | null }[]; covered_minutes: number; different_minutes: number; humidity_available: boolean; note: string };
export function ControlReplay({ co2Default, rhDefault, csrf }: { co2Default: number; rhDefault: number; csrf: string }) {
  const { can } = useSession();
  const allowed = can("configure");
  const [mode, setMode] = useState("local_auto");
  const [rooms, setRooms] = useState<{name: string; priority: string}[]>([]);
  const [priorities, setPriorities] = useState<Record<string, string>>({});
  useEffect(() => {
    if (mode !== "smart_auto" || !allowed) return;
    let active = true;
    void requestJson<{rooms?: {name: string; priority: string; control: boolean}[]}>("/api/controller/rooms?catalog=0")
      .then(view => { if (active) setRooms((view.rooms ?? []).filter(room => room.control)); })
      .catch(() => { if (active) setError("Kunne ikke hente rumprioriteter"); });
    return () => { active = false; };
  }, [mode, allowed]);
  const [co2, setCo2] = useState<string | null>(null), [rh, setRh] = useState<string | null>(null);
  const [range, setRange] = useState("24h"), [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setError(""); setResult(null);
    try { setResult(await postJson<Result>("/api/controller/replay", { range, ...(mode === "smart_auto" ? {mode} : {}), settings: { co2_setpoint: Number(co2 ?? co2Default), rh_setpoint: Number(rh ?? rhDefault), ...(mode === "smart_auto" ? {room_priorities: priorities} : {}) } }, csrf)); }
    catch (e) { setError(e instanceof Error ? e.message : "Prøvekørsel mislykkedes"); }
    finally { setBusy(false); }
  };
  return <article className="surface panel-card" aria-label="Prøvekør styringen">
    <div className="pro-card-head compact"><div><h2>Prøvekør styringen</h2><p>Sammenlign grænser på gemte målinger, før du ændrer styringen.</p></div></div>
    <div className="diag-actions">
      <label>Prøvetilstand<select value={mode} disabled={busy} onChange={e => {setMode(e.target.value); setResult(null); setError("");}}>
        <option value="local_auto">Local Auto</option><option value="smart_auto">Smart Auto-behov</option>
      </select></label>
      <label>CO₂-grænse (ppm)<input type="number" min="400" max="2000" step="50" disabled={busy} value={co2 ?? co2Default} onChange={e => { setCo2(e.target.value); setResult(null); }}/></label>
      <label>Fugtgrænse (%)<input type="number" min="20" max="80" disabled={busy} value={rh ?? rhDefault} onChange={e => { setRh(e.target.value); setResult(null); }}/></label>
      <label>Periode<select value={range} disabled={busy} onChange={e => { setRange(e.target.value); setResult(null); }}><option value="1h">1 time</option><option value="6h">6 timer</option><option value="24h">24 timer</option></select></label>
      <button className="primary-action" disabled={busy || !csrf || !can("configure") || !Number(co2 ?? co2Default) || !Number(rh ?? rhDefault)} onClick={() => void run()}>{busy ? "Prøvekører…" : "Sammenlign uden at ændre"}</button>
    </div>
    {mode === "smart_auto" && <div className="diag-actions">{rooms.map(room => <label key={room.name}>{room.name} · prøveprioritet
      <select aria-label={`${room.name} prøveprioritet`} disabled={busy} value={priorities[room.name] ?? room.priority} onChange={e => {setPriorities(old => ({...old, [room.name]: e.target.value})); setResult(null);}}>
        <option value="auto">Automatisk</option><option value="low">Lav</option><option value="normal">Normal</option><option value="high">Høj</option><option value="critical">Kritisk</option>
      </select></label>)}</div>}
    {mode === "smart_auto" && <p className="hx-note">Rumoptagelser samles fra denne opdatering, én gang i minuttet. Behovet beregnes før ugeplan, nat og andre begrænsninger. Ingen ændring sendes til anlægget.</p>}
    {error && <p role="alert">{error}</p>}
    {result && <><p>{result.covered_minutes.toLocaleString("da-DK")} min med data · forskelligt trin i {result.different_minutes.toLocaleString("da-DK")} min.</p>
      {result.points.length > 0 ? <HistoryChart title={`${mode === "smart_auto" ? "Smart Auto-behov" : "Local Auto"}: nuværende mod forslag`} samples={result.points.map(point => ({...point, observed_level: point.observed_level ?? null}))} unit=" trin" minSpan={3} series={[{ key: "current_level", label: "Nuværende indstillinger", slot: 1, stepped: true }, { key: "proposed_level", label: "Forslag", slot: 2, stepped: true }, ...(mode === "smart_auto" ? [{key: "observed_level", label: "Faktisk optaget trin", slot: 3 as const, stepped: true}] : [])]}/> : <p>Ingen egnede optagelser i perioden endnu. Rumdata samles løbende efter opdateringen.</p>}
      {!result.humidity_available && <p>Perioden mangler gemt fugtighed. Sammenligningen bruger kun CO₂.</p>}
      <p className="hx-note">{result.note} Starten af perioden og større datahuller nulstiller holdetiderne. Korte udsving mellem optagelser kan være skjult.</p></>}
  </article>;
}
