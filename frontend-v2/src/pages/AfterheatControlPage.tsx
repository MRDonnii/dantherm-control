import "../styles/panels.css"; import "../styles/management.css"; import "../styles/history.css"; import "../styles/afterheat-control.css";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { RefreshCw } from "lucide-react";
import { InfoList, type InfoRow } from "../components/InfoList";
import { HistoryChart } from "../components/HistoryChart";
import { requestJson } from "../lib/api";
import { AFTERHEAT_ALGORITHMS, algorithmName } from "../lib/afterheatControl";
import { clean, valvePercent, type Sample } from "../lib/historyData";
import { niceScale } from "../lib/chartScale";
import { usePullRefresh } from "../lib/pullRefresh";

type Num = number | null | undefined;
type Row = { algorithm: string; label?: string; minutes?: Num; measured_minutes?: Num; enough?: boolean; mean_error?: Num; mean_abs_error?: Num;
  air_min?: Num; under_floor_percent?: Num; level_changes_per_hour?: Num; safety_minutes?: Num };
type Performance = { algorithms?: Row[]; compare?: boolean };
type Model = { state?: string | null; step_k_now?: Num[] | null; tau_up_s?: Num; tau_down_s?: Num; dead_s?: Num; learned_minutes?: Num; samples?: Num;
  prediction_error_k?: Num; refits?: Num; refit_attempts?: Num;
  refit_last?: { at?: Num; prior_rmse?: Num; fitted_rmse?: Num; accepted?: Num } | null;
  algorithms?: Record<string, Record<string, unknown>> | null };
type Now = { afterheat_control_reason?: string | null; afterheat_level?: Num; afterheat_level_target?: Num; afterheat_level_wanted?: Num;
  afterheat_target?: Num; afterheat_algorithm_active?: string | null; afterheat_algorithm_wanted?: Num; afterheat_algorithm_why?: string | null;
  afterheat_algorithm_state?: string | null; afterheat_safety_override?: Num; afterheat_algorithm_forecast?: Num[] | null;
  afterheat_model?: Model | null; afterheat_ha?: { heat_kw?: Num; energy_today_kwh?: Num } | null; heating_coil_after_temperature?: Num;
  afterheat_pilot_learned?: { step_k?: Num[]; steps_measured?: Num; fall_overrun_s?: Record<string, Num> } | null };
export type AlgorithmReport = { now?: Now; performance?: Record<string, Performance>; algorithms?: { key: string; label: string; help?: string }[];
  overrides?: Record<string, string> };

const RANGES_PERF = [["1h", "Seneste time"], ["24h", "Seneste døgn"], ["7d", "Seneste uge"]] as const;
const RANGES_HIST = [["6h", "6 timer", 21600], ["24h", "24 timer", 86400], ["7d", "7 dage", 604800]] as const;
const CODES = AFTERHEAT_ALGORITHMS.map(item => item.key);
const CODE_COLORS = ["#2a78d6", "#1baf7a", "#d9a100", "#a385f5", "#e07aa8", "#3bb4c4"];

