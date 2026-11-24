import "../styles/panels.css"; import "../styles/management.css";
import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { InfoList } from "../components/InfoList";
import { haRoomsStatus, type HaRoomsHealth } from "../lib/haRoomsHealth";
import { requestJson } from "../lib/api";
import { usePullRefresh } from "../lib/pullRefresh";

type Period = { samples: number; gap_minutes: number; supply_minutes: number; target_mean: number | null; air_min: number | null;
  air_max: number | null; error_mean: number | null; error_sd: number | null; max_over: number | null; max_under: number | null;
  in_band_percent: number | null; level_changes: number; level_changes_per_hour: number | null; level_changes_max_hour: number;
  fan_changes: number; stale_minutes: Record<string, number> };
export type HealthReport = { generated_at: number; since: number; days: number; total: Period; per_day: (Period & { date: string })[];
  command_errors: { alarms: number; recent: { time: number; text: string }[]; pending_now: string[] };
  watchdog_restarts: { count: number; recent: { at: number; reason: string }[] };
  stale: { sensors: Record<string, string>; alarms: number; now: string[]; ha_rooms?: HaRoomsHealth | null }; note: string };

const n = (value: number | null | undefined, digits = 1, unit = "") => value === null || value === undefined ? "–"
  : `${value.toLocaleString("da-DK", { minimumFractionDigits: digits, maximumFractionDigits: digits })}${unit}`;
// data_health keys in "now"; HA rooms count only measurements that are too old.
const NOW_LABELS: Record<string, string> = { rh: "Fugt", co2: "CO₂", outdoor: "Ude", room: "Rum", supply: "Indblæsning", ha_rooms: "HA-rum" };
const when = (ts: number) => new Date(ts * 1000).toLocaleString("da-DK", { weekday: "short", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });
const day = (date: string) => new Date(`${date}T12:00:00`).toLocaleDateString("da-DK", { weekday: "short", day: "numeric", month: "numeric" });

