import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Power, RotateCw, X } from "lucide-react";
import { requestJson } from "../lib/api";
import { RESTART_ACTIONS, restartStep, startRestartWatch, type RestartKind, type RestartWatch } from "../lib/restart";

const POLL_MS = 2_000;

const COPY: Record<RestartKind, { button: string; title: string; lead: string; detail: string; confirm: string; waiting: string; down: string }> = {
  services: {
    button: "Genstart Dantherm-styringen",
    title: "Genstart Dantherm-styringen?",
    lead: "Tjenesterne på Pi’en (styring, WebUI og 1-Wire-følere) stoppes og startes igen.",
    detail: "WebUI og styringen er væk i et kort øjeblik. Siden venter og indlæser sig selv igen, når styringen svarer.",
    confirm: "Genstart styringen",
    waiting: "Genstarter Dantherm-styringen …",
    down: "Styringen starter op igen …",
  },
  pi: {
    button: "Genstart Pi’en",
    title: "Genstart Raspberry Pi’en?",
    lead: "Hele Pi’en genstarter. Det tager typisk 1–2 minutter.",
    detail: "Imens er WebUI, styringen og følerne væk. Siden venter og indlæser sig selv igen, når Pi’en er oppe.",
    confirm: "Genstart Pi’en",
    waiting: "Genstarter Pi’en …",
    down: "Pi’en starter op igen …",
  },
};

/** Restart of the control services or the whole Pi, for the protected System
 *  page. Nothing happens until the user confirms in the dialog. */
export function RestartPanel({ csrf }: { csrf: string }) {
  const [asking, setAsking] = useState<RestartKind | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [watch, setWatch] = useState<RestartWatch | null>(null);
  const [timedOut, setTimedOut] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!asking) return;
    cancelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape" && !sending) setAsking(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [asking, sending]);

  // Poll until the Pi answers again, then reload the page.
  useEffect(() => {
    if (!watch || timedOut) return;
    let cancelled = false;
    let timer = 0;
    let current = watch;
    const poll = async () => {
      let answered = false;
      try { await requestJson("/api/auth/status", { timeoutMs: 2_500 }); answered = true; } catch { answered = false; }
      if (cancelled) return;
      const step = restartStep(current, answered, Date.now());
      current = step.watch;
      setNow(Date.now());
      if (step.outcome === "reload") { window.location.reload(); return; }
      if (step.outcome === "timeout") { setWatch(current); setTimedOut(true); return; }
      if (current.sawDown !== watch.sawDown) setWatch(current);
      timer = window.setTimeout(() => void poll(), POLL_MS);
    };
    timer = window.setTimeout(() => void poll(), POLL_MS);
    return () => { cancelled = true; window.clearTimeout(timer); };
  // Only a new restart or a new phase starts the loop again.
  }, [watch?.startedAt, watch?.sawDown, timedOut]);

  const confirm = async () => {
    if (!asking) return;
    const kind = asking;
    setSending(true); setError("");
    try {
      await requestJson("/api/admin/action", {
        method: "POST", timeoutMs: 8_000,
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify(RESTART_ACTIONS[kind]),
      });
      setAsking(null); setTimedOut(false);
      const started = Date.now();
      setNow(started); setWatch(startRestartWatch(kind, started));
    } catch (failure) {
      const message = failure instanceof Error ? failure.message : "";
      setError(/Kun teknikere/.test(message) ? "Kun teknikere og administratorer kan genstarte."
        : /sikkerhedstoken/i.test(message) ? "Sikkerhedstoken mangler. Genindlæs siden og prøv igen."
        : "Genstarten kunne ikke sættes i gang. Prøv igen.");
      setAsking(null);
    } finally {
      setSending(false);
    }
  };

  const seconds = watch ? Math.max(0, Math.round((now - watch.startedAt) / 1000)) : 0;
  const copy = asking ? COPY[asking] : watch ? COPY[watch.kind] : null;

  return <article className="surface system-panel system-restart">
    <div className="pro-card-head compact"><div><h2>Genstart</h2><p>Kun når du selv trykker – aldrig automatisk</p></div><RotateCw size={19}/></div>
    <div className="system-restart-actions">
      <button type="button" className="secondary-action" disabled={!csrf || sending || !!watch} onClick={() => setAsking("services")}><RotateCw size={15}/>{COPY.services.button}</button>
      <button type="button" className="danger-outline" disabled={!csrf || sending || !!watch} onClick={() => setAsking("pi")}><Power size={15}/>{COPY.pi.button}</button>
    </div>
    <p className="system-footnote">Styringen: tjenesterne genstartes, og WebUI er væk i få sekunder. Pi’en: hele computeren genstarter (1–2 minutter).</p>
    {error && <p className="system-notice" role="alert">{error}</p>}

    {asking && copy && createPortal(<div className="restart-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !sending) setAsking(null); }}>
      <section className="restart-dialog surface" role="alertdialog" aria-modal="true" aria-labelledby="restart-dialog-title" aria-describedby="restart-dialog-lead">
        <div className="restart-dialog-heading"><div><span className="eyebrow">SYSTEM</span><h2 id="restart-dialog-title">{copy.title}</h2></div><button type="button" aria-label="Luk" disabled={sending} onClick={() => setAsking(null)}><X size={20}/></button></div>
        <p id="restart-dialog-lead" className="restart-dialog-lead">{copy.lead}</p>
        <p className="restart-dialog-detail">{copy.detail}</p>
        <div className="restart-dialog-actions">
          <button ref={cancelRef} type="button" className="secondary-action" disabled={sending} onClick={() => setAsking(null)}>Annullér</button>
          <button type="button" className="danger-outline" disabled={sending} onClick={() => void confirm()}>{sending ? "Sender …" : copy.confirm}</button>
        </div>
      </section>
    </div>, document.body)}

    {watch && createPortal(<div className="restart-backdrop restart-waiting" role="status" aria-live="polite">
      <section className="restart-dialog surface">
        {timedOut
          ? <><h2>{watch.kind === "pi" ? "Pi’en svarer ikke endnu" : "Styringen svarer ikke endnu"}</h2>
              <p className="restart-dialog-lead">Der er gået {Math.round(seconds / 60)} minutter. Tjek strøm og netværk til Pi’en, og prøv at genindlæse siden.</p>
              <div className="restart-dialog-actions"><button type="button" className="primary-action" onClick={() => window.location.reload()}>Genindlæs nu</button></div></>
          : <><span className="restart-spinner" aria-hidden="true"/>
              <h2>{watch.sawDown ? COPY[watch.kind].down : COPY[watch.kind].waiting}</h2>
              <p className="restart-dialog-lead">Siden indlæser sig selv igen, når {watch.kind === "pi" ? "Pi’en" : "styringen"} svarer. {seconds} sek.</p></>}
      </section>
    </div>, document.body)}
  </article>;
}
