import "../styles/panels.css";
import { useMemo } from "react";
import { afterheatAnalysis, type HeatSample } from "../lib/afterheatAnalysis";
import { InfoList } from "./InfoList";
const value = (n: number | null, unit: string) => n === null ? "Ikke nok data" : `${n.toLocaleString("da-DK", { maximumFractionDigits: 1 })}${unit}`;
export function AfterheatAnalysis({ samples }: { samples: HeatSample[] }) {
  const a = useMemo(() => afterheatAnalysis(samples), [samples]);
  return <article className="surface panel-card" aria-label="Eftervarmeanalyse">
    <div className="pro-card-head compact"><div><h2>Hvor roligt holder eftervarmen målet?</h2>
      <p>Analyse af {value(a.minutes, " min")} med gyldige temperaturer og mål.</p></div></div>
    <InfoList rows={[
      { label: "Tid inden for ±0,5 °C", value: value(a.inBandPercent, " %") },
      { label: "Gennemsnitlig afvigelse", value: value(a.meanError, " °C") },
      { label: "Største oversving", value: value(a.overshoot, " °C") },
      { label: "Største undersving", value: value(a.undershoot, " °C") },
      { label: "Observerede ventilskift", value: a.minutes ? String(a.observedValveChanges) : "Ikke nok data" },
      { label: "Stabil efter målændring", value: value(a.settlingMinutes, " min") },
    ]}/>
    <p className="hx-note">Stabil betyder mindst 10 minutter inden for ±0,5 °C. Huller over 5 minutter og slukket eftervarme udelades. Gemte gennemsnit kan skjule korte udsving og ventilskift; analysen er vejledende.</p>
  </article>;
}