/** Read-only weekly health report: supply-air swings, level changes, command errors, watchdog restarts, stale measurements. */
export function HealthReportPage() {
  const [report, setReport] = useState<HealthReport | null>(null);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true); setError("");
    try { setReport(await requestJson<HealthReport>("/api/health/weekly", { timeoutMs: 20000 })); }
    catch (e) { setError(e instanceof Error ? e.message : "Kunne ikke hente ugerapporten"); }
    finally { setBusy(false); }
  }, []);
  usePullRefresh(load);
  useEffect(() => { void load(); }, [load]);
  const t = report?.total;
  const staleTotal = t ? Object.values(t.stale_minutes).reduce((a, b) => a + b, 0) : 0;
  return <section className="dashboard-overview page-enter">
    <header className="overview-heading-row"><div><span className="eyebrow">UGERAPPORT</span><h1>Sundhed de seneste 7 dage</h1>
      <p>Indblæsningens udsving, trinskift, kommandofejl, genstarter og for gamle målinger. Kun visning; intet ændres.</p></div>
      <div className="diag-actions"><button className="secondary-action" disabled={busy} onClick={() => void load()}><RefreshCw size={15}/> {busy ? "Henter…" : "Opdatér"}</button></div></header>
    {error && <p role="alert">{error}</p>}
    {report && t && <>
      <div className="panel-grid">
        <article className="surface panel-card" aria-label="Indblæsning mod målet"><div className="pro-card-head compact"><div><h2>Indblæsning mod målet</h2>
          <p>{t.supply_minutes.toLocaleString("da-DK")} min med eftervarmen aktiv</p></div></div>
          <InfoList rows={[
            { label: "Spredning (standardafvigelse)", value: n(t.error_sd, 2, " K"), tone: t.error_sd === null ? "neutral" : t.error_sd <= 0.5 ? "ok" : t.error_sd <= 1 ? "warn" : "bad" },
            { label: "Laveste / højeste", value: `${n(t.air_min, 1, " °C")} / ${n(t.air_max, 1, " °C")}` },
            { label: "Gennemsnitligt mål", value: n(t.target_mean, 1, " °C") },
            { label: "Gennemsnitlig afvigelse", value: n(t.error_mean, 2, " K") },
            { label: "Tid inden for ±0,5 °C", value: n(t.in_band_percent, 0, " %") },
            { label: "Største over- / undersving", value: `${n(t.max_over, 1, " K")} / ${n(t.max_under, 1, " K")}` },
          ]}/></article>
        <article className="surface panel-card" aria-label="Trinskift"><div className="pro-card-head compact"><div><h2>Trinskift</h2><p>Eftervarmens HAC1-trin og blæseren</p></div></div>
          <InfoList rows={[
            { label: "Eftervarme: skift pr. time", value: n(t.level_changes_per_hour, 1) },
            { label: "Eftervarme: flest skift på én time", value: String(t.level_changes_max_hour), tone: t.level_changes_max_hour > 12 ? "warn" : "neutral" },
            { label: "Eftervarme: skift i alt", value: t.level_changes.toLocaleString("da-DK") },
            { label: "Blæser: trinskift i alt", value: t.fan_changes.toLocaleString("da-DK") },
            { label: "Huller i historikken", value: `${t.gap_minutes.toLocaleString("da-DK")} min`, tone: t.gap_minutes > 30 ? "warn" : "ok" },
          ]}/></article>
        <article className="surface panel-card" aria-label="Fejl og genstarter"><div className="pro-card-head compact"><div><h2>Fejl og genstarter</h2><p>Alarmloggen og vagthundens genstarter</p></div></div>
          <InfoList rows={[
            { label: "Alarmer for fejlede kommandoer", value: String(report.command_errors.alarms), tone: report.command_errors.alarms ? "warn" : "ok" },
            { label: "Kommandoer der venter nu", value: report.command_errors.pending_now.length ? report.command_errors.pending_now.join(", ") : "Ingen", tone: report.command_errors.pending_now.length ? "bad" : "ok" },
            { label: "Genstarter fra vagthunden", value: String(report.watchdog_restarts.count), tone: report.watchdog_restarts.count ? "warn" : "ok" },
            ...report.watchdog_restarts.recent.map(item => ({ label: `Genstart · ${when(item.at)}`, value: item.reason })),
            ...report.command_errors.recent.map(item => ({ label: `Kommandofejl · ${when(item.time)}`, value: item.text })),
          ]}/></article>
        <article className="surface panel-card" aria-label="For gamle målinger"><div className="pro-card-head compact"><div><h2>For gamle målinger</h2><p>Minutter hvor en lokal måling var for gammel</p></div></div>
          <InfoList rows={[
            { label: "Alarmer for gamle målinger", value: String(report.stale.alarms), tone: report.stale.alarms ? "warn" : "ok" },
            { label: "For gamle lige nu", value: report.stale.now.length ? report.stale.now.map(key => NOW_LABELS[key] ?? key).join(", ") : "Ingen", tone: report.stale.now.length ? "warn" : "ok" },
            ...(report.stale.ha_rooms ? [{ label: "HA-rum lige nu", ...haRoomsStatus(report.stale.ha_rooms) }] : []),
            ...Object.entries(report.stale.sensors).map(([key, label]) => ({ label, value: `${(t.stale_minutes[key] ?? 0).toLocaleString("da-DK")} min`,
              tone: (t.stale_minutes[key] ? "warn" : "ok") as "warn" | "ok" })),
          ]}/>{staleTotal === 0 && <p className="hx-note">Ingen for gamle målinger registreret.</p>}</article>
      </div>
      <article className="surface panel-card" aria-label="Dag for dag"><div className="pro-card-head compact"><div><h2>Dag for dag</h2></div></div>
        <div className="settings-table-wrap"><table className="settings-table">
          <thead><tr><th>Dag</th><th>Minutter</th><th>Indblæsning min–maks</th><th>Spredning</th><th>±0,5 °C</th><th>Trinskift/time</th><th>Flest på en time</th><th>For gamle (min)</th><th>Huller</th></tr></thead>
          <tbody>{report.per_day.map(row => <tr key={row.date}>
            <td>{day(row.date)}</td><td>{row.samples.toLocaleString("da-DK")}</td><td>{n(row.air_min, 1)}–{n(row.air_max, 1, " °C")}</td>
            <td>{n(row.error_sd, 2, " K")}</td><td>{n(row.in_band_percent, 0, " %")}</td><td>{n(row.level_changes_per_hour, 1)}</td>
            <td>{row.level_changes_max_hour}</td><td>{Object.values(row.stale_minutes).reduce((a, b) => a + b, 0).toLocaleString("da-DK")}</td>
            <td>{row.gap_minutes.toLocaleString("da-DK")} min</td></tr>)}</tbody>
        </table></div>
        <p className="hx-note">{report.note} Genstarter gemmes fra 1.13.0-beta.1; alarmloggen rummer de seneste 500 hændelser.</p>
      </article>
    </>}
  </section>;
}
