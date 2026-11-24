import { useCallback, useEffect, useMemo, useState } from "react";
import type React from "react";
import { Activity, Fan, Leaf, Recycle, SlidersHorizontal } from "lucide-react";
import { AfterheatAnalysis } from "../components/AfterheatAnalysis";
import { AfterheatScale } from "../components/AfterheatScale";
import { AlarmHistory } from "../components/AlarmHistory";
import { HistoryChart } from "../components/HistoryChart";
import { requestJson } from "../lib/api";
import { AFTERHEAT_CHART, afterheatLayers, clean, withAfterheatLines } from "../lib/historyData";
import "../styles/history.css";

type Sample = Record<string, number | null>;
type HistoryResponse = { range: string; samples: Sample[] };
type ChangeEvent = { timestamp: number; source: string; changes: Record<string, [unknown, unknown]> };

// Readable names for the settings that are changed most often.
const SETTING_LABELS: Record<string, string> = {
  afterheat_setpoint: "Eftervarme setpunkt",
  afterheat_enabled: "Eftervarme tændt",
  afterheat_control_mode: "Eftervarme regulering",
  mode: "Driftstilstand",
  manual_level: "Manuelt trin",
  local_normal_level: "Normaltrin",
  bypass: "Bypass",
  standby_minutes: "Sluk anlæg (min)",
  bonfire_minutes: "Bål i haven (min)",
  fireplace_minutes: "Pejsefunktion (min)",
  schedule_enabled: "Ugeplan tændt",
  schedule_periods: "Ugeplan",
  vacation_enabled: "Ferie",
  vacation_level: "Trin under ferie",
  vacation_from: "Ferie fra",
  vacation_until: "Ferie til",
};
function sourceLabel(source: string) {
  if (source === "home_assistant") return "Home Assistant";
  if (source.startsWith("webui:")) return `WebUI · ${source.slice(6)}`;
  if (source.startsWith("apikey:")) return `API-nøgle · ${source.slice(7)}`;
  return source === "api" ? "API" : source;
}
function valueLabel(value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  if (value === true) return "til";
  if (value === false) return "fra";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function changeValue(key: string, value: unknown) {
  if ((key === "vacation_from" || key === "vacation_until") && value && Number.isFinite(Number(value))) {
    return new Date(Number(value) * 1000).toLocaleString("da-DK", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  return valueLabel(value);
}

const RANGES: { value: string; label: string; seconds: number }[] = [
  { value: "1h", label: "1 time", seconds: 3600 },
  { value: "6h", label: "6 timer", seconds: 21600 },
  { value: "24h", label: "24 timer", seconds: 86400 },
  { value: "7d", label: "7 dage", seconds: 604800 },
  { value: "30d", label: "30 dage", seconds: 2592000 },
];

const AFTER = "heating_coil_after_temperature";

function finite(value: unknown): value is number { return typeof value === "number" && Number.isFinite(value); }

function summary(samples: Sample[], key: string) {
  let latest: number | null = null;
  let low = Infinity;
  let high = -Infinity;
  let total = 0;
  let count = 0;
  for (const sample of samples) {
    const value = sample[key];
    if (!finite(value)) continue;
    latest = value;
    low = Math.min(low, value);
    high = Math.max(high, value);
    total += value;
    count += 1;
  }
  return { latest, low: count ? low : null, high: count ? high : null, mean: count ? total / count : null };
}

function format(value: number | null, decimals: number) {
  return value === null ? "–" : value.toLocaleString("da-DK", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function StatTile({ label, value, unit, detail }: { label: string; value: string; unit: string; detail: string }) {
  return (
    <article className="surface hx-stat">
      <span>{label}</span>
      <strong>{value}<small>{value === "–" ? "" : unit}</small></strong>
      <em>{detail}</em>
    </article>
  );
}

function ChartCard({ title, subtitle, icon, wide = false, children }: { title: string; subtitle: string; icon: React.ReactNode; wide?: boolean; children: React.ReactNode }) {
  return (
    <article className={`surface history-card${wide ? " history-card-wide" : ""}`}>
      <div className="pro-card-head compact"><div><h2>{title}</h2><p>{subtitle}</p></div>{icon}</div>
      {children}
    </article>
  );
}

export function HistoryPage() {
  const [range, setRange] = useState("24h");
  const [samples, setSamples] = useState<Sample[]>([]);
  const [window_, setWindow] = useState<{ from: number; to: number } | undefined>(undefined);
  const [updated, setUpdated] = useState<number | null>(null);
  const [online, setOnline] = useState(false);
  const [loading, setLoading] = useState(true);
  const [hover, setHover] = useState<number | null>(null);
  const [changes, setChanges] = useState<ChangeEvent[]>([]);

  const refresh = useCallback(async (activeRange: string) => {
    try {
      const result = await requestJson<HistoryResponse>(`/history.json?range=${activeRange}`, { timeoutMs: 8000 });
      const now = Math.floor(Date.now() / 1000);
      const seconds = RANGES.find(item => item.value === activeRange)?.seconds ?? 86400;
      setSamples(Array.isArray(result.samples) ? clean(result.samples) : []);
      setWindow({ from: now - seconds, to: now });
      setUpdated(now);
      const state = await requestJson<{ change_log?: ChangeEvent[] }>("/api/controller/state", { timeoutMs: 3500 }).catch(() => null);
      if (state && Array.isArray(state.change_log)) setChanges(state.change_log);
      setOnline(true);
    } catch {
      setOnline(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    setHover(null);
    void refresh(range);
    const timer = window.setInterval(() => void refresh(range), 60000);
    return () => window.clearInterval(timer);
  }, [range, refresh]);

  const label = RANGES.find(item => item.value === range)?.label ?? "";
  const outdoor = summary(samples, "outdoor_temp");
  const after = summary(samples, AFTER);
  const supplyKey = after.latest !== null ? AFTER : "supply_temp";
  const supply = after.latest !== null ? after : summary(samples, "supply_temp");
  const extract = summary(samples, "extract_temp");
  const co2 = summary(samples, "co2");
  const recovery = summary(samples, "heat_recovery_efficiency");
  const valve = summary(samples, "afterheat_active");
  const span = (item: ReturnType<typeof summary>, decimals: number, unit: string) =>
    item.low === null ? "Ingen data" : `${format(item.low, decimals)}–${format(item.high, decimals)}${unit} i perioden`;
  const shared = { samples, range: window_, hoverTs: hover, onHover: setHover, loading };
  // Gateway 1.9.0 logs HAC1's level (valve opening = level × 10 %) and the user's
  // target; older history has neither, and its whole-degree setpoint was the target then.
  const afterheatSamples = useMemo(() => withAfterheatLines(samples), [samples]);
  const layers = afterheatLayers(afterheatSamples);
  const opening = summary(afterheatSamples, "afterheat_valve_percent");

  return (
    <section className="dashboard-overview page-enter">
      <header className="overview-heading-row">
        <div>
          <span className="eyebrow">HISTORIK</span>
          <h1>Udvikling over tid</h1>
          <p>Peg eller tryk på en graf for at se alle værdier på samme tidspunkt.</p>
        </div>
        <div className="overview-status-pills">
          <div><span className={`status-led ${online ? "" : "warn"}`}/><small>Historik</small><strong>{online ? "Live" : "Afventer"}</strong></div>
        </div>
      </header>

      <div className="hx-toolbar">
        <div className="pro-segment history-range-segment" role="group" aria-label="Periode">
          {RANGES.map(item => (
            <button key={item.value} className={range === item.value ? "active" : ""} aria-pressed={range === item.value} onClick={() => setRange(item.value)}>{item.label}</button>
          ))}
        </div>
        <span className="hx-updated">{updated ? `Opdateret kl. ${new Date(updated * 1000).toLocaleTimeString("da-DK", { hour: "2-digit", minute: "2-digit" })} · gennemsnit pr. punkt` : ""}</span>
      </div>

      <div className="hx-stats">
        <StatTile label="Ude" value={format(outdoor.latest, 1)} unit="°C" detail={span(outdoor, 1, "°C")}/>
        <StatTile label={supplyKey === AFTER ? "Indblæsning" : "Tilluft"} value={format(supply.latest, 1)} unit="°C" detail={span(supply, 1, "°C")}/>
        <StatTile label="Udsugning" value={format(extract.latest, 1)} unit="°C" detail={span(extract, 1, "°C")}/>
        <StatTile label="CO₂" value={format(co2.latest, 0)} unit=" ppm" detail={span(co2, 0, " ppm")}/>
        <StatTile label="Genvinding" value={format(recovery.latest, 0)} unit="%" detail={recovery.mean === null ? "Ingen data" : `Gns. ${format(recovery.mean, 0)}% i perioden`}/>
        {layers.hasValve
          ? <StatTile label="Ventilåbning" value={format(opening.latest, 0)} unit=" %" detail={opening.mean === null ? "Ingen data" : `Gns. ${format(opening.mean, 0)} % i perioden`}/>
          : <StatTile label="Eftervarme" value={valve.mean === null ? "–" : format(valve.mean * 100, 0)} unit="%" detail={`af tiden med åben ventil · ${label}`}/>}
      </div>

      <AfterheatAnalysis samples={samples}/>
      <AfterheatScale/>

      <div className="history-grid">
        <ChartCard wide title="Indblæsning og eftervarme" subtitle="Luften efter varmefladen mod dit mål, setpunktet sendt til HAC1, og ventilens åbning mod styringens beslutning" icon={<Activity size={20}/>}>
          <HistoryChart
            {...shared} samples={afterheatSamples} title="Indblæsning og eftervarme" unit="°C" height={layers.hasValve ? 310 : 270} minSpan={4}
            series={[
              { key: AFTER, label: "Indblæsning · T2AH", slot: 1 },
              { key: "extract_temp", label: "Udsugning · T3", slot: 2 },
            ]}
            band={{ seriesKey: AFTER, minKey: `${AFTER}_min`, maxKey: `${AFTER}_max`, label: "Udsving T2AH" }}
            reference={AFTERHEAT_CHART.goal}
            command={layers.showSent ? AFTERHEAT_CHART.sent : undefined}
            valve={layers.hasValve ? AFTERHEAT_CHART.valve : undefined}
            strip={layers.hasValve ? undefined : AFTERHEAT_CHART.strip}
          />
          <p className="hx-note">{AFTERHEAT_CHART.note}</p>
        </ChartCard>

        <ChartCard title="Eftervarmevand" subtitle="Fremløb, retur og frostføleren på vandsiden" icon={<Activity size={20}/>}>
          <HistoryChart
            {...shared} title="Eftervarmevand" unit="°C" minSpan={6}
            series={[
              { key: "flow_temperature", label: "Fremløb", slot: 1 },
              { key: "return_temperature", label: "Retur", slot: 2 },
              { key: "heating_coil_frost_temperature", label: "Frostføler", slot: 3 },
            ]}
          />
        </ChartCard>

        <ChartCard title="Ude og afkast" subtitle="Udeluften ind og den brugte luft ud af huset" icon={<Activity size={20}/>}>
          <HistoryChart
            {...shared} title="Ude og afkast" unit="°C" minSpan={4}
            series={[
              { key: "outdoor_temp", label: "Udeluft · T1", slot: 1 },
              { key: "exhaust_temp", label: "Afkast · T4", slot: 2 },
            ]}
          />
        </ChartCard>

        <ChartCard title="Ventilatorer" subtitle="Ydelse i procent · omdrejninger i forklaringen" icon={<Fan size={20}/>}>
          <HistoryChart
            {...shared} title="Ventilatorer" unit="%" decimals={0} minSpan={10}
            series={[
              { key: "fan_supply_percent", label: "Indblæsning", slot: 1 },
              { key: "fan_extract_percent", label: "Udsugning", slot: 2 },
            ]}
            extras={[
              { key: "fan_supply_rpm", label: "Ind", unit: " rpm" },
              { key: "fan_extract_rpm", label: "Ud", unit: " rpm" },
            ]}
          />
        </ChartCard>

        <ChartCard title="Luftkvalitet" subtitle="CO₂ i udsugningsluften" icon={<Leaf size={20}/>}>
          <HistoryChart
            {...shared} title="CO₂" unit=" ppm" decimals={0} minSpan={200}
            series={[{ key: "co2", label: "CO₂", slot: 1 }]}
            threshold={{ value: 1000, label: "1.000 ppm" }}
          />
        </ChartCard>

        <ChartCard title="Varmegenvinding" subtitle="Udsugningssiden, beregnet fra T1, T3 og T4" icon={<Recycle size={20}/>}>
          <HistoryChart
            {...shared} title="Varmegenvinding" unit="%" decimals={0} minSpan={10}
            series={[{ key: "heat_recovery_efficiency", label: "Genvinding", slot: 1 }]}
          />
        </ChartCard>

        <AlarmHistory/>

        <article className="surface history-card change-log-card">
          <div className="pro-card-head compact"><div><h2>Ændringer af indstillinger</h2><p>Hvem ændrede hvad · seneste 50</p></div><SlidersHorizontal size={20}/></div>
          {changes.length === 0 ? <div className="history-chart-empty" style={{ height: 80 }}><span>Ingen ændringer logget endnu</span></div> : (
            <ul className="change-log">
              {changes.map(event => Object.entries(event.changes).map(([key, [from, to]]) => (
                <li key={`${event.timestamp}-${key}`}>
                  <time>{new Date(event.timestamp * 1000).toLocaleString("da-DK", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</time>
                  <strong>{SETTING_LABELS[key] ?? key}</strong>
                  <span>{key === "schedule_periods" ? "perioder ændret" : (from !== null && typeof from === "object") || (to !== null && typeof to === "object") ? "ændret" : `${changeValue(key, from)} → ${changeValue(key, to)}`}</span>
                  <em>{sourceLabel(event.source)}</em>
                </li>
              )))}
            </ul>
          )}
        </article>
      </div>
    </section>
  );
}
