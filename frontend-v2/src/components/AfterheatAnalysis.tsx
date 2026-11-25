import "../styles/panels.css";
import { useMemo, useState } from "react";
import { afterheatAnalysis, AFTERHEAT_CONTEXTS, DEFAULT_BAND, type HeatSample } from "../lib/afterheatAnalysis";
import { InfoList } from "./InfoList";
const value = (n: number | null, unit: string) => n === null ? "Ikke nok data" : `${n.toLocaleString("da-DK", { maximumFractionDigits: 1 })}${unit}`;
export function AfterheatAnalysis({ samples, band = DEFAULT_BAND }: { samples: HeatSample[]; band?: number }) {
  const [context, setContext] = useState("all");
  const a = useMemo(() => afterheatAnalysis(samples, context, band), [samples, context, band]);
  return <article className="surface panel-card" aria-label="Eftervarmeanalyse">
    <div className="pro-card-head compact"><div><h2>Hvor roligt holder eftervarmen målet?</h2>
      <p>Analyse af {value(a.minutes, " min")} med gyldige temperaturer og mål.</p></div></div>
    <label className="heat-context-select">Vurder driftstilstand
      <select value={context} onChange={e => setContext(e.target.value)}>
        {Object.entries(AFTERHEAT_CONTEXTS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select>
    </label>
    <InfoList rows={[
      { label: `Tid inden for ±${band.toLocaleString("da-DK", { maximumFractionDigits: 2 })} °C`, value: value(a.inBandPercent, " %") },
      { label: "Gennemsnitlig afvigelse", value: value(a.meanError, " °C") },
      { label: "Største oversving", value: value(a.overshoot, " °C") },
      { label: "Største undersving", value: value(a.undershoot, " °C") },
      { label: "Observerede ventilskift", value: a.minutes ? String(a.observedValveChanges) : "Ikke nok data" },
      { label: "Stabil efter målændring", value: value(a.settlingMinutes, " min") },
    ]}/>
    <p className="hx-note">Stabil betyder mindst 10 minutter inden for det valgte bånd. Huller over 5 minutter og slukket eftervarme udelades; tid med lukket ventil tæller med, når eftervarmen er tændt. Frost, opstart og ændret ventilatortrin kan vurderes hver for sig. Ældre data og blandede intervaller har ukendt driftstilstand. Gemte gennemsnit kan skjule korte udsving og ventilskift; analysen er vejledende.</p>
  </article>;
}
