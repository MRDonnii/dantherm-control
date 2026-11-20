import { NavLink } from "react-router-dom";
import { haLinkText, readHaLink } from "../lib/haLink";
import { useSession } from "../lib/session";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ArrowRight, CloudFog, Flame, Gauge, House, Leaf, Power, Snowflake, Wind, X, Zap } from "lucide-react";
import { Hch5UnitDiagram } from "../components/Hch5UnitDiagram";
import { AfterheatThermostat } from "../components/AfterheatThermostat";
import { AfterheatLevelPanel } from "../components/AfterheatLevel";
import { readAfterheatControl } from "../lib/afterheatControl";
import { describeControl } from "../lib/control";
import { FROST_COOL_HINT, FROST_COOL_MESSAGE, FROST_OFF_MESSAGE, frostFloorMessage, frostLockRefuses, readFrostLock } from "../lib/frostLock";
import { SENSOR_HISTORY, SensorHistoryDialog } from "../components/SensorHistoryDialog";
import { postJson, requestJson } from "../lib/api";
import { usePollHealth, useSinglePoll } from "../lib/connection";
import { bypassTravel, formatRemaining } from "../lib/bypass";
import { useTopbarNotice } from "../lib/topbar-notice";
import { LEVEL_NAMES, MAX_LEVEL_HOURS, levelList, stepCount } from "../lib/fanSteps";
import { OverviewSide } from "../components/OverviewSide";
import { useSkin } from "../lib/appearance";
import "../styles/overview.css";

type Data = Record<string, unknown>;
type AfterheatValue = number | "off";
type AuthState = { csrf?: string | null };

function number(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
function first(source: Data, ...keys: string[]) {
  for (const key of keys) {
    const value = number(source[key]);
    if (value !== null) return value;
  }
  return null;
}
function text(value: unknown, fallback = "—") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}
function temp(value: number | null) {
  // Non-breaking space keeps the value and its unit on one line.
  return value === null ? "—" : `${value.toLocaleString("da-DK", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}\u00a0°C`;
}
function whole(value: number | null) {
  return value === null ? "—" : Math.round(value).toLocaleString("da-DK");
}
const BONFIRE_CHOICES: [number, string][] = [[30, "30 min"], [60, "1 t"], [120, "2 t"], [180, "3 t"]];
// Presets in the OFF popup; -2 = until 07:00, -1 = until switched on again.
const COOL_CHOICES: [number, string][] = [
  [30, "30 min"],
  [60, "1 time"],
  [120, "2 timer"],
];
const STANDBY_CHOICES: [number, string, string][] = [
  [60, "1 time", "Tænder selv om en time"],
  [240, "4 timer", "Tænder selv om fire timer"],
  [480, "8 timer", "Tænder selv om otte timer"],
  [-2, "Til i morgen", "Tænder selv kl. 07:00"],
  [-1, "Permanent", "Slukket, til du tænder igen"],
];
function energy(value: number | null) {
  return value === null ? "—" : value.toLocaleString("da-DK", { maximumFractionDigits: 2 });
}
function cost(energyKwh: number | null, price: number | null) {
  return energyKwh === null || price === null ? "—" : `${(energyKwh * price).toLocaleString("da-DK", { maximumFractionDigits: 2 })} kr`;
}
function modeLabel(value: unknown) {
  return ({ local_auto: "Local Auto", smart_auto: "Smart Auto", manual: "Manuel" } as Record<string, string>)[String(value)] ?? text(value);
}
function masterLabel(value: unknown) {
  if (value === "pi") return "Raspberry Pi";
  if (value === "hcp4") return "HCP4";
  return "Afventer";
}
function coolingLabel(value: unknown) {
  const labels: Record<string, string> = {
    disabled: "Standby", standby: "Standby", qualifying: "Kvalificerer", opening: "Åbner bypass",
    active: "Aktiv", minimum_on_hold: "Minimum køretid", minimum_off_hold: "Minimum pause",
    outdoor_too_cold: "Ude for kold", not_cooler_outside: "Ude ikke koldere", room_below_start: "Rum under start",
    room_satisfied: "Rumtemperatur nået", sensor_missing: "Mangler sensor", manual_mode: "Manuel mode", vacation: "Ferie",
  };
  return labels[String(value)] ?? text(value).replaceAll("_", " ");
}
function remaining(value: unknown) {
  const seconds = number(value);
  if (seconds === null || seconds <= 0) return "Ikke aktiv";
  const minutes = Math.ceil(seconds / 60);
  return `${minutes} min tilbage`;
}

/** Én midlertidig funktion på forsiden: ikon, navn, tilstand og en knaprække nederst, ens for alle fem. */
function FunctionTile({ active, icon, tone, label, state, hint, children }: { active: boolean; icon: ReactNode; tone?: "flame" | "smoke"; label: string; state: string; hint: string; children: ReactNode }) {
  return (
    <article className={`ovx-fn${active ? " active" : ""}`}>
      <div className="ovx-fn-head">
        <div className={`status-action-icon${tone ? ` ${tone}` : ""}`}>{icon}</div>
        <div className="ovx-fn-text"><span>{label}</span><strong>{state}</strong><small>{hint}</small></div>
      </div>
      <div className="fireplace-actions ovx-fn-buttons">{children}</div>
    </article>
  );
}

