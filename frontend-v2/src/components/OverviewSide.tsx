import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { requestJson } from "../lib/api";
import { sortDay, toMinutes, type Period } from "../lib/schedule";
import { TablerIcon } from "./TablerIcon";

type Data = Record<string, unknown>;
type Sample = { ts?: number } & Record<string, unknown>;
type Alarm = { code: string; severity: string; text: string };

const SERIES = [
  { key: "outdoor_temp", label: "Udeluft", unit: "°C", color: "var(--blue-t)" },
  { key: "supply_temp", label: "Indblæsning", unit: "°C", color: "var(--green-t)" },
  { key: "co2", label: "CO₂", unit: "ppm", color: "var(--orange-t)" },
  { key: "humidity", label: "Luftfugtighed", unit: "%", color: "var(--teal-t)" },
] as const;

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function fmt(value: number | null, unit: string) {
  if (value === null) return "—";
  const digits = unit === "°C" ? 1 : 0;
  return `${value.toLocaleString("da-DK", { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${unit}`;
}

function Spark({ values, color }: { values: (number | null)[]; color: string }) {
  const finite = values.filter((value): value is number => value !== null);
  if (finite.length < 2) return <div className="ov-spark empty"/>;
  const lo = Math.min(...finite), hi = Math.max(...finite), span = hi - lo || 1;
  const points = values.map((value, index) => value === null ? null : `${(index / (values.length - 1) * 100).toFixed(2)},${(30 - (value - lo) / span * 26).toFixed(2)}`).filter(Boolean).join(" ");
  return <svg className="ov-spark" viewBox="0 0 100 32" preserveAspectRatio="none" aria-hidden="true">
    <polygon points={`0,32 ${points} 100,32`} fill={color} opacity=".14"/>
    <polyline points={points} fill="none" stroke={color} strokeWidth="1.6" vectorEffect="non-scaling-stroke" strokeLinejoin="round"/>
  </svg>;
}

/** Højre kolonne på forsiden i forslag B (Aurora): Kræver opmærksomhed, Ugeplan i dag, 24 timer og Home Assistant. */
export function OverviewSide({ controller, busHealthy, haLabel, haDetail, haTone, haVisible, canConfigure }: {
  controller: Data; busHealthy: boolean; haLabel: string; haDetail: string; haTone: string; haVisible: boolean; canConfigure: boolean;
}) {
  const [samples, setSamples] = useState<Sample[]>([]);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const result = await requestJson<{ samples?: Sample[] }>("/history.json?range=24h", { timeoutMs: 6000 });
        if (!cancelled && Array.isArray(result.samples)) setSamples(result.samples);
      } catch { /* kolonnen er kun et overblik; graferne bliver tomme */ }
    };
    void load();
    const timer = window.setInterval(() => void load(), 300000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  const alarms = Array.isArray(controller.diagnostics_alarms) ? controller.diagnostics_alarms as Alarm[] : [];
  const filterLife = num(controller.filter_life_percent);
  const filterDays = num(controller.filter_days_remaining);
  const filterDue = controller.filter_status === "skift_snart" || controller.filter_status === "overskredet";
  const master = String(controller.active_master ?? "") === "pi" ? "Pi er master" : controller.active_master ? `${String(controller.active_master)} er master` : null;

  const now = new Date();
  const today = String((now.getDay() + 6) % 7);
  const minutes = now.getHours() * 60 + now.getMinutes();
  const week = controller.schedule_periods as Record<string, Period[]> | undefined;
  const periods = sortDay([...(week?.[today] ?? [])]);
  const scheduleOn = controller.schedule_enabled !== false;
  const isNow = (period: Period) => {
    const start = toMinutes(period.start), end = toMinutes(period.end);
    return start <= end ? minutes >= start && minutes < end : minutes >= start || minutes < end;
  };

  return <aside className="ov-side" aria-label="Overblik">
    <section className="paper">
      <div className="paper-head"><h3>Kræver opmærksomhed</h3></div>
      <div className="ov-alerts">
        {alarms.map(alarm => <div key={alarm.code} className={`alert ${alarm.severity === "critical" ? "bad" : "warn"}`}><TablerIcon name="alert-triangle" size={20}/><div><b>{alarm.severity === "critical" ? "Fejl" : alarm.severity === "warning" ? "Advarsel" : "Bemærk"}</b><div>{alarm.text}</div></div></div>)}
        {alarms.length === 0 && <div className="alert ok"><TablerIcon name="check" size={20}/><div><b>Ingen alarmer</b><div>{[busHealthy ? "RS485-bus sund" : "Bussen afventer", master].filter(Boolean).join(" · ")}</div></div></div>}
        {filterLife !== null && <div className={`alert ${filterDue ? "warn" : ""}`}><TablerIcon name="filter" size={20}/><div><b>Filter {Math.round(filterLife)} %</b><div>{controller.filter_status === "overskredet" ? "Skift filteret nu" : filterDays !== null ? `Skift anslået om ca. ${filterDays} dage` : "Filtertæller fra anlægget"}</div></div></div>}
      </div>
    </section>

    <section className="paper">
      <div className="paper-head"><h3>I dag efter ugeplanen</h3><NavLink className="btn sm subtle more" to="/schedule">Ugeplan<TablerIcon name="arrow-right" size={14}/></NavLink></div>
      {!scheduleOn ? <p className="ov-empty">Ugeplanen er slået fra.</p> : periods.length === 0 ? <p className="ov-empty">Ingen perioder i dag.</p> : periods.map(period => (
        <div key={`${period.start}-${period.label}`} className="list-row ov-period">
          <span className="tabnum dim">{period.start}</span><strong>{period.label || "Periode"}</strong><span className="dim">{period.mode === "min" ? "mindst " : ""}trin {period.level}</span>
          {isNow(period) && <span className="badge b-blue">Nu</span>}
        </div>
      ))}
    </section>

    <section className="paper">
      <div className="paper-head"><h3>Seneste 24 timer</h3><NavLink className="btn sm subtle more" to="/history">Historik<TablerIcon name="arrow-right" size={14}/></NavLink></div>
      <div className="ov-sparks">
        {SERIES.map(series => {
          const values = samples.map(sample => num(sample[series.key]));
          const last = [...values].reverse().find(value => value !== null) ?? null;
          return <div key={series.key}><span className="dim">{series.label}</span><strong className="tabnum">{fmt(last, series.unit)}</strong><Spark values={values} color={series.color}/></div>;
        })}
      </div>
    </section>

    {haVisible && <section className="paper">
      <div className="paper-head"><h3>Home Assistant</h3>{canConfigure && <NavLink className="btn sm subtle more" to="/home-assistant">Åbn<TablerIcon name="arrow-right" size={14}/></NavLink>}</div>
      <div className="row ov-ha"><span className={`fd-dot ${haTone === "ok" ? "c-ok" : haTone === "bad" ? "c-bad" : haTone === "warn" ? "c-warn" : "c-off"}`}/><span>{haDetail || haLabel}</span><span className={`badge ${haTone === "ok" ? "b-ok" : haTone === "bad" ? "b-bad" : haTone === "warn" ? "b-warn" : "b-gray"}`}>{haLabel}</span></div>
    </section>}
  </aside>;
}
