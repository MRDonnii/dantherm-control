import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, ExternalLink, RefreshCw, Rocket, ShieldCheck } from "lucide-react";
import { ApiError, postJson, requestJson } from "../lib/api";
import { usePullRefresh } from "../lib/pullRefresh";

type Channel = "stable" | "beta";

interface AuthStatus { csrf?: string | null; }
interface UpdateState {
  running?: boolean;
  channel?: Channel | null;
  started_at?: number | null;
  finished_at?: number | null;
  progress?: number;
  phase?: string;
  detail?: string;
  last_error?: string | null;
  /** The last attempt: from which version to which, and how it ended. */
  from_version?: string | null;
  target_version?: string | null;
  ref?: string | null;
  result?: "complete" | "failed" | "up_to_date" | null;
  /** The last lines of update.sh's output when the attempt failed. */
  log_tail?: string | null;
}
interface UpdateInfo {
  ok?: boolean;
  channel?: Channel;
  current_version?: string;
  current_build?: string;
  available_version?: string;
  available_build?: string | null;
  published_at?: string | null;
  update_available?: boolean;
  /** The installed version is newer than the newest release on this channel. */
  ahead_of_release?: boolean;
  update?: UpdateState;
  message?: string;
  error?: string;
}

function shortBuild(value?: string | null) {
  if (!value || value === "unknown") return "—";
  return value.slice(0, 8);
}
function clamp(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(100, number)) : 0;
}
function phaseLabel(value?: string) {
  const labels: Record<string, string> = {
    idle: "Klar", starting: "Starter", checking: "Kontrollerer", preparing: "Forbereder", downloading: "Downloader",
    extracting: "Pakker ud", validating: "Validerer", installing: "Installerer", verifying: "Verificerer", complete: "Færdig", failed: "Fejlet",
    up_to_date: "Ingen nyere build", dismissed: "Klar",
  };
  return labels[value ?? ""] ?? value ?? "Klar";
}

/** Text for an install that is newer than the newest release, e.g. 1.6.1 from main while stable is 1.6.0. */
export function aheadOfReleaseText(info: UpdateInfo | null): string | null {
  if (!info?.ahead_of_release || info.update_available || !info.current_version || !info.available_version) return null;
  return `Du kører ${info.current_version} – nyere end seneste udgivelse (${info.available_version})`;
}

const RESULT_LABELS: Record<string, string> = { complete: "lykkedes", failed: "fejlede", up_to_date: "intet nyere at installere" };
/** "Sidste forsøg 19.11.2026 kl. 18.57 · 1.6.1 → 1.7.0 · Beta · fejlede", or null before the first attempt. */
export function lastAttemptText(update?: UpdateState | null): string | null {
  if (!update?.started_at) return null;
  const when = new Date(update.started_at * 1000);
  const date = `${when.toLocaleDateString("da-DK", { day: "2-digit", month: "2-digit", year: "numeric" })} kl. ${when.toLocaleTimeString("da-DK", { hour: "2-digit", minute: "2-digit" })}`;
  const versions = update.target_version ? `${update.from_version ?? "?"} → ${update.target_version}` : null;
  const channel = update.channel ? (update.channel === "beta" ? "Beta" : "Stable") : null;
  const result = update.running ? "i gang" : RESULT_LABELS[update.result ?? ""] ?? null;
  return ["Sidste forsøg " + date, versions, channel, result].filter(Boolean).join(" · ");
}
function statusMessage(result: UpdateInfo) {
  return aheadOfReleaseText(result) ?? (result.update_available ? "En nyere build er klar." : "Denne kanal er opdateret.");
}