export function OverviewPage() {
  const session = useSession();
  const skin = useSkin();
  const [unit, setUnit] = useState<Data>({});
  const [controller, setController] = useState<Data>({});
  const [csrf, setCsrf] = useState("");
  const [online, reportPoll] = usePollHealth(3);
  const [busy, setBusy] = useState<string | null>(null);
  const { setNotice } = useTopbarNotice();
  const [afterheatDraft, setAfterheatDraft] = useState<AfterheatValue | null>(null);
  // A confirmed value shown while it is being saved; not a draft needing confirmation.
  const [afterheatSent, setAfterheatSent] = useState<AfterheatValue | null>(null);
  const afterheatSeq = useRef(0);
  // Setpoint to return to when the power button turns the afterheat back on.
  const lastAfterheatOn = useRef(20);
  const [activeSensor, setActiveSensor] = useState<string | null>(null);
  const closeSensor = useCallback(() => setActiveSensor(null), []);
  const [standbyDialog, setStandbyDialog] = useState(false);
  const [coolDialog, setCoolDialog] = useState(false);

  const refresh = useSinglePoll(useCallback(async () => {
    const [unitResult, controllerResult, authResult] = await Promise.allSettled([
      requestJson<Data>("/state.json", { timeoutMs: 3500 }),
      requestJson<Data>("/api/controller/state?compact=1", { timeoutMs: 3500 }),
      requestJson<AuthState>("/api/auth/status", { timeoutMs: 3500 }),
    ]);
    if (unitResult.status === "fulfilled") setUnit(unitResult.value);
    if (controllerResult.status === "fulfilled") setController(controllerResult.value);
    if (authResult.status === "fulfilled") setCsrf(authResult.value.csrf ?? "");
    reportPoll(unitResult.status === "fulfilled" && controllerResult.status === "fulfilled");
  }, [reportPoll]));

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const command = useCallback(async (name: string, patch: Data, success: string) => {
    if (!csrf) {
      setNotice("Sikkerhedstoken mangler. Genindlæs siden og prøv igen.");
      return;
    }
    setBusy(name);
    setNotice("Gemmer…");
    try {
      const next = await postJson<Data>("/api/controller/config", patch, csrf);
      setController(next);
      setNotice(success);
    } catch (error) {
      setNotice(`Kunne ikke gemme: ${error instanceof Error ? error.message : "ukendt fejl"}`);
      await refresh();
    } finally {
      setBusy(null);
    }
  }, [csrf, refresh]);


  const sendAfterheat = useCallback(async (target: AfterheatValue, seq: number) => {
    await command(
      "afterheat",
      target === "off" ? { afterheat_enabled: false } : { afterheat_setpoint: target },
      target === "off" ? "Eftervarmen er sat til OFF." : `Eftervarmen er sat til ${target} °C.`,
    );
    if (afterheatSeq.current === seq) setAfterheatSent(null);
  }, [command]);


  const outdoor = first(unit, "outdoor_temp", "outdoor_temperature");
  const extract = first(unit, "extract_temp", "extract_temperature");
  const exhaust = first(unit, "exhaust_temp", "exhaust_temperature");
  const afterHeater = first(controller, "actual_supply_air_temperature") ?? first(unit, "heating_coil_after_temperature");
  const frost = first(controller, "actual_afterheat_frost_temperature") ?? first(unit, "heating_coil_frost_temperature");
  const flowWater = first(unit, "flow_temperature");
  const returnWater = first(unit, "return_temperature");
  const supplyRpm = first(controller, "actual_fan_supply_rpm") ?? first(unit, "fan_supply_rpm");
  const extractRpm = first(controller, "actual_fan_extract_rpm") ?? first(unit, "fan_extract_rpm");
  const supplyPercent = first(controller, "actual_fan_supply_percent") ?? first(unit, "fan_supply_percent");
  const extractPercent = first(controller, "actual_fan_extract_percent") ?? first(unit, "fan_extract_percent");
  const humidity = first(unit, "humidity", "relative_humidity");
  const co2 = first(unit, "co2");
  // The unit's own filter counter (HAC1 block 1024) via the controller, else the gateway timer.
  const filterLife = number(controller.filter_life_percent) ?? first(unit, "filter_life_percent");
  const filterDays = number(controller.filter_days_remaining);
  const filterHours = number(controller.filter_hours_since_change);
  const filterChange = number(controller.filter_power_change_percent);
  const filterLearning = controller.filter_reference_learning === true;
  const bypassActual = unit.bypass_active === true || controller.actual_bypass === true;
  const bypassRaw = number(controller.actual_bypass_raw) ?? number(unit.bypass_raw);
  const bypassMoving = bypassRaw !== null && bypassRaw !== 0 && bypassRaw !== 255;
  const bypassRequest = String(controller.actual_bypass_request ?? unit.bypass_request ?? controller.bypass ?? "off");
  // The damper takes about three minutes: say which way it runs, how far
  // along it is and how long is left, from the moment On is read back.
  const bypassTravelDirection = controller.actual_bypass_travel_direction as string | null | undefined;
  const bypassTravelSeconds = number(controller.actual_bypass_travel_seconds);
  const bypassTravelTotal = number(controller.bypass_travel_expected_seconds);
  const bypassRun = bypassTravel({ raw: bypassRaw, requestOn: bypassRequest.toLowerCase() === "on", direction: bypassTravelDirection, seconds: bypassTravelSeconds, total: bypassTravelTotal });
  const bypassActualLabel = bypassRun
    ? [
        bypassRun.direction === "opening" ? "åbner" : bypassRun.direction === "closing" ? "lukker" : "bevæger sig",
        bypassRun.percent === null ? null : `${bypassRun.percent} %`,
        bypassRun.awaitingEnd ? "afventer endestilling" : null,
        bypassRun.remainingSeconds === null ? null : `${formatRemaining(bypassRun.remainingSeconds)} tilbage`,
      ].filter(Boolean).join(" · ")
    : bypassActual ? "åben" : "lukket";
  const heatKnown = typeof controller.actual_afterheat === "boolean" || typeof unit.afterheat_active === "boolean";
  const heating = controller.actual_afterheat === true || unit.afterheat_active === true;
  // HAC1 never heats at 15 C outdoor or above; say so instead of just "Inaktiv".
  const afterheatLockout = controller.actual_afterheat_outdoor_lockout === true;
  const afterheatCutoff = number(controller.afterheat_outdoor_cutoff) ?? 15;
  const afterheatStatus = heating ? "Aktiv" : afterheatLockout ? "Spærret af sommerstop" : heatKnown ? "Inaktiv" : "Ukendt";
  const fireplace = controller.actual_fireplace === true || unit.fireplace === true;
  const bonfireActive = controller.bonfire_active === true;
  const standbyActive = controller.standby_active === true;
  const mode = String(controller.mode ?? "local_auto");
  const level = number(controller.effective_level) ?? 3;
  const afterheatSetpoint = number(controller.afterheat_setpoint) ?? 20;
  const afterheatEnabled = controller.afterheat_enabled !== false;
  // While it freezes the Pi keeps the afterheat on at the floor or above, even
  // when the stored choice is OFF or lower; show what is actually sent to HAC1.
  const frostLock = readFrostLock(controller);
  // HAC1's level 0..10 and who regulates it (Pi or HAC1); absent on older firmware.
  const afterheatControl = readAfterheatControl(controller);
  const committedAfterheat: AfterheatValue = afterheatSent ?? (frostLock.locked
    ? Math.max(afterheatSetpoint, frostLock.floor)
    : afterheatEnabled ? afterheatSetpoint : "off");
  const shownAfterheat: AfterheatValue = afterheatDraft ?? committedAfterheat;
  if (typeof afterheatSetpoint === "number") lastAfterheatOn.current = afterheatSetpoint;
  const actualAfterheatSelectionNumber = number(controller.actual_afterheat_selection);
  const actualAfterheatSelection = controller.actual_afterheat_selection === "off"
    ? "OFF"
    : actualAfterheatSelectionNumber !== null
      ? `${whole(actualAfterheatSelectionNumber)} °C`
      : "Afventer";
  const busHealthy = controller.rs485_healthy === true || unit.bus_traffic === true || unit.available === true;
  const quickBoostActive = (number(controller.quick_boost_remaining_seconds) ?? 0) > 0;
  const coolActive = (number(controller.cool_boost_remaining_seconds) ?? 0) > 0;

  const recovery = useMemo(() => {
    if (bypassActual || outdoor === null || extract === null || exhaust === null || Math.abs(extract - outdoor) < .5) return null;
    const value = ((extract - exhaust) / (extract - outdoor)) * 100;
    return value >= 0 && value <= 105 ? Math.round(value) : null;
  }, [bypassActual, outdoor, extract, exhaust]);
  const correctedT2 = number(controller.corrected_t2_temperature);
  const t2Recovery = number(controller.supply_recovery_percent);
  const t2Difference = number(controller.afterheat_lift);
  const t2NeedsCheck = heatKnown && !heating && t2Difference !== null && t2Difference >= 0.5;

  const levelPatch = mode === "manual" ? "manual_level" : "local_normal_level";
  const levels = levelList(controller);
  const dantherm = stepCount(controller) === 4;
  // Remote-style range: OFF - 10 - 11 ... 35. Minus below 10 selects OFF.
  // +/-, the dial and the power button only move a draft; nothing is sent
  // before the user confirms, so a slip cannot change the afterheat.
  const setAfterheatTarget = (next: AfterheatValue) => {
    if (frostLockRefuses(frostLock, next)) return;
    if (next !== "off") lastAfterheatOn.current = next;
    setAfterheatDraft(next === committedAfterheat ? null : next);
  };
  const confirmAfterheat = () => {
    if (afterheatDraft === null) return;
    // The lock may have engaged while a draft was open; the Pi would refuse it.
    if (frostLockRefuses(frostLock, afterheatDraft)) {
      setNotice(afterheatDraft === "off" ? FROST_OFF_MESSAGE : frostFloorMessage(frostLock.floor));
      setAfterheatDraft(null);
      return;
    }
    const seq = ++afterheatSeq.current;
    setAfterheatSent(afterheatDraft);
    setAfterheatDraft(null);
    void sendAfterheat(afterheatDraft, seq);
  };

  const canConfigure = session.can("configure");
  const haLink = readHaLink(controller);
  const haInfo = haLinkText(haLink);
  const haWarning = haLink.required && haLink.state !== null && haLink.state !== "online" && haLink.state !== "waiting";

  const afterheatCard = (
    <article className="surface afterheat-setpoint-card ovx-heat">
      <AfterheatThermostat value={shownAfterheat} onChange={setAfterheatTarget} heating={heating} lockout={afterheatLockout}
        cutoff={afterheatCutoff} outdoor={outdoor} airBefore={correctedT2 ?? number(controller.actual_supply_before_heater_temperature)} airAfter={afterHeater}
        registered={actualAfterheatSelection} lastOn={lastAfterheatOn.current}
        current={committedAfterheat} onConfirm={confirmAfterheat} onCancel={() => setAfterheatDraft(null)} busy={busy !== null}
        frostLock={frostLock.locked} floor={frostLock.floor}/>
      {afterheatControl.known && <AfterheatLevelPanel control={afterheatControl}/>}
    </article>
  );

  return (
    <section className={`dashboard-overview page-enter${skin === "aurora" ? " with-side" : ""}`}>
      <div className="ov-layout">
      {/* Dantherm Live-kortet er forsidens hovedelement; i Aurora står en oversigtskolonne til højre. */}
      <div className="ov-card ovx">
      <header className="overview-heading-row">
        <div>
          <span className="eyebrow">OVERBLIK</span>
          <h1>Aktuel drift og status</h1>
          <p>Live visning af HCH5, luftveje, sensorer og den styring der er aktiv lige nu.</p>
        </div>
        <div className="overview-status-pills ovx-status">
          <div><span className="status-led"/><small>Master</small><strong>{masterLabel(controller.active_master)}</strong></div>
          <div><span className={`status-led ${busHealthy ? "" : "warn"}`}/><small>Bus</small><strong>{busHealthy ? "Sund" : "Afventer"}</strong></div>
          <div><Leaf size={18}/><small>Driftstilstand</small><strong>{modeLabel(controller.mode)}</strong></div>
          {number(controller.unit_power_w) !== null && <div><Zap size={18}/><small>Forbrug</small><strong>{whole(number(controller.unit_power_w))} W</strong></div>}
          {number(controller.attic_temperature) !== null && <div><House size={18}/><small>Loftrum</small><strong>{temp(number(controller.attic_temperature))}</strong></div>}
        </div>
      </header>

      {Array.isArray(controller.diagnostics_alarms) && controller.diagnostics_alarms.length > 0 && <div className="diagnostics-alarms" role="alert">
        {(controller.diagnostics_alarms as { code: string; severity: string; text: string }[]).map(alarm => <div key={alarm.code} className={`diagnostics-alarm ${alarm.severity}`}><strong>{alarm.severity === "critical" ? "Fejl" : alarm.severity === "warning" ? "Advarsel" : "Bemærk"}</strong><span>{alarm.text}</span></div>)}
      </div>}

      {/* Ét fast gitter (overview-grid.css): tegning, styring, funktioner og nøgletal i ens kolonner og ens felthøjder. */}
      <div className="ovx-board">
        <article className="surface pro-air-card ovx-air">
          <div className="pro-card-head">
            <div><h2>Luftstrømme og temperaturer</h2><p>Live luftveje gennem HCH5 med aktuelle temperaturer og fysisk status.</p></div>
            <span className={`status-chip${online ? "" : " muted"}`}><span className="live-dot"/>{online ? "Live" : "Afventer"}</span>
          </div>
          <Hch5UnitDiagram
            onTemperatureClick={setActiveSensor}
            outdoor={outdoor} extract={extract} exhaust={exhaust} afterHeater={afterHeater} beforeHeater={correctedT2 ?? number(controller.actual_supply_before_heater_temperature)} t2Corrected={(number(controller.t2_offset_k) ?? 0) !== 0} beforeHeaterEstimate={number(controller.actual_supply_before_heater_estimate)}
            frost={frost} flowWater={flowWater} returnWater={returnWater}
            supplyRpm={supplyRpm} extractRpm={extractRpm} supplyPercent={supplyPercent} extractPercent={extractPercent}
            bypassActual={bypassActual} bypassRequest={bypassRequest} heating={heating} recovery={recovery} supplyRecovery={t2Recovery}
            busActive={busHealthy} bypassRaw={bypassRaw} afterheatLockout={afterheatLockout} afterheatFrostLock={frostLock.locked} afterheatCoil={controller.afterheat_coil === "water" ? "water" : "electric"}
            bypassTravelDirection={bypassTravelDirection} bypassTravelSeconds={bypassTravelSeconds} bypassTravelTotal={bypassTravelTotal}
            control={online ? describeControl(controller) : null}
          />
        </article>

        <article className={`surface pro-control-card ovx-drift${unit.unit_generation === "mk2" ? " ovx-solo" : ""}`}>
          <div className="pro-card-head compact"><div><h2>Drift og styring</h2><p>Daglige funktioner</p></div><Gauge size={22}/></div>
          <label className="control-label">Ventilationstilstand</label>
          <div className="pro-segment three">
            {["local_auto", "smart_auto", "manual"].map(value => (
              <button key={value} className={mode === value ? "active" : ""} disabled={busy !== null} onClick={() => void command(`mode-${value}`, { mode: value }, `${modeLabel(value)} valgt.`)}>{modeLabel(value)}</button>
            ))}
          </div>
          {haWarning && (canConfigure
            ? <NavLink to="/home-assistant" className={`ha-warning tone-${haInfo.tone}`} role="status"><strong>Home Assistant: {haInfo.label}</strong><span>{haInfo.detail}</span></NavLink>
            : <div className={`ha-warning tone-${haInfo.tone}`} role="status"><strong>Home Assistant: {haInfo.label}</strong><span>{haInfo.detail}</span></div>)}
          <label className="control-label">{dantherm ? "Ventilatortrin · Dantherm 4 trin" : "Ventilatorniveau"}</label>
          <div className="pro-levels with-off" style={{ "--levels": levels.length } as CSSProperties}>
            {levels.map(value => <button key={value} title={dantherm ? `Trin ${value} · ${LEVEL_NAMES[value].da}${value === 4 ? ` (${MAX_LEVEL_HOURS} timer, derefter trin 3)` : ""}` : undefined} className={!standbyActive && level === value ? "active" : ""} disabled={busy !== null} onClick={() => void command(`level-${value}`, standbyActive ? { standby_minutes: 0, [levelPatch]: value } : { [levelPatch]: value }, standbyActive ? `Anlægget er tændt på trin ${value}.` : `Ventilation sat til trin ${value}.`)}>{value}</button>)}
            <button className={`level-off${standbyActive ? " active" : ""}`} aria-haspopup="dialog" aria-pressed={standbyActive} disabled={busy !== null} onClick={() => setStandbyDialog(true)}>OFF</button>
          </div>
          <div className="active-decision"><span>Aktiv beslutning</span><strong>{standbyActive ? "OFF · anlæg slukket" : `Trin ${whole(level)} · ${text(controller.effective_source).replaceAll("_", " ")}`}</strong><small>{text(controller.effective_reason, "Afventer controllerens beslutning")}</small></div>
        </article>
        {unit.unit_generation === "mk2" ? null : afterheatCard}

        {/* Midlertidige funktioner: samme felt, samme knaphøjde og knapperne på én række nederst i hvert felt. */}
        <div className="ovx-functions">
          <FunctionTile active={quickBoostActive} icon={<Wind size={22}/>} label="Hurtig boost"
            state={quickBoostActive ? `Aktiv · ${remaining(controller.quick_boost_remaining_seconds)}` : "Ikke aktiv"}
            hint={fireplace ? "Ikke under pejsefunktion" : `Trin ${number(controller.quick_boost_level) ?? levels.length} i kort tid`}>
            {[15, 30, 60].map(minutes => <button key={minutes} className={quickBoostActive && number(controller.quick_boost_minutes) === minutes ? "active" : ""} aria-pressed={quickBoostActive && number(controller.quick_boost_minutes) === minutes} disabled={busy !== null || fireplace} onClick={() => void command(`boost-${minutes}`, { quick_boost_minutes: minutes }, `Quick Boost ${minutes} min startet.`)}>{minutes} min</button>)}
            {quickBoostActive && <button className="stop" onClick={() => void command("boost-stop", { quick_boost_minutes: 0 }, "Quick Boost stoppet.")}>Stop</button>}
          </FunctionTile>
          <FunctionTile active={String(controller.bypass) === "on"} icon={<ArrowRight size={22}/>} label="Bypass-styring"
            state={`Faktisk: ${bypassActualLabel}`} hint={String(controller.bypass) === "on" ? "Ønske: On · bypass åben" : "Ønske: Auto · anlægget styrer selv"}>
            <button className={String(controller.bypass ?? "off") === "off" ? "active" : ""} aria-pressed={String(controller.bypass ?? "off") === "off"} disabled={busy !== null || bypassMoving} onClick={() => void command("bypass-auto", { bypass: "off" }, "Bypass sat til Auto.")}>Auto</button>
            <button className={String(controller.bypass) === "on" ? "active" : ""} aria-pressed={String(controller.bypass) === "on"} disabled={busy !== null || fireplace || bypassMoving} onClick={() => void command("bypass-on", { bypass: "on" }, "Bypass ønskes åben.")}>On</button>
          </FunctionTile>
          <FunctionTile active={controller.cooling_enabled === true || coolActive} icon={<Snowflake size={22}/>} label="Frikøling"
            state={coolActive ? "Køl aktiv" : coolingLabel(controller.cooling_state)}
            hint={coolActive ? `${remaining(controller.cool_boost_remaining_seconds)} · bypass åben og boost` : frostLock.locked ? FROST_COOL_HINT : controller.cooling_enabled === true ? "Automatik aktiv" : "Automatik slået fra"}>
            <button className={coolActive ? "active" : ""} aria-haspopup="dialog" disabled={busy !== null || fireplace || standbyActive || frostLock.locked} title={frostLock.locked ? FROST_COOL_HINT : fireplace ? "Ikke under pejsefunktion" : "Åbn bypass og kør boost i en periode"} onClick={() => setCoolDialog(true)}>Køl</button>
            {coolActive && <button className="stop" disabled={busy !== null} onClick={() => void command("cool-stop", { cool_boost_minutes: 0 }, "Køl stoppet.")}>Stop</button>}
            <button className={controller.cooling_enabled === true ? "active" : ""} aria-pressed={controller.cooling_enabled === true} disabled={busy !== null} onClick={() => controller.cooling_enabled !== true && void command("cooling", { cooling_enabled: true }, "Frikøling aktiveret.")}>Til</button>
            <button className={controller.cooling_enabled !== true ? "active" : ""} aria-pressed={controller.cooling_enabled !== true} disabled={busy !== null} onClick={() => controller.cooling_enabled === true && void command("cooling", { cooling_enabled: false }, "Frikøling deaktiveret.")}>Fra</button>
          </FunctionTile>
          <FunctionTile active={bonfireActive} icon={<CloudFog size={22}/>} tone="smoke" label="Bål i haven"
            state={bonfireActive ? "Aktiv · anlæg slukket" : "Ikke aktiv"}
            hint={bonfireActive ? `${remaining(controller.bonfire_remaining_seconds)} · tænder selv igen` : fireplace ? "Ikke under pejsefunktion" : standbyActive ? "Anlægget er slukket" : "Slukker anlægget, tænder selv igen"}>
            {bonfireActive
              ? <button className="stop" onClick={() => void command("bonfire-stop", { bonfire_minutes: 0 }, "Bål-tilstand stoppet.")}>Stop</button>
              : BONFIRE_CHOICES.map(([minutes, label]) => <button key={minutes} disabled={busy !== null || fireplace || standbyActive} onClick={() => void command(`bonfire-${minutes}`, { bonfire_minutes: minutes }, `Bål-tilstand startet i ${label}.`)}>{label}</button>)}
          </FunctionTile>
          <FunctionTile active={fireplace} icon={<Flame size={22}/>} tone="flame" label="Pejsefunktion"
            state={fireplace ? "Aktiv" : "Ikke aktiv"} hint={fireplace ? remaining(controller.fireplace_remaining_seconds) : "Overtryk mens der fyres · 15 eller 30 min"}>
            {fireplace
              ? <button className="stop" onClick={() => void command("fireplace-stop", { fireplace_minutes: 0 }, "Pejsefunktion stoppet.")}>Stop</button>
              : <><button disabled={busy !== null} onClick={() => void command("fireplace-15", { fireplace_minutes: 15 }, "Pejsefunktion startet i 15 min.")}>15 min</button><button disabled={busy !== null} onClick={() => void command("fireplace-30", { fireplace_minutes: 30 }, "Pejsefunktion startet i 30 min.")}>30 min</button></>}
          </FunctionTile>
        </div>

        {/* Nøgletal: to og to afsnit side om side med samme antal felter, så rækkerne ender lige. */}
        <div className="ovx-figures">
          <div className="ovx-pair">
            <section className="ovx-sec">
              <div className="pro-card-head compact"><div><h2>Indeklimadata</h2><p>Aktuelle værdier</p></div></div>
              <div className="climate-metrics">
                <div className="climate-metric green"><Leaf size={21}/><span>CO₂</span><strong>{whole(co2)} <small>ppm</small></strong><em>{co2 === null ? "Ukendt" : co2 < 800 ? "God" : co2 < 1200 ? "Moderat" : "Høj"}</em><i style={{ width: `${co2 === null ? 0 : Math.min(100, Math.max(5, co2 / 16))}%` }}/></div>
                <div className="climate-metric blue"><span className="metric-drop">●</span><span>Luftfugtighed</span><strong>{whole(humidity)} <small>%</small></strong><em>{humidity === null ? "Ukendt" : humidity < 60 ? "Normal" : "Høj"}</em><i style={{ width: `${humidity ?? 0}%` }}/></div>
                <div className="climate-metric cyan"><span className="metric-filter">▧</span><span>Filter</span><strong>{whole(filterLife)} <small>%</small></strong><em title={filterHours === null ? undefined : `Skiftet for ${Math.floor(filterHours / 24)} dage siden (anlæggets tæller, nulstilles med knappen på anlægget)`}>{filterLife === null ? "Ukendt" : controller.filter_status === "overskredet" ? "Skift filter" : controller.filter_status === "skift_snart" ? `Snart skift · ${filterDays ?? "—"} dage` : filterDays !== null ? `OK · ${filterDays} dage tilbage` : filterLife > 40 ? "OK" : filterLife > 15 ? "Snart skift" : "Skift filter"}</em><i style={{ width: `${Math.max(0, Math.min(100, filterLife ?? 0))}%` }}/></div>
                <div className="climate-metric neutral"><span className="metric-heat">≋</span><span>Eftervarme setpunkt</span><strong>{shownAfterheat === "off" ? "OFF" : temp(shownAfterheat)}</strong><em>{afterheatStatus}</em><i style={{ width: `${shownAfterheat === "off" ? 0 : ((shownAfterheat - 10) / 25) * 100}%` }}/></div>
              </div>
            </section>
            {/* Extract duct loss: the HA average of the rooms with an extract valve against T3 and the loft. */}
            {number(controller.extract_rooms_temperature) !== null && <section className="ovx-sec">
              <div className="pro-card-head compact"><div><h2>Udsugningskanaler · afkøling på loftet</h2><p>Rum med udsugning → T3 ved anlægget{number(controller.attic_temperature) !== null ? ` · loft ${temp(number(controller.attic_temperature))}` : ""}</p></div></div>
              <div className="climate-metrics">
                <div className="climate-metric neutral"><span className="metric-heat">≋</span><span>Udsugningsrum</span><strong>{temp(number(controller.extract_rooms_temperature))}</strong><em>Gennemsnit fra Home Assistant</em></div>
                <div className="climate-metric cyan"><Wind size={21}/><span>T3 ved anlægget</span><strong>{temp(number(controller.extract_duct_loss_k) === null ? null : number(controller.extract_rooms_temperature)! - number(controller.extract_duct_loss_k)!)}</strong><em>Udsugning ind i veksleren</em></div>
                <div className="climate-metric neutral"><Gauge size={21}/><span>Afkøling</span><strong>{number(controller.extract_duct_loss_k) === null ? "—" : `${number(controller.extract_duct_loss_k)!.toLocaleString("da-DK", { maximumFractionDigits: 1 })} K`}</strong><em>Rum − T3</em></div>
                <div className={`climate-metric ${(number(controller.extract_duct_loss_percent) ?? 0) > 15 ? "neutral" : "green"}`}><Snowflake size={21}/><span>Andel tabt</span><strong>{number(controller.extract_duct_loss_percent) === null ? "—" : `${whole(number(controller.extract_duct_loss_percent))} %`}</strong><em title="Afkølingen delt med forskellen mellem rummene og loftet. En isoleret, tæt kanal giver få procent.">af forskellen rum − loft</em></div>
              </div>
            </section>}
          </div>
          <div className="ovx-pair">
            <section className="ovx-sec">
              <div className="pro-card-head compact"><div><h2>Diagnose og energi i dag</h2><p>{controller.diagnostics_status === "ok" || !controller.diagnostics_status ? "Ingen advarsler" : `${controller.diagnostics_alarm_count} advarsel${controller.diagnostics_alarm_count === 1 ? "" : "er"}`}</p></div></div>
              <div className="climate-metrics metrics-5">
                <div className="climate-metric cyan"><Snowflake size={21}/><span>Frost i veksler</span><strong>{({ ok: "OK", watch: "Hold øje", risk: "Risiko", unknown: "—" } as Record<string, string>)[String(controller.frost_state ?? "unknown")] ?? "—"}</strong><em>Afkast T4 {temp(exhaust)}</em><i style={{ width: controller.frost_state === "risk" ? "100%" : controller.frost_state === "watch" ? "50%" : "5%" }}/></div>
                <div className="climate-metric neutral"><Gauge size={21}/><span>Filter · strøm</span><strong>{filterChange === null ? "—" : `${filterChange > 0 ? "+" : ""}${filterChange.toLocaleString("da-DK", { maximumFractionDigits: 1 })} %`}</strong><em title="Effekten ved samme ventilatorgear sammenlignet med de første 72 timer efter filterskift. Ventilatorerne holder omdrejningerne, så tilstopning ses som ændret strøm.">{number(controller.unit_power_w) === null ? "Kræver effektmåler" : filterLearning ? "Lærer rene filtre (72 t efter skift)" : controller.filter_reference_gears ? `ved gear ${String(controller.filter_reference_gears)} mod rene filtre` : "Ingen reference for disse gear endnu"}</em><i style={{ width: `${Math.min(100, Math.abs(filterChange ?? 0) / 15 * 100)}%` }}/></div>
                <div className="climate-metric green"><Leaf size={21}/><span>Genvundet i dag</span><strong>{energy(number(controller.recovered_energy_today_kwh))} <small>kWh</small></strong><em>Teoretisk varmeværdi ca. {cost(number(controller.recovered_energy_today_kwh), number(controller.heat_price_dkk_kwh))}</em><i style={{ width: `${Math.min(100, (number(controller.recovered_energy_today_kwh) ?? 0) * 5)}%` }}/></div>
                <div className="climate-metric neutral"><Zap size={21}/><span>Strøm i dag{number(controller.unit_energy_measured_today_kwh) !== null ? " · målt" : " · anslået"}</span><strong>{energy(number(controller.unit_energy_measured_today_kwh) ?? number(controller.unit_energy_today_kwh))} <small>kWh</small></strong><em>Ca. {cost(number(controller.unit_energy_measured_today_kwh) ?? number(controller.unit_energy_today_kwh), number(controller.electricity_price_dkk_kwh))} ved aktuel elpris</em><i style={{ width: `${Math.min(100, (number(controller.unit_energy_measured_today_kwh) ?? number(controller.unit_energy_today_kwh) ?? 0) * 50)}%` }}/></div>
                <div className="climate-metric neutral"><Flame size={21}/><span>Eftervarme i dag · anslået</span><strong>{energy(number(controller.afterheat_energy_today_kwh))} <small>kWh</small></strong><em>Ca. {cost(number(controller.afterheat_energy_today_kwh), number(controller.heat_price_dkk_kwh))} ved aktuel varmepris</em><i style={{ width: `${Math.min(100, (number(controller.afterheat_energy_today_kwh) ?? 0) * 50)}%` }}/></div>
              </div>
            </section>
            {/* Only with a measured T2 before the afterheat coil (1-Wire role "t2"). */}
            {number(controller.actual_supply_before_heater_temperature) !== null && <section className="ovx-sec">
              <div className="pro-card-head compact"><div><h2>Genvinding og eftervarme · målt T2</h2><p>Luftmængde anslået for aktuelt trin{number(controller.supply_airflow_estimate_m3h) === null ? "" : ` · ${whole(number(controller.supply_airflow_estimate_m3h))} m³/h`}</p></div></div>
              <div className="climate-metrics metrics-5">
                <div className="climate-metric green"><Leaf size={21}/><span>Veksler · korrigeret T2</span><strong>{t2Recovery === null ? "—" : `≈${whole(t2Recovery)}`} <small>%</small></strong><em>{t2NeedsCheck ? "T2/T2AH afviger trods slukket eftervarme" : `T2 ${temp(number(controller.t2_offset_k))} · (T2 − T1) / (T3 − T1)`}</em><i style={{ width: `${Math.max(0, Math.min(100, t2Recovery ?? 0))}%` }}/></div>
                <div className="climate-metric neutral"><Leaf size={21}/><span>Genvinding · udsugningsside</span><strong>{whole(recovery)} <small>%</small></strong><em>(T3 − T4) / (T3 − T1)</em><i style={{ width: `${Math.max(0, Math.min(100, recovery ?? 0))}%` }}/></div>
                <div className="climate-metric cyan"><Wind size={21}/><span>Genvundet varme</span><strong>{whole(number(controller.recovered_heat_w))} <small>W</small></strong><em>Veksler → indblæsning</em><i style={{ width: `${Math.min(100, (number(controller.recovered_heat_w) ?? 0) / 30)}%` }}/></div>
                <div className="climate-metric neutral"><span className="metric-heat">≋</span><span>T2AH − T2 · korrigeret T2</span><strong>{temp(t2Difference)}</strong><em>{heating ? "Eftervarme aktiv" : `Kun T2 korrigeret ${temp(number(controller.t2_offset_k))}`}</em><i style={{ width: `${Math.max(0, Math.min(100, (t2Difference ?? 0) * 10))}%` }}/></div>
                <div className="climate-metric neutral"><Flame size={21}/><span>Varmeflade til luft</span><strong>{whole(number(controller.afterheat_power_w))} <small>W</small></strong><em>Ud fra T2AH − T2 · anslået</em><i style={{ width: `${Math.min(100, (number(controller.afterheat_power_w) ?? 0) / 20)}%` }}/></div>
              </div>
            </section>}
          </div>
        </div>
      </div>
      </div>
      {skin === "aurora" && <OverviewSide controller={controller} busHealthy={busHealthy} haLabel={haInfo.label} haDetail={haInfo.detail} haTone={haInfo.tone}
        haVisible={haLink.state !== null && (haLink.state !== "never" || haLink.required)} canConfigure={canConfigure}/>}
      </div>
      {standbyDialog && createPortal(<div className="sensor-history-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setStandbyDialog(false); }}>
        <section className="standby-dialog surface" role="dialog" aria-modal="true" aria-labelledby="standby-dialog-title">
          <div className="sensor-history-heading"><div><span className="eyebrow">VENTILATION</span><h2 id="standby-dialog-title">{standbyActive ? "Anlægget er slukket" : "Sluk anlægget"}</h2></div><button type="button" aria-label="Luk" onClick={() => setStandbyDialog(false)}><X size={20}/></button></div>
          <p className="standby-dialog-lead">{standbyActive
            ? (controller.standby_remaining_seconds == null ? "Begge ventilatorer står stille, til du tænder igen." : `Begge ventilatorer står stille · ${remaining(controller.standby_remaining_seconds)}.`)
            : "Begge ventilatorer stopper. Pejs, bål og boost kan ikke startes, mens anlægget er slukket."}</p>
          <div className="standby-choices">
            {STANDBY_CHOICES.map(([minutes, label, hint]) => <button key={minutes} type="button" className={standbyActive && number(controller.standby_minutes) === minutes ? "active" : ""} disabled={busy !== null} onClick={() => { setStandbyDialog(false); void command(`standby-${minutes}`, { standby_minutes: minutes }, minutes === -1 ? "Anlægget er slukket, til du tænder igen." : minutes === -2 ? "Anlægget er slukket til i morgen kl. 07:00." : `Anlægget er slukket i ${label}.`); }}><strong>{label}</strong><small>{hint}</small></button>)}
          </div>
          {standbyActive && <button type="button" className="standby-on-action" disabled={busy !== null} onClick={() => { setStandbyDialog(false); void command("standby-stop", { standby_minutes: 0 }, "Anlægget er tændt igen."); }}>Tænd anlægget igen</button>}
        </section>
      </div>, document.body)}
      {coolDialog && createPortal(<div className="sensor-history-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setCoolDialog(false); }}>
        <section className="standby-dialog surface" role="dialog" aria-modal="true" aria-labelledby="cool-dialog-title">
          <div className="sensor-history-heading"><div><span className="eyebrow">FRIKØLING</span><h2 id="cool-dialog-title">{coolActive ? "Køl kører" : "Køl huset"}</h2></div><button type="button" aria-label="Luk" onClick={() => setCoolDialog(false)}><X size={20}/></button></div>
          <p className="standby-dialog-lead">{coolActive
            ? `Bypass er åben og anlægget kører boost · ${remaining(controller.cool_boost_remaining_seconds)}. Derefter går det tilbage til normal drift.`
            : frostLock.locked ? FROST_COOL_MESSAGE
            : `Bypass åbnes, anlægget kører boost, og eftervarmen holdes nede. Bagefter går det selv tilbage til normal drift.${outdoor === null ? "" : ` Udeluft lige nu ${temp(outdoor)}.`}`}</p>
          <div className="standby-choices">
            {COOL_CHOICES.map(([minutes, label]) => <button key={minutes} type="button" className={coolActive && number(controller.cool_boost_minutes) === minutes ? "active" : ""} disabled={busy !== null || frostLock.locked} title={frostLock.locked ? FROST_COOL_HINT : undefined} onClick={() => { setCoolDialog(false); void command(`cool-${minutes}`, { cool_boost_minutes: minutes }, `Køl startet i ${label}.`); }}><strong>{label}</strong><small>Bypass åben og boost</small></button>)}
          </div>
          {coolActive && <button type="button" className="standby-on-action" disabled={busy !== null} onClick={() => { setCoolDialog(false); void command("cool-stop", { cool_boost_minutes: 0 }, "Køl stoppet."); }}>Stop køl</button>}
        </section>
      </div>, document.body)}
      {activeSensor && SENSOR_HISTORY[activeSensor] && <SensorHistoryDialog sensor={SENSOR_HISTORY[activeSensor]} onClose={closeSensor}/>}
    </section>
  );
}
