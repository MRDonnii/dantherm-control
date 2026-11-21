import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { HistoryChart, type HistoryChartProps, type HistorySeries } from "./HistoryChart";
import { requestJson } from "../lib/api";
import { AFTERHEAT_CHART, afterheatLayers, clean, withAfterheatLines, withWaterDelta, type Sample } from "../lib/historyData";
import "../styles/history.css";

const AFTER = "heating_coil_after_temperature";

export interface SensorHistoryDef {
  title: string;
  /** The measurement the tile shows; the key figures are for this one. */
  key: string;
  unit?: string;
  decimals?: number;
  minSpan?: number;
  /** Extra lines for context, e.g. return next to flow. */
  related?: HistorySeries[];
  band?: HistoryChartProps["band"];
  reference?: HistoryChartProps["reference"];
  strip?: HistoryChartProps["strip"];
  /** The afterheat chart: target, the setpoint sent to HAC1, the valve opening and the control's decision. */
  afterheat?: boolean;
  note?: string;
}

const WATER: HistorySeries[] = [
  { key: "flow_temperature", label: "Fremløb", slot: 1 },
  { key: "return_temperature", label: "Retur", slot: 2 },
];

/** The tiles in the animated unit drawing that open a graph. */
export const SENSOR_HISTORY: Record<string, SensorHistoryDef> = {
  outdoor: { title: "Udeluft · T1", key: "outdoor_temp", minSpan: 4 },
  extract: { title: "Udsugning · T3", key: "extract_temp", minSpan: 3 },
  exhaust: {
    title: "Afkast · T4", key: "exhaust_temp", minSpan: 4,
    related: [{ key: "exhaust_temp", label: "Afkast · T4", slot: 1 }, { key: "outdoor_temp", label: "Udeluft · T1", slot: 2 }],
    note: "Afkastet ligger normalt få grader over udeluften, når varmeveksleren genvinder godt.",
  },
  afterHeater: {
    title: "T2AH efter eftervarme", key: AFTER, minSpan: 4,
    band: { seriesKey: AFTER, minKey: `${AFTER}_min`, maxKey: `${AFTER}_max`, label: "Udsving" },
    reference: AFTERHEAT_CHART.goal,
    afterheat: true,
    note: AFTERHEAT_CHART.note,
  },
  frost: { title: "Frostføler · vandside", key: "heating_coil_frost_temperature", minSpan: 6 },
  flowWater: { title: "Eftervarmevand · Frem", key: "flow_temperature", minSpan: 6, related: WATER },
  returnWater: { title: "Eftervarmevand · Retur", key: "return_temperature", minSpan: 6, related: WATER },
  waterDelta: {
    title: "Eftervarmevand · Afkøl", key: "water_delta", minSpan: 4,
    note: "Fremløb minus retur. Jo større afkøling, jo bedre udnytter fladen vandet.",
  },
  recovery: { title: "Genvinding · udsugningsside (T3/T4)", key: "heat_recovery_efficiency", unit: "%", decimals: 0, minSpan: 10 },
};

const RANGES = [
  { value: "6h", label: "6 timer", seconds: 21600 },
  { value: "24h", label: "24 timer", seconds: 86400 },
  { value: "7d", label: "7 dage", seconds: 604800 },
  { value: "30d", label: "30 dage", seconds: 2592000 },
];

function figure(value: number | null, decimals: number, unit: string) {
  return value === null ? "–" : `${value.toLocaleString("da-DK", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}${unit}`;
}

export function SensorHistoryDialog({ sensor, onClose }: { sensor: SensorHistoryDef; onClose: () => void }) {
  const [range, setRange] = useState("24h");
  const [samples, setSamples] = useState<Sample[]>([]);
  const [window_, setWindow] = useState<{ from: number; to: number } | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [hover, setHover] = useState<number | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const unit = sensor.unit ?? "°C";
  const decimals = sensor.decimals ?? 1;

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const result = await requestJson<{ samples: Sample[] }>(`/history.json?range=${range}`, { timeoutMs: 6000 });
        if (cancelled) return;
        const now = Math.floor(Date.now() / 1000);
        setSamples(withAfterheatLines(withWaterDelta(clean(Array.isArray(result.samples) ? result.samples : []))));
        setWindow({ from: now - (RANGES.find(item => item.value === range)?.seconds ?? 86400), to: now });
        setError("");
      } catch {
        if (!cancelled) setError("Historikken kunne ikke hentes. Prøv igen senere.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    setLoading(true);
    setHover(null);
    void load();
    const timer = window.setInterval(() => void load(), 60000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [range]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKeyDown);
    closeRef.current?.focus();
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const values = samples.map(sample => sample[sensor.key]).filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const latest = values.length ? values[values.length - 1] : null;
  const low = values.length ? Math.min(...values) : null;
  const high = values.length ? Math.max(...values) : null;
  const mean = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
  const series = sensor.related ?? [{ key: sensor.key, label: sensor.title, slot: 1 as const }];
  const label = RANGES.find(item => item.value === range)?.label ?? "";
  const layers = sensor.afterheat ? afterheatLayers(samples) : null;
  // History from before HAC1's level was logged only knows whether the valve was open.
  const strip = sensor.strip ?? (layers && !layers.hasValve ? AFTERHEAT_CHART.strip : undefined);
  const command = layers?.showSent ? AFTERHEAT_CHART.sent : undefined;
  const valve = layers?.hasValve ? AFTERHEAT_CHART.valve : undefined;

  return createPortal(
    <div className="sensor-history-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="sensor-history-dialog surface hx-dialog" role="dialog" aria-modal="true" aria-labelledby="sensor-history-title">
        <div className="sensor-history-heading">
          <div><span className="eyebrow">HISTORIK · {label.toUpperCase()}</span><h2 id="sensor-history-title">{sensor.title}</h2></div>
          <button ref={closeRef} type="button" aria-label="Luk temperaturgraf" onClick={onClose}><X size={20}/></button>
        </div>
        <div className="pro-segment history-range-segment hx-dialog-range" role="group" aria-label="Periode">
          {RANGES.map(item => <button key={item.value} type="button" className={range === item.value ? "active" : ""} aria-pressed={range === item.value} onClick={() => setRange(item.value)}>{item.label}</button>)}
        </div>
        <div className="hx-figures">
          <div><span>Nu</span><strong>{figure(latest, decimals, unit)}</strong></div>
          <div><span>Laveste</span><strong>{figure(low, decimals, unit)}</strong></div>
          <div><span>Gennemsnit</span><strong>{figure(mean, decimals, unit)}</strong></div>
          <div><span>Højeste</span><strong>{figure(high, decimals, unit)}</strong></div>
        </div>
        {error ? <p className="sensor-history-message" role="alert">{error}</p> : (
          <HistoryChart
            samples={samples} series={series} unit={unit} decimals={decimals} height={valve ? 300 : 280} minSpan={sensor.minSpan}
            band={sensor.band} reference={sensor.reference} command={command} strip={strip} valve={valve}
            range={window_} hoverTs={hover} onHover={setHover} loading={loading} title={sensor.title}
          />
        )}
        {sensor.note && <p className="hx-note">{sensor.note}</p>}
      </section>
    </div>, document.body);
}