const fin = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const n = (v: Num, digits = 1, unit = "") => fin(v) ? `${v.toLocaleString("da-DK", { minimumFractionDigits: digits, maximumFractionDigits: digits })}${unit}` : "–";
const signed = (v: Num, digits = 2, unit = "") => fin(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toLocaleString("da-DK", { minimumFractionDigits: digits, maximumFractionDigits: digits })}${unit}` : "–";
const step = (v: Num) => fin(v) ? `trin ${v.toLocaleString("da-DK", { maximumFractionDigits: 1 })}` : "–";
const seconds = (v: Num) => !fin(v) ? "–" : v >= 120 ? `ca. ${Math.round(v / 60).toLocaleString("da-DK")} min` : `ca. ${Math.round(v).toLocaleString("da-DK")} s`;
const clock = (ts: number) => new Date(ts * 1000).toLocaleString("da-DK", { weekday: "short", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });

const STATE_TEXT: Record<string, string> = {
  paused: "Styringen havde fejl og holder pause; standard bruger imens",
  fallback: "HAC1 styrer selv lige nu",
};
const MODEL_BADGE: Record<string, [string, "ok" | "warn" | "neutral"]> = {
  learning: ["Lærer stadig", "warn"], ready: ["Klar", "ok"], uncertain: ["Usikker", "warn"],
};

function Card({ title, lead, label, children }: { title: string; lead?: string; label?: string; children: ReactNode }) {
  return <article className="surface panel-card" aria-label={label ?? title}><div className="pro-card-head compact"><div><h2>{title}</h2>{lead && <p>{lead}</p>}</div></div>{children}</article>;
}

function Segment({ items, value, onChange, label }: { items: readonly (readonly [string, string, ...unknown[]])[]; value: string; onChange: (v: string) => void; label: string }) {
  return <div className="pro-segment ac-segment" role="group" aria-label={label}>
    {items.map(([key, text]) => <button key={key} type="button" className={value === key ? "active" : ""} aria-pressed={value === key} onClick={() => onChange(key)}>{text}</button>)}
  </div>;
}

/** The predicted air against the target and the floor (target − 0,5 °C). Own small SVG: the x axis is minutes ahead, not time of day. */
export function ForecastChart({ forecast, target }: { forecast: number[]; target: Num }) {
  const W = 600, H = 190, left = 40, right = 12, top = 10, bottom = 28;
  const floor = fin(target) ? target - 0.5 : null;
  const values = [...forecast, ...(fin(target) ? [target] : []), ...(floor !== null ? [floor] : [])];
  const scale = niceScale(Math.min(...values), Math.max(...values), 2);
  const x = (i: number) => left + (forecast.length <= 1 ? 0 : (i / (forecast.length - 1)) * (W - left - right));
  const y = (v: number) => top + (1 - (v - scale.lo) / (scale.hi - scale.lo)) * (H - top - bottom);
  const path = forecast.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join("");
  const every = forecast.length > 8 ? 2 : 1;
  const lowest = Math.min(...forecast);
  return <>
    <svg className="ac-forecast" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`Forventet luft de næste ${forecast.length} minutter, laveste ${n(lowest, 1, " °C")}`}>
      {scale.ticks.map(t => <g key={t}><line className="ac-grid" x1={left} x2={W - right} y1={y(t)} y2={y(t)}/><text x={left - 6} y={y(t) + 4} textAnchor="end">{n(t, Number.isInteger(t) ? 0 : 1)}</text></g>)}
      {fin(target) && <line className="ac-target" x1={left} x2={W - right} y1={y(target)} y2={y(target)}/>}
      {floor !== null && <line className="ac-floor" x1={left} x2={W - right} y1={y(floor)} y2={y(floor)}/>}
      <path className="ac-air" d={path}/>
      {forecast.map((_, i) => i % every === 0 || i === forecast.length - 1 ? <text key={i} x={x(i)} y={H - 10} textAnchor="middle">{i + 1}</text> : null)}
      <text x={W - right} y={H - 0} textAnchor="end">min frem</text>
    </svg>
    <ul className="ac-legend">
      <li><i style={{ "--c": "var(--hx-1)" } as React.CSSProperties}/>Forventet luft</li>
      {fin(target) && <li><i style={{ "--c": "var(--hx-3)" } as React.CSSProperties}/>Mål {n(target, 1, " °C")}</li>}
      {floor !== null && <li><i style={{ "--c": "var(--hx-2)" } as React.CSSProperties}/>Bund (mål − 0,5 °C)</li>}
    </ul>
  </>;
}

function decisionRows(now: Now): InfoRow[] {
  const active = now.afterheat_algorithm_active ?? null;
  const state = now.afterheat_algorithm_state ?? null;
  const wanted = fin(now.afterheat_algorithm_wanted) ? now.afterheat_algorithm_wanted : now.afterheat_level_wanted;
  return [
    { label: "Styring der bestemmer nu", value: active ? algorithmName(active) : "HAC1 styrer selv", tone: state ? "warn" : undefined },
    ...(state && STATE_TEXT[state] ? [{ label: "Bemærk", value: STATE_TEXT[state], tone: "warn" as const }] : []),
    { label: "Ønsket", value: step(wanted) },
    { label: "Brugt", value: step(now.afterheat_level_target) },
    { label: "Trin lige nu", value: step(now.afterheat_level) },
    { label: "Luft nu", value: n(now.heating_coil_after_temperature, 1, " °C") },
    { label: "Mål", value: n(now.afterheat_target, 1, " °C") },
    { label: "Hvorfor", value: now.afterheat_algorithm_why || "–" },
    { label: "Begrundelse", value: now.afterheat_control_reason || "–" },
  ];
}

function safetyText(code: Num, overrides: Record<string, string>) {
  return !fin(code) || code === 0 ? "Sikkerheden greb ikke ind" : `Sikkerheden greb ind: ${overrides[String(code)] ?? `årsag ${code}`}`;
}

function Performance({ report }: { report: AlgorithmReport }) {
  const [range, setRange] = useState("24h");
  const block = report.performance?.[range];
  const rows = block?.algorithms ?? [];
  const compare = !!block?.compare && rows.length > 1;
  const cells: [string, (r: Row) => string][] = [
    ["Gennemsnitlig afvigelse", r => signed(r.mean_error, 2, " K")],
    ["Laveste luft", r => n(r.air_min, 1, " °C")],
    ["Tid under målet − 0,5 °C", r => n(r.under_floor_percent, 0, " %")],
    ["Trinskift pr. time", r => n(r.level_changes_per_hour, 1)],
    ["Minutter hvor sikkerheden greb ind", r => n(r.safety_minutes, 0, " min")],
  ];
  const name = (r: Row) => r.label ?? algorithmName(r.algorithm);
  return <Card title="Hvor godt går det" lead="Hvordan hver styring har klaret sig. Kun visning." label="Hvor godt går det">
    <Segment items={RANGES_PERF} value={range} onChange={setRange} label="Periode for resultater"/>
    {rows.length === 0 && <p className="hx-note">Ingen målinger endnu i den periode.</p>}
    {compare ? <div className="settings-table-wrap"><table className="settings-table">
      <thead><tr><th></th>{rows.map(r => <th key={r.algorithm}>{name(r)}<br/><small>{n(r.measured_minutes ?? r.minutes, 0, " min")}</small></th>)}</tr></thead>
      <tbody>{cells.map(([label, f]) => <tr key={label}><td>{label}</td>{rows.map(r => <td key={r.algorithm}>{f(r)}</td>)}</tr>)}
        {rows.some(r => r.enough === false) && <tr><td>Datamængde</td>{rows.map(r => <td key={r.algorithm}>{r.enough === false ? "For lidt data til at sammenligne" : "Nok data"}</td>)}</tr>}</tbody>
    </table></div> : <div className="ac-cards">
      {rows.map(r => <section key={r.algorithm} aria-label={name(r)}><h3>{name(r)}</h3>
        {r.enough === false && <p className="ac-short">For lidt data til at sammenligne</p>}
        <InfoList rows={[{ label: "Målt i", value: n(r.measured_minutes ?? r.minutes, 0, " min") }, ...cells.map(([label, f]) => ({ label, value: f(r) }))]}/></section>)}
    </div>}
    {!compare && rows.length > 0 && <p className="hx-note">Kun én styring har kørt i perioden, så der er intet at sammenligne med endnu.</p>}
  </Card>;
}

function learnedRows(model: Model | null | undefined): InfoRow[] {
  if (!model) return [];
  const [badge, tone] = MODEL_BADGE[model.state ?? ""] ?? ["–", "neutral" as const];
  const k = Array.isArray(model.step_k_now) ? model.step_k_now : [];
  const last = model.refit_last;
  return [
    { label: "Modellen er", value: <span className={`ac-badge tone-${tone}`}>{badge}</span> },
    ...(["0 → 1", "1 → 2", "2 → 3", "3 → 4"].map((text, i) => ({ label: `Varme pr. trin ved nuværende blæser, ${text}`, value: n(k[i], 1, " K") }))),
    { label: "Hvor hurtigt luften reagerer, op", value: seconds(model.tau_up_s) },
    { label: "Hvor hurtigt luften reagerer, ned", value: seconds(model.tau_down_s) },
    { label: "Forsinkelse", value: fin(model.dead_s) ? `${n(model.dead_s, 0)} s` : "–" },
    { label: "Lært af", value: fin(model.samples) ? `${n(model.samples, 0)} målinger${fin(model.learned_minutes) ? ` (${n(model.learned_minutes, 0)} min på samme trin)` : ""}` : "–" },
    { label: "Hvor godt modellen rammer", value: fin(model.prediction_error_k) ? `typisk ${n(model.prediction_error_k, 2, " K")} forkert et minut frem` : "måles endnu" },
    { label: "Seneste genberegning", value: last && fin(last.at)
      ? `${clock(last.at)} · ${last.accepted ? "taget i brug" : "ikke taget i brug"} · fejl før ${n(last.prior_rmse, 2, " K")}, efter ${n(last.fitted_rmse, 2, " K")}`
      : "Ikke endnu (kræver mindst en times data)" },
    { label: "Genberegninger", value: `${n(model.refits, 0)} taget i brug af ${n(model.refit_attempts, 0)} forsøg` },
  ];
}

function perAlgorithmRows(model: Model | null | undefined): InfoRow[] {
  const all = model?.algorithms ?? {};
  const rows: InfoRow[] = [];
  const simple = all.mpc_simple as { gain?: Num; tau?: Num } | undefined;
  if (simple && (fin(simple.gain) || fin(simple.tau))) {
    rows.push({ label: "Forudseende enkel: varme pr. trin", value: n(simple.gain, 1, " K") }, { label: "Forudseende enkel: reaktionstid", value: seconds(simple.tau) });
  }
  const pid = all.pid as { tune?: Record<string, number[]> } | undefined;
  for (const [bucket, vals] of Object.entries(pid?.tune ?? {})) {
    if (Array.isArray(vals)) rows.push({ label: `Klassisk regulator: justeret til målet ${bucket} °C`,
      value: `afstand ×${n(vals[0], 1)}, varighed ×${n(vals[1], 1)}, hastighed ×${n(vals[2], 1)}` });
  }
  const power = all.heat_power as { trim?: Num } | undefined;
  if (power && fin(power.trim)) rows.push({ label: "Varmeeffekt: rettelse", value: `${power.trim > 0 ? "+" : power.trim < 0 ? "−" : "±"}${Math.abs(power.trim).toLocaleString("da-DK")} trin` });
  return rows;
}

function Learned({ now }: { now: Now }) {
  const model = now.afterheat_model;
  const pilot = now.afterheat_pilot_learned;
  const extra = perAlgorithmRows(model);
  const falls = Object.entries(pilot?.fall_overrun_s ?? {}).filter(([, v]) => fin(v));
  return <Card title="Hvad den har lært" lead="Den fælles model bruges af Forudseende; de andre bruger den til at hjælpe og sikre." label="Hvad den har lært">
    {model ? <InfoList rows={learnedRows(model)}/> : <p className="hx-note">Modellen er ikke tilgængelig endnu.</p>}
    {extra.length > 0 && <InfoList rows={extra}/>}
    {now.afterheat_ha && <InfoList rows={[
      { label: "Varme fra varmefladen lige nu", value: n(now.afterheat_ha.heat_kw, 2, " kW") },
      { label: "Varme fra varmefladen i dag", value: n(now.afterheat_ha.energy_today_kwh, 2, " kWh") },
    ]}/>}
    <p className="hx-note ac-model-note">Tidsdeling og Forudseende lærer ikke selv noget ekstra; de bruger den fælles model.</p>
    {pilot && <InfoList rows={[
      { label: "Standardstyringen har målt", value: `${n(pilot.steps_measured, 0)} trinskift` },
      ...["0 → 1", "1 → 2", "2 → 3", "3 → 4"].map((text, i) => ({ label: `Standard: varme pr. trin, ${text}`, value: n(pilot.step_k?.[i], 1, " K") })),
      ...falls.map(([fan, v]) => ({ label: `Standard: luften falder videre efter skift op, blæser ${fan} %`, value: n(v as number, 0, " s") })),
    ]}/>}
  </Card>;
}

type HistoryResponse = { samples?: Sample[] };

function HistorySection() {
  const [range, setRange] = useState("24h");
  const [samples, setSamples] = useState<Sample[]>([]);
  const [error, setError] = useState(""), [loading, setLoading] = useState(true);
  const [hover, setHover] = useState<number | null>(null);
  const [window_, setWindow] = useState<{ from: number; to: number } | undefined>();
  const load = useCallback(async () => {
    try {
      const result = await requestJson<HistoryResponse>(`/history.json?range=${range}`, { timeoutMs: 8000 });
      const span = RANGES_HIST.find(item => item[0] === range)?.[2] ?? 86400;
      const now = Math.floor(Date.now() / 1000);
      setSamples(Array.isArray(result.samples) ? clean(result.samples) : []); setWindow({ from: now - span, to: now }); setError("");
    } catch (e) { setError(e instanceof Error ? e.message : "Kunne ikke hente historikken"); }
    finally { setLoading(false); }
  }, [range]);
  useEffect(() => { setLoading(true); void load(); const timer = window.setInterval(() => void load(), 60000); return () => window.clearInterval(timer); }, [load]);
  const rows = useMemo<Sample[]>(() => samples.map(s => ({
    ...s,
    ac_floor: fin(s.afterheat_target) ? s.afterheat_target - 0.5 : null,
    ac_level: valvePercent(s.afterheat_level),
    ac_wanted: valvePercent(s.afterheat_algorithm_wanted),
  })), [samples]);
  const sorted = useMemo(() => rows.filter(s => fin(s.ts)).sort((a, b) => (a.ts as number) - (b.ts as number)), [rows]);
  const used = new Set<string>();
  let hasNone = false;
  const from = window_?.from ?? (sorted[0]?.ts as number | undefined) ?? 0;
  const to = window_?.to ?? (sorted[sorted.length - 1]?.ts as number | undefined) ?? 1;
  const span = Math.max(1, to - from);
  const segments = sorted.map((s, i) => {
    const start = Math.max(from, s.ts as number);
    const end = Math.min(to, i + 1 < sorted.length ? sorted[i + 1].ts as number : to);
    const code = fin(s.afterheat_algorithm_code) ? Math.round(s.afterheat_algorithm_code) : null;
    if (code !== null && CODES[code]) used.add(CODES[code]); else if (code === null && fin(s.afterheat_level)) hasNone = true;
    return { start, end, code, ts: s.ts as number, safety: fin(s.afterheat_safety_override) && s.afterheat_safety_override > 0 };
  }).filter(s => s.end > s.start);
  const safetyCount = segments.filter(s => s.safety).length;
  const hasAlgo = sorted.some(s => fin(s.afterheat_algorithm_code));
  return <Card title="Historik" lead="Luften mod målet og bunden, trin og hvilken styring der kørte" label="Historik">
    <Segment items={RANGES_HIST} value={range} onChange={setRange} label="Periode for historik"/>
    {error && <p role="alert">{error}</p>}
    <HistoryChart samples={rows} range={window_} hoverTs={hover} onHover={setHover} loading={loading} title="Eftervarmens styring" unit="°C" height={310} minSpan={3}
      series={[{ key: "heating_coil_after_temperature", label: "Luft efter varmefladen", slot: 1 }, { key: "ac_floor", label: "Bund (mål − 0,5 °C)", slot: 2, stepped: true }]}
      reference={{ key: "afterheat_target", label: "Mål", decimals: 1 }}
      valve={{ key: "ac_level", label: "Trin (ventilåbning)", decisionKey: "ac_wanted", decisionLabel: "Styringens ønske", decisionIdle: "HAC1 styrer selv" }}/>
    {hasAlgo && <>
      <div className="ac-strip" role="img" aria-label={`Hvilken styring der kørte. Sikkerheden greb ind ${safetyCount} gange i perioden.`}>
        {segments.map(s => <i key={s.ts} title={s.code !== null ? algorithmName(CODES[s.code]) : "HAC1 styrer selv"}
          style={{ width: `${((s.end - s.start) / span) * 100}%`, marginLeft: s === segments[0] ? `${((s.start - from) / span) * 100}%` : undefined,
            background: s.code !== null ? CODE_COLORS[s.code] ?? "#888" : "#6b7280" }}/>)}
        {segments.filter(s => s.safety).map(s => <b key={`s${s.ts}`} title="Sikkerheden greb ind" style={{ left: `${((s.start - from) / span) * 100}%` }}/>)}
      </div>
      <ul className="ac-legend">
        {CODES.filter(key => used.has(key)).map(key => <li key={key}><i style={{ "--c": CODE_COLORS[CODES.indexOf(key)] } as React.CSSProperties}/>{algorithmName(key)}</li>)}
        {hasNone && <li><i style={{ "--c": "#6b7280" } as React.CSSProperties}/>HAC1 styrer selv</li>}
        <li><i style={{ "--c": "var(--red)" } as React.CSSProperties}/>Sikkerheden greb ind ({safetyCount})</li>
      </ul></>}
    <p className="hx-note">Den farvede bjælke viser hvilken styring der kørte hvornår; røde streger er steder hvor sikkerheden greb ind.</p>
  </Card>;
}

/** Read-only view of the afterheat control algorithms: the decision now, the results, what the model has learned and the history. */
export function AfterheatControlPage() {
  const [report, setReport] = useState<AlgorithmReport | null>(null);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true); setError("");
    try { setReport(await requestJson<AlgorithmReport>("/api/afterheat/algorithm", { timeoutMs: 20000 })); }
    catch (e) { setError(e instanceof Error ? e.message : "Kunne ikke hente eftervarmens styring"); }
    finally { setBusy(false); }
  }, []);
  usePullRefresh(load);
  useEffect(() => { void load(); const timer = window.setInterval(() => void load(), 30000); return () => window.clearInterval(timer); }, [load]);
  const now = report?.now;
  const overrides = report?.overrides ?? {};
  const forecast = (now?.afterheat_algorithm_forecast ?? []).filter(fin);
  const safetyCode = now?.afterheat_safety_override;
  const safe = !fin(safetyCode) || safetyCode === 0;
  return <section className="dashboard-overview page-enter">
    <header className="overview-heading-row"><div><span className="eyebrow">EFTERVARME</span><h1>Eftervarmens styring</h1>
      <p>Hvad styringen vælger lige nu, hvor godt det går, og hvad den har lært. Kun visning; intet ændres. Styringen vælges under Indstillinger.</p></div>
      <div className="diag-actions"><button className="secondary-action" disabled={busy} onClick={() => void load()}><RefreshCw size={15}/> {busy ? "Henter…" : "Opdatér"}</button></div></header>
    {error && <p role="alert">{error}</p>}
    {now && <>
      <Card title="Nu: beslutningen" lead="Hvad styringen vil, og hvad der faktisk bruges" label="Nu: beslutningen">
        <p className={`ac-safety${safe ? "" : " is-active"}`} role="status">{safetyText(safetyCode, overrides)}</p>
        <InfoList rows={decisionRows(now)}/>
        {forecast.length > 1 && <><h3>Forventet luft</h3><ForecastChart forecast={forecast} target={now.afterheat_target}/></>}
      </Card>
      <Performance report={report}/>
      <Learned now={now}/>
    </>}
    <HistorySection/>
  </section>;
}