export function UpdatesPage() {
  const [csrf, setCsrf] = useState("");
  const [channel, setChannel] = useState<Channel>("beta");
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("Henter versionsstatus…");
  const mounted = useRef(true);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const action = useCallback(async (actionName: string, target?: string, extra?: Record<string, unknown>) => {
    return postJson<UpdateInfo>("/api/admin/action", { action: actionName, target, ...extra }, csrf);
  }, [csrf]);

  const refresh = useCallback(async (selected?: Channel) => {
    const wanted = selected ?? channel;
    setBusy(true);
    try {
      const auth = await requestJson<AuthStatus>("/api/auth/status", { timeoutMs: 3500 });
      const token = auth.csrf ?? "";
      if (mounted.current) setCsrf(token);
      const result = await postJson<UpdateInfo>("/api/admin/action", { action: "check_update", target: wanted }, token);
      if (!mounted.current) return;
      setInfo(result);
      if (result.channel) setChannel(result.channel);
      setMessage(statusMessage(result));
    } catch (error) {
      if (!mounted.current) return;
      setMessage(error instanceof ApiError ? error.message : "Kunne ikke hente versionsstatus");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, [channel]);

  usePullRefresh(() => refresh());
  useEffect(() => { void refresh("beta"); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function chooseChannel(next: Channel) {
    if (busy || next === channel) return;
    setBusy(true);
    setMessage(`Skifter til ${next === "beta" ? "Beta" : "Stable"}…`);
    try {
      await action("set_update_channel", next);
      setChannel(next);
      await refresh(next);
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : "Kunne ikke gemme kanal");
      setBusy(false);
    }
  }

  async function installUpdate(force = false) {
    if (busy) return;
    setBusy(true);
    setMessage("Opdateringen er startet. Controlleren fortsætter, mens den nye build valideres…");
    setInfo(current => ({
      ...current,
      update: { ...current?.update, running: true, progress: 2, phase: "starting", detail: "Forbereder sikker opdatering…", last_error: null },
    }));
    try {
      await action("install_update", channel, force ? { force: true } : undefined);
    } catch (error) {
      setBusy(false);
      setInfo(current => ({ ...current, update: { ...current?.update, running: false, progress: 0 } }));
      if (error instanceof ApiError && error.status === 423 && !force) {
        // It freezes: the update waits for thaw unless the user chooses it anyway.
        setMessage(error.message);
        if (window.confirm(`${error.message}\n\nVil du opdatere alligevel?`)) void installUpdate(true);
        return;
      }
      setMessage(error instanceof ApiError ? error.message : "Kunne ikke starte opdateringen");
      return;
    }

    const started = Date.now();
    const timer = window.setInterval(async () => {
      if (!mounted.current) return window.clearInterval(timer);
      try {
        const auth = await requestJson<AuthStatus>("/api/auth/status", { timeoutMs: 2500 });
        const token = auth.csrf ?? csrf;
        const local = await postJson<UpdateInfo>("/api/admin/action", { action: "get_update_status" }, token);
        if (!mounted.current) return;
        setInfo(current => ({ ...current, ...local, update: local.update ?? current?.update }));
        if (!local.update?.running && local.update?.phase === "up_to_date") {
          window.clearInterval(timer);
          setBusy(false);
          setMessage(local.update.detail ?? "Ingen nyere build. Intet installeret.");
          return;
        }
        if (!local.update?.running && local.update?.phase === "deferred") {
          window.clearInterval(timer);
          setBusy(false);
          setMessage(local.update.detail ?? "Opdateringen venter, til det er tøet.");
          return;
        }
        if (local.update?.last_error) {
          window.clearInterval(timer);
          setBusy(false);
          setMessage(`Opdatering rullet tilbage: ${local.update.last_error}`);
          return;
        }
        if (!local.update?.running && clamp(local.update?.progress) >= 100) {
          window.clearInterval(timer);
          setMessage("Opdateringen er installeret. Kontrollerer den nye build…");
          window.setTimeout(async () => {
            try {
              const final = await postJson<UpdateInfo>("/api/admin/action", { action: "check_update", target: channel }, token);
              setInfo(final);
            } catch {}
            window.location.reload();
          }, 1200);
          return;
        }
      } catch {
        // Gateway/admin kan genstarte kortvarigt. Fortsæt polling.
      }
      // The new build is watched for 5 minutes before it is accepted.
      if (Date.now() - started > 900_000) {
        window.clearInterval(timer);
        setBusy(false);
        setMessage("Opdateringen tager længere tid end forventet. Tryk Kontroller igen om lidt.");
      }
    }, 1200);
  }

  async function hideError() {
    if (busy) return;
    setBusy(true);
    try {
      const result = await action("dismiss_update_error");
      setInfo(current => ({ ...current, update: result.update ?? current?.update }));
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : "Kunne ikke skjule fejlen");
    } finally {
      setBusy(false);
    }
  }

  const progress = clamp(info?.update?.progress);
  const lastAttempt = lastAttemptText(info?.update);
  const failed = info?.update?.phase === "failed" && !info?.update?.running && !!info?.update?.last_error;
  const running = info?.update?.running === true;
  const detail = info?.update?.detail ?? (running ? "Installerer…" : message);
  const ahead = aheadOfReleaseText(info);

  return (
    <section className="page-view page-enter">
      <header className="page-hero">
        <div>
          <span className="eyebrow">OPDATERINGER</span>
          <h1>Software og kanal</h1>
          <p>Beta bruges som live udviklingskanal. Updateren validerer build, controller, auth og services før den accepterer en ny version.</p>
        </div>
        <span className={`status-chip${info?.update_available ? "" : " muted"}`}>
          {info?.update_available ? <RefreshCw size={14} /> : <CheckCircle2 size={14} />}
          {info?.update_available ? " Update klar" : ahead ? " Nyere end udgivelsen" : " Opdateret"}
        </span>
      </header>

      <div className="overview-grid-v2">
        <article className="surface control-card-v2">
          <div className="section-head compact">
            <div><span className="eyebrow">KANAL</span><h2>Stable eller Beta</h2></div>
            <ShieldCheck size={22} />
          </div>
          <div className="segmented-v2" role="group" aria-label="Opdateringskanal">
            <button type="button" className={channel === "stable" ? "active" : ""} onClick={() => void chooseChannel("stable")} disabled={busy}>Stable</button>
            <button type="button" className={channel === "beta" ? "active" : ""} onClick={() => void chooseChannel("beta")} disabled={busy}>Beta</button>
          </div>
          <div className="control-summary">
            <span>Installeret</span>
            <strong>{info?.current_version ?? "—"}</strong>
            <small>Build {shortBuild(info?.current_build)}</small>
          </div>
          <div className="control-summary">
            <span>{ahead ? "Seneste udgivelse" : "Tilgængelig"}</span>
            <strong>{info?.available_version ?? "—"}</strong>
            <small>Build {shortBuild(info?.available_build)}</small>
          </div>
          {ahead && <div className="control-notice update-ahead" role="status">{ahead}</div>}
        </article>

        <aside className="control-column">
          <article className="surface quick-card">
            <div className="quick-icon green"><Rocket size={20}/></div>
            <div><span>Status</span><strong>{message}</strong></div>
            <button type="button" onClick={() => void refresh()} disabled={busy}>Kontroller</button>
          </article>
          <article className="surface control-card-v2">
            <div className="section-head compact"><div><span className="eyebrow">FAILSAFE</span><h2>Installer næste build</h2></div></div>
            <p>Den valgte kanal gemmes på Pi’en. Ved fejl ruller updateren applikationsfilerne tilbage og holder controlleren i sikker drift.</p>
            <div className="update-progress-panel" aria-live="polite">
              <div className="update-progress-meta"><strong>{detail}</strong><span>{Math.round(progress)}%</span></div>
              <div className="update-progress-track"><i style={{ width: `${progress}%` }}/></div>
              <div className="update-stage">{phaseLabel(info?.update?.phase)}</div>
            </div>
            {lastAttempt && <div className="update-last-attempt">{lastAttempt}</div>}
            {failed && <div className="control-notice error update-failure" role="alert">
              <div className="update-failure-head"><strong>{info?.update?.last_error}</strong><button type="button" className="secondary-action" onClick={() => void hideError()} disabled={busy}>Skjul</button></div>
              {info?.update?.log_tail && <details className="update-log-tail"><summary>Sidste linjer fra update.sh</summary><pre>{info.update.log_tail}</pre></details>}
            </div>}
            <button type="button" className="primary-action" onClick={() => void installUpdate()} disabled={busy || running || !info?.update_available}>
              {running ? "Installerer…" : busy ? "Arbejder…" : info?.update_available ? "Opdater nu" : "Ingen update"}
            </button>
          </article>
        </aside>
      </div>
      <article className="surface control-card-v2" style={{ marginTop: 20 }}>
        <div className="section-head compact"><div><span className="eyebrow">HOME ASSISTANT</span><h2>Integration og dashboardkort</h2></div><ExternalLink size={22}/></div>
        <p>Home Assistant opdaterer integrationen og kortet via HACS. Åbn dem på den enhed, hvor du er logget ind i Home Assistant.</p>
        <div className="update-ha-links">
          <a className="primary-action" href="https://my.home-assistant.io/redirect/hacs_repository/?owner=MRDonnii&repository=dantherm-control-ha&category=integration" target="_blank" rel="noopener noreferrer">Åbn HA-integrationen i HACS <ExternalLink size={16}/></a>
          <a className="primary-action" href="https://my.home-assistant.io/redirect/hacs_repository/?owner=MRDonnii&repository=ha-smart-home-cards&category=plugin" target="_blank" rel="noopener noreferrer">Åbn dashboardkort i HACS <ExternalLink size={16}/></a>
          <a href="https://github.com/MRDonnii/dantherm-control/blob/main/docs/kom-godt-i-gang.da.md" target="_blank" rel="noopener noreferrer">Guide: kom godt i gang <ExternalLink size={16}/></a>
        </div>
      </article>
    </section>
  );
}
