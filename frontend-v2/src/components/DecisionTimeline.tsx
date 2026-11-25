import { useState } from "react";
import { History } from "lucide-react";
import { fanChanges, lastedText } from "../lib/decisionLog";

/** "Hvorfor skiftede blæseren?": when the step changed, what decided it and for how long. Read only. */
export function DecisionTimeline({ log }: { log: unknown }) {
  const [all, setAll] = useState(false);
  const changes = fanChanges(log, 200);
  const shown = all ? changes : changes.slice(0, 12);
  return (
    <article className="surface history-card decision-timeline" aria-label="Hvorfor skiftede blæseren">
      <div className="pro-card-head compact"><div><h2>Hvorfor skiftede blæseren?</h2><p>Hvornår trinnet skiftede, hvad der bestemte det, og hvor længe det holdt</p></div><History size={20}/></div>
      {changes.length === 0
        ? <div className="history-chart-empty" style={{ height: 80 }}><span>Ingen skift logget endnu</span></div>
        : <ol className="decision-list">
          {shown.map(change => (
            <li key={change.ts}>
              <time dateTime={new Date(change.ts * 1000).toISOString()}>{new Date(change.ts * 1000).toLocaleString("da-DK", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</time>
              <strong>{change.from === null || change.from === change.to ? `Trin ${change.to ?? "—"}` : `Trin ${change.from} → ${change.to ?? "—"}`}</strong>
              <span className="decision-who">{change.who}</span>
              <span className="decision-reason">{change.reason}</span>
              <em>{change.lasted === null ? "gælder stadig" : `holdt i ${lastedText(change.lasted)}`}</em>
            </li>
          ))}
        </ol>}
      {changes.length > shown.length && <button type="button" className="decision-more" onClick={() => setAll(true)}>Vis alle {changes.length}</button>}
    </article>
  );
}
