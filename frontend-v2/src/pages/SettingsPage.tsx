import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { NAV_KEY, SKIN_KEY, SKIN_PRESETS, readNav, readSkin, type NavPosition, type Skin } from "../lib/appearance";
import { Activity, DoorOpen, Droplets, Fan, Flame, Gauge, House, Mail, Monitor, Moon, RotateCcw, Save, Scale, ShieldCheck, ShowerHead, Snowflake, Thermometer, Timer, Users, Wind } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { postJson, requestJson } from "../lib/api";
import { airflowPlan, balancedProfiles, effectiveProfiles, sideConstants, type AirflowProfiles, type RatioSource } from "../lib/airflow";
import { LEVEL_NAMES, MAX_LEVEL_HOURS, NOMINAL_RANGE, OFFSET_RANGE, baseProfiles, danthermLadder, fanSettings, fanSettingsError, levelList, stepCount, type FanSettings } from "../lib/fanSteps";
import { LANG_KEY, currentLang, useLang, type Lang } from "../lib/i18n";
import { useSession, type Permission } from "../lib/session";
import { FROST_LOCK_SETTINGS, formatFloor, readFrostLock } from "../lib/frostLock";
import { AFTERHEAT_ALGORITHMS, AFTERHEAT_CONTROL_SETTINGS, controlLabel, readAfterheatControl } from "../lib/afterheatControl";
import { NIGHT_COOL_SETTINGS, ROOM_TARGET_SETTINGS } from "../lib/automation";
import { NightCoolCard, RoomTargetCard } from "../components/AutomationCards";
import { AccountPanel } from "../components/AccountPanel";
import { MailPanel } from "../components/MailPanel";
import { UsersPanel } from "../components/UsersPanel";
import "../styles/panels.css";import "../styles/management.css";
import { changedKeys, type DraftCheck, keepDraftNow, usePullRefresh } from "../lib/pullRefresh";

type Data = Record<string, unknown>;
type Auth = { csrf?: string | null; enabled?: boolean; username?: string | null };
type Text = { da: string; en: string };
type LevelPlan = { estimate_supply_m3h?: number; estimate_extract_m3h?: number; supply_m3h?: number; extract_m3h?: number; air_changes_per_hour?: number | null; measured?: boolean; meets_requirement?: boolean; meets_reduced?: boolean };
type Plan = { fan_curve?: { rpm_at_0?: number; rpm_per_percent?: number; learned?: boolean }; duct_ratio?: number; duct_ratio_source?: RatioSource; volume_m3?: number; supply_required_m3h?: number; extract_required_m3h?: number; area_requirement_ls?: number; wet_room_requirement_ls?: number; required_air_changes_per_hour?: number | null; base_level?: number; min_level?: number; reachable?: boolean; estimated?: boolean; levels?: Record<string, LevelPlan> };
type Airflow = Record<string, { supply?: number; extract?: number; supply_percent?: number; extract_percent?: number }>;
type Profiles = Record<string, { supply?: number; extract?: number; name?: string }>;
type BalanceLive = { state?: string; reason?: string; remaining_seconds?: number; progress_percent?: number; delta_t?: number | null; delta_t_now?: number | null; excess_now_percent?: number | null };
type BalanceLearned = { ratio?: number | null; ratio_in_use?: number | null; spread?: number | null; count?: number; nights?: number; confidence?: string; updated_at?: number | null; last?: { t?: number; accepted?: boolean; reason?: string } | null };
type BalanceStatus = { enabled?: boolean; duct_ratio?: number; duct_ratio_source?: RatioSource; learned?: BalanceLearned; error?: string | null; levels?: Record<string, { current_supply?: number; current_excess_percent?: number }> };

function n(v: unknown, f: number) { const x = Number(v); return Number.isFinite(x) ? x : f; }
function s(v: unknown, f = "") { return v === null || v === undefined ? f : String(v); }
function fmt(v: unknown, lang: Lang, digits = 1, unit = "") {
  const x = typeof v === "number" ? v : Number.NaN;
  return Number.isFinite(x) ? `${x.toLocaleString(lang === "en" ? "en-GB" : "da-DK", { maximumFractionDigits: digits })}${unit}` : "—";
}

/** Every controller field this page edits. UI-only preferences stay in localStorage. */
export const CONTROLLER_KEYS = [
  "local_normal_level", "local_min_level", "local_max_level", "downshift_delay_seconds", "boost_hold_seconds",
  "air_quality_max_level", "rh_setpoint", "rh_hysteresis", "co2_setpoint", "co2_hysteresis", "co2_offset", "ha_timeout_seconds",
  "auto_step_rh", "auto_step_co2",
  "bathroom_rh_setpoint", "bathroom_rh_hysteresis", "bathroom_max_level",
  "night_enabled", "night_start", "night_end", "night_level", "night_air_quality_max_level",
  "afterheat_setpoint", "afterheat_coil", ...ROOM_TARGET_SETTINGS, ...NIGHT_COOL_SETTINGS,
  ...FROST_LOCK_SETTINGS, ...AFTERHEAT_CONTROL_SETTINGS,
  "cooling_enabled", "cooling_room_setpoint", "cooling_hysteresis", "cooling_outdoor_min", "cooling_min_delta",
  "cooling_level", "cooling_start_delay_seconds", "cooling_min_on_seconds", "cooling_min_off_seconds",
  "sizing_enabled", "house_area_m2", "ceiling_height_m", "house_bathrooms", "house_utility_rooms",
  "airflow_max_m3h", "sizing_reduced_percent", "airflow_measured",
  "balance_enabled", "balance_extract_excess_percent", "balance_ratio_mode", "balance_duct_ratio", "fan_settings",
  "humidity_smart_enabled", "outdoor_humidity_source", "humidity_margin_gm3", "duct_extract_source", "duct_rooms_offset_k", "duct_t3_offset_k", "t2_offset_k",
  "dry_protection_enabled", "dry_rh_limit", "dry_max_level", "dry_hold_minutes",
  "winter_dry_enabled", "winter_outdoor_max", "winter_outdoor_hysteresis", "winter_rh_limit", "winter_rh_hysteresis",
  "winter_rh_basis", "winter_level", "winter_hold_minutes",
  "fireplace_auto_enabled", "fireplace_auto_source", "fireplace_auto_on_temp", "fireplace_auto_off_temp",
  "fireplace_afterrun_minutes", "fireplace_max_hours", "fireplace_cold_guard_enabled", "onewire_roles",
  "pm25_enabled", "pm25_setpoint", "pm25_step", "pm25_hysteresis", "pm25_max_level", "pm25_ignored_rooms",
  "bathroom_rise_percent", "bathroom_rise_minutes", "bathroom_night_drying", "bathroom_max_minutes",
  "upshift_delay_seconds", "humidity_rise_min_rh",
] as const;

/** The settings changed in the form compared with the state it was loaded from. */
export function settingsPatch(form: Data, loaded: Data): Data {
  return changedKeys(form, loaded, CONTROLLER_KEYS) as Data;
}

const T = {
  eyebrow: { da: "INDSTILLINGER", en: "SETTINGS" },
  title: { da: "Anlæg og konto", en: "Unit and account" },
  intro: { da: "Vælg et område til venstre. Hvert felt forklarer, hvad det styrer, og hvad der sker, hvis du ændrer det. Rum og følere fra Home Assistant sættes op på siden Rum.", en: "Pick an area on the left. Every field explains what it controls and what happens when you change it. Rooms and sensors from Home Assistant are set up on the Rooms page." },
  saveHint: { da: "Ændringer gemmes først, når du trykker Gem controller.", en: "Changes are stored only when you press Save controller." },
  save: { da: "Gem controller", en: "Save controller" }, saving: { da: "Gemmer…", en: "Saving…" },
  saved: { da: "Indstillinger gemt.", en: "Settings saved." }, error: { da: "Fejl", en: "Error" },
  uiSaved: { da: "Brugerflade gemt.", en: "Interface saved." }, saveUi: { da: "Gem brugerflade", en: "Save interface" },
  on: { da: "Til", en: "On" }, off: { da: "Fra", en: "Off" }, none: { da: "Ingen valgt", en: "None selected" },
  level: { da: "Trin", en: "Level" }, status: { da: "Status lige nu", en: "Status right now" },
} satisfies Record<string, Text>;

type SectionId = "ui" | "house" | "air" | "humidity" | "night" | "afterheat" | "cooling" | "fireplace" | "security" | "sensors" | "users" | "mail";
type Section = { id: SectionId; icon: LucideIcon; title: Text; lead: Text; group: Text };
const GROUP = {
  ventilation: { da: "Ventilation", en: "Ventilation" },
  air: { da: "Luftkvalitet", en: "Air quality" },
  heat: { da: "Varme og køl", en: "Heating and cooling" },
  sensors: { da: "Følere", en: "Sensors" },
  account: { da: "Konto og system", en: "Account and system" },
} satisfies Record<string, Text>;
/** The side menu, grouped. The ids are kept, so #/settings?section=… links keep working. */
const SECTIONS: Section[] = [
  { id: "house", group: GROUP.ventilation, icon: House, title: { da: "Trin og luftmængde", en: "Steps and airflow" }, lead: { da: "Trinstyring, grundtrin, grænser og luftbalance", en: "Step control, base level, limits and air balance" } },
  { id: "night", group: GROUP.ventilation, icon: Moon, title: { da: "Nat", en: "Night" }, lead: { da: "Lavere trin mens I sover", en: "Lower level while you sleep" } },
  { id: "air", group: GROUP.air, icon: Wind, title: { da: "Fugt og CO₂", en: "Humidity and CO₂" }, lead: { da: "Grænser der løfter ventilationen, og PM2.5", en: "Limits that raise the ventilation, and PM2.5" } },
  { id: "humidity", group: GROUP.air, icon: Droplets, title: { da: "Badeværelse og tør luft", en: "Bathroom and dry air" }, lead: { da: "Udtørring efter bad, udeluftens fugt og vinter", en: "Drying after a shower, outdoor humidity and winter" } },
  { id: "afterheat", group: GROUP.heat, icon: Thermometer, title: { da: "Eftervarme og frost", en: "Afterheat and frost" }, lead: { da: "Indblæsningens temperatur og frostlås", en: "Supply air temperature and frost lock" } },
  { id: "cooling", group: GROUP.heat, icon: Snowflake, title: { da: "Frikøling og bypass", en: "Free cooling and bypass" }, lead: { da: "Køl huset med kølig udeluft via bypass", en: "Cool the house with cool outdoor air via bypass" } },
  { id: "fireplace", group: GROUP.heat, icon: Flame, title: { da: "Pejs og brændeovn", en: "Fireplace and stove" }, lead: { da: "Overtryk mens der fyres", en: "Positive pressure while the stove burns" } },
  { id: "sensors", group: GROUP.sensors, icon: Activity, title: { da: "Følere og målerum", en: "Sensors and measuring rooms" }, lead: { da: "1-Wire-følere, korrektioner og udsugningsrum", en: "1-Wire sensors, corrections and extract rooms" } },
  { id: "ui", group: GROUP.account, icon: Monitor, title: { da: "Brugerflade", en: "Interface" }, lead: { da: "Sprog, tema og bevægelse", en: "Language, theme and motion" } },
  { id: "security", group: GROUP.account, icon: ShieldCheck, title: { da: "Sikkerhed", en: "Security" }, lead: { da: "Min konto, login og hvem der styrer anlægget", en: "My account, login and who controls the unit" } },
  { id: "users", group: GROUP.account, icon: Users, title: { da: "Brugere", en: "Users" }, lead: { da: "Brugere, teknikere og administratorer", en: "Users, technicians and administrators" } },
  { id: "mail", group: GROUP.account, icon: Mail, title: { da: "Mail", en: "Mail" }, lead: { da: "Fejlmeddelelser og nulstilling af adgangskode", en: "Fault notifications and password reset" } },
];
const sectionInfo = (id: SectionId) => SECTIONS.find(item => item.id === id) as Section;

/** Who may open each section. Controller sections need "configure"; the rest follow their own permission. */
const SECTION_PERMISSION: Partial<Record<SectionId, Permission>> = { users: "users", mail: "mail" };
const OPEN_SECTIONS: SectionId[] = ["ui", "security"];
/** Sections with their own save button instead of the controller save bar. */
const SELF_SAVING: SectionId[] = ["ui", "security", "users", "mail"];
export function sectionAllowed(id: SectionId, can: (permission: Permission) => boolean) {
  if (OPEN_SECTIONS.includes(id)) return true;
  return can(SECTION_PERMISSION[id] ?? "configure");
}

function Card({ title, lead, icon: Icon, children }: { title: string; lead?: string; icon?: LucideIcon; children: ReactNode }) {
  return <article className="surface panel-card settings-card"><div className="pro-card-head compact"><div><h2>{title}</h2>{lead && <p>{lead}</p>}</div>{Icon && <Icon size={20}/>}</div>{children}</article>;
}

/** Deep link: #/settings?section=afterheat (HashRouter keeps the query in the hash). */
function initialSection(): SectionId {
  const wanted = new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("section");
  return SECTIONS.some(item => item.id === wanted) ? wanted as SectionId : "house";
}

function Help({ children }: { children: ReactNode }) { return <small className="settings-field-help">{children}</small>; }


/**
 * Frost lock for a water afterheat coil in a cold attic. The Pi decides; this
 * card only edits the limits and shows the state. Hidden by the caller on
 * firmware that does not report the frost lock, so its keys are never sent there.
 */
export function FrostLockCard({ lang, form, controller, set }: { lang: Lang; form: Data; controller: Data; set: (key: string, value: unknown) => void }) {
  const da = lang === "da";
  const lock = readFrostLock(controller);
  const num = (key: string) => (e: { target: { value: string } }) => set(key, Number(e.target.value));
  const water = (form.afterheat_coil ?? controller.afterheat_coil) === "water";
  const state = !lock.known ? "—"
    : !lock.enabled ? (da ? "Slået fra" : "Off")
    : !lock.locked ? (da ? "Ikke aktiv" : "Not active")
    : `${da ? "Aktiv" : "Active"} · min. ${formatFloor(lock.floor)} °C`;
  return <Card title={da ? "Frostlås" : "Frost lock"} lead={da ? "Vandfladen på loftet må ikke fryse" : "The water coil in the attic must not freeze"} icon={Snowflake}>
    <div className="settings-grid">
      <label className="check-row"><input type="checkbox" checked={form.frost_lock_enabled !== false} onChange={e => set("frost_lock_enabled", e.target.checked)}/> {da ? "Frostsikring af eftervarmen" : "Frost protection of the afterheat"}</label>
      <Help>{da
        ? "Når det fryser, holder Pi'en eftervarmen tændt på mindst minimumssetpunktet. Den kan ikke slukkes eller sættes lavere, og Køl kan ikke startes, heller ikke fra Home Assistant, standby eller bål. Dine egne valg gemmes og bruges igen, når frosten er væk. Gælder kun en vandbåren varmeflade."
        : "While it freezes the Pi keeps the afterheat on at the minimum setpoint or above. It cannot be switched off or set lower, and Køl cannot be started, not even from Home Assistant, standby or bonfire. Your own choices are kept and used again once the frost is gone. Only applies to a water coil."}</Help>
      <label>{da ? "Frost ude eller på loftet" : "Frost outdoors or in the attic"}<input type="number" min="-10" max="10" step="0.5" value={n(form.frost_lock_outdoor_c, 3)} onChange={num("frost_lock_outdoor_c")}/><span>°C</span><Help>{da ? "Låsen slår til, når udeluften T1 eller loftføleren er på eller under denne temperatur." : "The lock engages when outdoor air T1 or the attic sensor is at or below this temperature."}</Help></label>
      <label>{da ? "Frost ved vandfladen" : "Frost at the water coil"}<input type="number" min="3" max="15" step="0.5" value={n(form.frost_lock_coil_c, 8)} onChange={num("frost_lock_coil_c")}/><span>°C</span><Help>{da ? "Låsen slår også til, når HAC1's frostføler på vandsiden er på eller under denne temperatur." : "The lock also engages when HAC1's frost sensor on the water side is at or below this temperature."}</Help></label>
      <label>{da ? "Mindste setpunkt i frost" : "Lowest setpoint in frost"}<input type="number" min="15" max="25" step="0.5" value={n(form.frost_lock_min_setpoint_c, 18)} onChange={num("frost_lock_min_setpoint_c")}/><span>°C</span><Help>{da ? "Eftervarmen sendes til HAC1 med mindst denne temperatur, så længe låsen er aktiv. Et lavere grundsetpunkt, rumstyring og Køl bliver løftet hertil." : "The afterheat is sent to HAC1 with at least this temperature while the lock is active. A lower base setpoint, room control and Køl are raised to it."}</Help></label>
      <p className="settings-help">{da
        ? "Låsen slipper først, når alle kendte målinger har ligget mindst 2 °C over grænserne i 30 minutter. Mangler alle målinger, beholdes den hidtidige tilstand."
        : "The lock only releases once every known reading has been at least 2 °C above its limit for 30 minutes. With no readings at all, the previous state is kept."}</p>
    </div>
    <div className="settings-summary">
      <span>{T.status[lang]}<strong>{state}</strong></span>
      <span>{da ? "Årsag" : "Reason"}<strong>{lock.locked && lock.reason ? lock.reason : "—"}</strong></span>
      <span>{da ? "Håndhævet" : "Enforced"}<strong>{!lock.known ? "—" : lock.enforced ? (da ? "Ja · Pi styrer bussen" : "Yes · the Pi drives the bus") : (da ? "Nej · Pi er ikke busmaster" : "No · the Pi is not bus master")}</strong></span>
    </div>
    {!water && <p className="settings-help">{da ? "Varmefladen er sat til elvarmeflade, så frostlåsen er ikke i brug." : "The coil is set to electric, so the frost lock is not in use."}</p>}
    {lock.locked && !lock.enforced && <p className="settings-help">{da ? "HCP4 styrer bussen lige nu og kan stadig slukke eftervarmen." : "HCP4 drives the bus right now and could still switch the afterheat off."}</p>}
  </Card>;
}

/**
 * Who regulates HAC1's afterheat level: the Pi from the chosen temperature, or
 * HAC1 on its own. Hidden by the caller on firmware without the setting; the
 * tuning fields only show when the Pi reports them.
 */
export function AfterheatControlCard({ lang, form, controller, set }: { lang: Lang; form: Data; controller: Data; set: (key: string, value: unknown) => void }) {
  const da = lang === "da";
  const pi = form.afterheat_control_mode === "pi";
  const control = readAfterheatControl(controller);
  const num = (key: string) => (e: { target: { value: string } }) => set(key, Number(e.target.value));
  // A whole number of ticks: an empty field is not sent (it is not 0) and a fraction is rounded.
  const whole = (key: string, low: number, high: number) => (e: { target: { value: string } }) => {
    const text = e.target.value.trim();
    const value = Number(text);
    if (text === "" || !Number.isFinite(value)) return;
    set(key, Math.min(high, Math.max(low, Math.round(value))));
  };
  const hasGain = typeof controller.afterheat_pi_gain === "number";
  const hasIntegral = typeof controller.afterheat_pi_integral_minutes === "number";
  const hasBand = typeof controller.afterheat_pi_band === "number";
  const hasDwell = typeof controller.afterheat_pi_min_dwell_ticks === "number";
  const hasAlgorithm = typeof controller.afterheat_algorithm === "string";
  const chosenAlgorithm = AFTERHEAT_ALGORITHMS.find(item => item.key === form.afterheat_algorithm) ?? AFTERHEAT_ALGORITHMS[0];
  return <Card title={da ? "Regulering" : "Control"} lead={da ? "Hvem styrer varmefladens trin" : "Who drives the coil's levels"} icon={Gauge}>
    <div className="settings-grid">
      <div className="settings-segment" role="radiogroup" aria-label={da ? "Regulering af eftervarmen" : "Afterheat control"}>
        <button type="button" role="radio" aria-checked={pi} className={pi ? "is-active" : undefined} onClick={() => set("afterheat_control_mode", "pi")}>{da ? "Pi-styring" : "Pi control"}</button>
        <button type="button" role="radio" aria-checked={!pi} className={!pi ? "is-active" : undefined} onClick={() => set("afterheat_control_mode", "native")}>{da ? "HAC1 styrer selv" : "HAC1 on its own"}</button>
      </div>
      <Help>{da
        ? "HAC1 åbner vandventilen i trin fra 0 til 10 og flytter højst ét trin i minuttet. Selv regulerer den kun på hele grader og svinger, når ventilatorerne skifter trin. Med Pi-styring vælger Pi'en trinnet ud fra den ønskede temperatur, også med decimaler, og tager højde for ventilatorernes luftmængde. Er en måling forældet, er Pi'en ikke busmaster, eller kører Køl, frikøling, standby eller bål, overlader Pi'en straks styringen til HAC1. Frostsikringen gælder stadig."
        : "HAC1 opens the water valve in levels from 0 to 10 and moves at most one level a minute. On its own it only regulates on whole degrees and swings when the fans change speed. With Pi control the Pi picks the level from the wanted temperature, decimals included, and allows for the fans' airflow. With a stale reading, when the Pi is not bus master, or while Køl, free cooling, standby or bonfire runs, the Pi hands control straight back to HAC1. The frost protection still applies."}</Help>
      {pi && hasAlgorithm && <label>{da ? "Styring" : "Control method"}
        <select value={chosenAlgorithm.key} onChange={e => set("afterheat_algorithm", e.target.value)}>
          {AFTERHEAT_ALGORITHMS.map(item => <option key={item.key} value={item.key}>{item.label}</option>)}
        </select>
        <Help>{chosenAlgorithm.help}</Help></label>}
      {pi && hasGain && <label>{da ? "Forstærkning" : "Gain"}<input type="number" min="0.3" max="2" step="0.1" value={n(form.afterheat_pi_gain, 1)} onChange={num("afterheat_pi_gain")}/><span>×</span><Help>{da ? "Ganger Pi'ens beregnede forstærkning (1,0 = som designet, 0,3–2,0). Højere reagerer hurtigere, men kan svinge." : "Scales the Pi's computed gain (1.0 = as designed, 0.3–2.0). Higher reacts faster but may swing."}</Help></label>}
      {pi && hasIntegral && <label>{da ? "Integraltid" : "Integral time"}<input type="number" min="1" max="20" step="0.5" value={n(form.afterheat_pi_integral_minutes, 2)} onChange={num("afterheat_pi_integral_minutes")}/><span>min</span><Help>{da ? "Hvor hurtigt en vedvarende afvigelse rettes. Kortere retter hurtigere, men kan svinge." : "How fast a lasting deviation is corrected. Shorter corrects faster but may swing."}</Help></label>}
      {pi && hasBand && <label>{da ? "Komfortbånd" : "Comfort band"}<input type="number" min="0.1" max="1.5" step="0.1" value={n(form.afterheat_pi_band, 0.5)} onChange={num("afterheat_pi_band")}/><span>± K</span><Help>{da ? "Så tæt på den ønskede temperatur (0,1–1,5 K) lader Pi'en trinnet være i fred. Et trin giver ofte 1,5–2 K, så et bredere bånd giver roligere drift med færre skift; et smallere rammer målet tættere, men skifter oftere. På laveste trin må luften være 1,3 × båndet for kold, men mens frostsikringen er slået til, aldrig under frostgrænsen. Ligger intet trin i båndet, og er luften for kold i flere minutter, skiftes der mellem de to nærmeste trin." : "How close to the wanted temperature (0.1–1.5 K) the Pi leaves the level alone. One level often gives 1.5–2 K, so a wider band means calmer running with fewer changes; a narrower one is closer to the target but changes more often. On the lowest level the air may be 1.3 × the band too cold, but while the frost protection is on never below the frost limit. When no level lies in the band and the air stays too cold for some minutes, the Pi alternates between the two nearest levels."}</Help></label>}
      {pi && hasDwell && <label>{da ? "Mindste tid pr. trin" : "Minimum time per level"}<input type="number" min="1" max="10" step="1" value={n(form.afterheat_pi_min_dwell_ticks, 3)} onChange={whole("afterheat_pi_min_dwell_ticks", 1, 10)}/><span>min</span><Help>{da ? "Et nyt trin holdes mindst så mange af HAC1's minut-tick (1–10, hele tal), før Pi'en flytter igen. Ventetiden springes over, når luften er langt fra målet: mere end 2 × båndet for varm (også når den stiger derop), mere end 2 × båndet for kold (på laveste trin 2 × 1,3 × båndet) eller under frostgrænsen, mens frostsikringen er slået til. Et trin, der vil efterlade luften længere fra målet, tages dog kun som et trin ned, når luften er over 2 K for varm, og som et trin op, når den er under frostgrænsen eller har været for kold i flere minutter; så skiftes der mellem de to nabotrin, hvert holdt mindst denne tid, så luften i snit rammer målet." : "A new level is kept for at least this many of HAC1's one-minute ticks (1–10, whole numbers) before the Pi moves again. The wait is skipped when the air is far off: more than 2 × the band too warm (also when it is rising there), more than 2 × the band too cold (2 × 1.3 × the band on the lowest level) or below the frost limit while the frost protection is on. A step that would leave the air further from the target is only taken as a step down when the air is over 2 K too warm, and as a step up when it is below the frost limit or has been too cold for some minutes; then the Pi alternates between the two neighbouring levels, each kept at least this long, so the air is right on average."}</Help></label>}
    </div>
    <div className="settings-summary">
      <span>{T.status[lang]}<strong>{control.known ? controlLabel(control) : "—"}</strong></span>
      {typeof controller.afterheat_algorithm_label === "string" && <span>{da ? "Styring" : "Control method"}<strong>{controller.afterheat_algorithm_label}</strong></span>}
      <span>{da ? "Begrundelse" : "Reason"}<strong>{control.reason ?? "—"}</strong></span>
    </div>
  </Card>;
}

type BathroomStatus = { name: string; humidity: number | null; rh_setpoint: number; rh_end: number; max_level: number; state: string; drying_minutes: number | null; webui_override: boolean };

/** Bathroom control: common limits, shower detection and the bathrooms' state (rooms are set up on the page Rum). */
function BathroomCards({ lang, form, controller, set, maxLevel }: { lang: Lang; form: Data; controller: Data; set: (key: string, value: unknown) => void; maxLevel: number }) {
  const da = lang === "da";
  const status = new Map((Array.isArray(controller.bathroom_status) ? controller.bathroom_status as BathroomStatus[] : []).map(item => [item.name, item]));
  const common = { rh: n(form.bathroom_rh_setpoint, 65), hysteresis: n(form.bathroom_rh_hysteresis, 5), level: n(form.bathroom_max_level, maxLevel) };
  const stateText: Record<string, [string, string]> = {
    drying: [da ? "Tørrer ud" : "Drying", "drying"], normal: [da ? "Normal" : "Normal", "normal"], expired: [da ? "Tid udløbet" : "Time up", "expired"],
    view_only: [da ? "Kun visning" : "View only", "normal"], no_humidity: [da ? "Ingen fugtmåling" : "No humidity", "normal"],
  };
  return <>
    <Card title={da ? "Badeværelse" : "Bathroom"} lead={da ? "Fugtgrænse, største trin og hvornår et bad opdages" : "Humidity limit, highest level and how a shower is detected"} icon={ShowerHead}>
      <div className="settings-grid">
        <label>{da ? "Fugt start" : "Humidity start"}<input type="number" min="35" max="90" value={common.rh} onChange={e => set("bathroom_rh_setpoint", Number(e.target.value))}/><span>%</span><Help>{da ? "Over denne fugt begynder udtørringen. Badeværelser er naturligt fugtigere, så de får en højere grænse end resten af huset." : "Drying starts above this humidity. Bathrooms are naturally more humid, so they get a higher limit than the rest of the house."}</Help></label>
        <label>{da ? "Hysterese" : "Hysteresis"}<input type="number" min="1" max="20" value={common.hysteresis} onChange={e => set("bathroom_rh_hysteresis", Number(e.target.value))}/><span>%</span><Help>{da ? "Udtørringen stopper, når fugten er under grænsen minus dette tal." : "Drying ends when humidity is below the limit minus this number."}</Help></label>
        <label>{da ? "Højeste trin ved udtørring" : "Highest level while drying"}<input type="number" min="1" max={maxLevel} value={common.level} onChange={e => set("bathroom_max_level", Number(e.target.value))}/><Help>{da ? "Ventilationen starter på dette trin og trapper ned mod normal, efterhånden som fugten falder. Sæt det lavere, hvis anlægget larmer for meget." : "Ventilation starts at this level and steps down towards normal as humidity falls. Set it lower if the unit is too loud."}</Help></label>
        <label>{da ? "Maks. udtørringstid" : "Longest drying time"}<input type="number" min="0" max="720" step="10" value={n(form.bathroom_max_minutes, 0)} onChange={e => set("bathroom_max_minutes", Number(e.target.value))}/><span>min</span><Help>{da ? "Sikkerhed hvis en føler hænger højt: efter så lang tid stopper udtørringen. Den starter igen ved et nyt bad (fugten stiger igen) eller efter en halv time, hvis fugten stadig er over grænsen og bevæger sig. 0 = ingen grænse." : "Safety if a sensor sticks high: after this long drying stops until humidity has been below the end point. 0 = no limit."}</Help></label>
      </div>
      <h3 className="settings-subhead">{da ? "Bad opdages før grænsen nås" : "Shower detected before the limit is reached"}</h3>
      <div className="settings-grid">
        <label>{da ? "Fugten stiger med" : "Humidity rises by"}<input type="number" min="3" max="20" step="0.5" value={n(form.bathroom_rise_percent, 7)} onChange={e => set("bathroom_rise_percent", Number(e.target.value))}/><span>%-point</span></label>
        <label>{da ? "på højst" : "within"}<input type="number" min="3" max="30" value={n(form.bathroom_rise_minutes, 10)} onChange={e => set("bathroom_rise_minutes", Number(e.target.value))}/><span>min</span></label>
        <Help>{da ? "Stiger fugten så hurtigt, regnes det som et bad, og udtørringen starter med det samme, også selvom fugten stadig er under grænsen. Lavere tal giver hurtigere reaktion, men kan også reagere på madlavning." : "If humidity rises this fast it counts as a shower and drying starts at once, even if humidity is still below the limit. Lower numbers react faster but may also react to cooking."}</Help>
        <label>{da ? "Fugtstigning tæller fra" : "A humidity rise counts from"}<input type="number" min="0" max="80" value={n(form.humidity_rise_min_rh, 0)} onChange={e => set("humidity_rise_min_rh", Number(e.target.value))}/><span>%</span><Help>{da ? "Gælder alle rum. Stiger fugten (bad, madlavning), mens luften i rummet stadig er under denne fugt, skrues der ikke op: i tør luft er fugten noget huset mangler. Fugt over badeværelsets grænse tørres altid ud. 0 = altid (som før)." : "Applies to all rooms. If humidity rises (shower, cooking) while the room is still below this humidity, the level does not go up: in dry air the moisture is something the house lacks. Humidity above the bathroom limit is always dried out. 0 = always (as before)."}</Help></label>
        <label className="check-row"><input type="checkbox" checked={form.bathroom_night_drying !== false} onChange={e => set("bathroom_night_drying", e.target.checked)}/> {da ? "Tør også ud om natten" : "Also dry at night"}</label>
        <Help>{da ? "Slået til må udtørringen køre med fuld kraft om natten, uanset natsænkningen, og trapper ned af sig selv. Slået fra holder natsænkningen ventilationen nede, også efter et bad." : "On, drying may run at full strength at night regardless of night reduction and steps down by itself. Off, night reduction keeps ventilation down, even after a shower."}</Help>
      </div>
    </Card>
    <Card title={da ? "Badeværelser" : "Bathrooms"} lead={da ? "Hvilke rum der er badeværelser, og egne grænser pr. rum" : "Which rooms are bathrooms, and limits per room"} icon={ShowerHead}>
      {status.size > 0 && <div className="settings-summary">{[...status.values()].map(live => { const shown = stateText[live.state]; return <span key={live.name}>{live.name}<strong className={`bathroom-state ${shown?.[1] ?? "normal"}`}>{fmt(live.humidity, lang, 0, " %")} · {shown?.[0]}{live.state === "drying" && live.drying_minutes !== null ? ` ${live.drying_minutes} min` : ""}</strong></span>; })}</div>}
      <p className="settings-help">{da ? "Rumtype og egne grænser pr. badeværelse (fugt start, hysterese, højeste trin) sættes på siden " : "Room type and own limits per bathroom (humidity start, hysteresis, highest level) are set on the page "}<a href="#/rooms">{da ? "Rum" : "Rooms"}</a>{da ? ". Felterne ovenfor er de fælles værdier, som rum uden egne grænser bruger." : ". The fields above are the common values used by rooms without their own limits."}</p>
    </Card>
  </>;
}

export function SettingsPage() {
  const lang = useLang();
  const { can } = useSession();
  const t = (text: Text) => text[lang];
  const [controller, setController] = useState<Data>({});
  const [auth, setAuth] = useState<Auth>({});
  const [csrf, setCsrf] = useState("");
  const [form, setForm] = useState<Data>({});
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [section, setSection] = useState<SectionId>(initialSection);
  const [theme, setTheme] = useState(() => localStorage.getItem("hch5-v2-theme") ?? "dark");
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("hch5-v2-sidebar") === "1");
  const [motion, setMotion] = useState(() => localStorage.getItem("hch5-v2-motion") ?? "normal");
  const [skin, setSkin] = useState<Skin>(readSkin);
  const [navPosition, setNavPosition] = useState<NavPosition>(readNav);
  const [language, setLanguage] = useState<Lang>(currentLang);
  // Six steps: gears typed in the step table, saved as level profiles.
  const [sixEdits, setSixEdits] = useState<Record<string, { extract?: number; supply?: number }>>({});

  const controllerRef = useRef<Data>(controller);
  controllerRef.current = controller;
  // keepDraft: the fields changed but not saved keep their value; everything else is refreshed.
  const refresh = useCallback(async (keepDraft: DraftCheck = false) => {
    const [c, a] = await Promise.all([requestJson<Data>("/api/controller/state?compact=1"), requestJson<Auth>("/api/auth/status")]);
    const base = controllerRef.current;
    const keep = keepDraftNow(keepDraft);  // asked after the answer: an edit made while waiting counts
    setForm(current => keep ? { ...c, ...changedKeys(current, base, CONTROLLER_KEYS) } : c);
    setController(c); setAuth(a); setCsrf(a.csrf ?? "");
  }, []);
  const dirty = Object.keys(changedKeys(form, controller, CONTROLLER_KEYS)).length > 0 || Object.keys(sixEdits).length > 0;
  usePullRefresh(({ keepDraft }) => refresh(keepDraft), { dirty });
  useEffect(() => { void refresh(); }, [refresh]);

  const set = (key: string, value: unknown) => setForm(v => ({ ...v, [key]: value }));
  const num = (key: string) => (e: { target: { value: string } }) => set(key, Number(e.target.value));
  const check = (key: string) => (e: { target: { checked: boolean } }) => set(key, e.target.checked);
  const save = async () => {
    if (!csrf) return;
    setBusy(true); setNotice(t(T.saving));
    // Only what was changed on this page: values Home Assistant or the Overview set
    // since the page was opened are not sent back with their old value.
    const patch: Data = settingsPatch(form, controller);
    // Six steps keep the four-step commissioning untouched.
    if (stepCount(controller) !== 4) delete patch.fan_settings;
    else if (patch.fan_settings !== undefined && fanError) { setNotice(`${t(T.error)}: ${fanError}`); setBusy(false); return; }
    if (Object.keys(sixEdits).length) patch.profiles = sixEdits;
    if (!Object.keys(patch).length) { setNotice(t(T.saved)); setBusy(false); return; }
    try { const next = await postJson<Data>("/api/controller/config", patch, csrf); setController(next); setForm(next); setSixEdits({}); setNotice(t(T.saved)); }
    catch (e) { setNotice(`${t(T.error)}: ${e instanceof Error ? e.message : "?"}`); }
    finally { setBusy(false); }
  };
  const uiSave = () => {
    localStorage.setItem("hch5-v2-theme", theme); localStorage.setItem("hch5-v2-sidebar", collapsed ? "1" : "0");
    localStorage.setItem("hch5-v2-motion", motion); localStorage.setItem(LANG_KEY, language);
    localStorage.setItem(SKIN_KEY, skin); localStorage.setItem(NAV_KEY, navPosition);
    document.documentElement.dataset.motion = motion; document.documentElement.lang = language;
    window.dispatchEvent(new Event("hch5-ui-preferences"));
    setNotice(language === "en" ? T.uiSaved.en : T.uiSaved.da);
  };

  const rooms = Array.isArray(controller.measurement_rooms) ? controller.measurement_rooms.map(String) : [];
  const roomOptions = (current: unknown) => {
    const names = new Set(rooms); const selected = s(current);
    const gone = selected.startsWith("room:") && !names.has(selected.slice(5)) ? selected.slice(5) : null;
    if (gone) names.add(gone);
    return [...names].sort().map(name => <option key={name} value={`room:${name}`}>{name === gone ? `${name} ${lang === "da" ? "(findes ikke på siden Rum)" : "(not on the Rooms page)"}` : name}</option>);
  };
  // Why a stove/extract room gives no value, in plain words.
  const sourceStatus = (status: unknown, kind: "stove" | "extract") => {
    const da = lang === "da";
    if (status === "missing") return <p className="settings-help settings-warning">{da
      ? `Rummet findes ikke længere på siden Rum, så ${kind === "stove" ? "den automatiske pejsefunktion kan ikke se brændeovnen" : "tabet i udsugningskanalen kan ikke måles"}. Opret det igen på siden Rum med typen ${kind === "stove" ? "Kilde (brændeovn)" : "Kun kanalmåling (udsugning)"}.`
      : `The room is no longer on the Rooms page, so ${kind === "stove" ? "automatic fireplace mode cannot see the stove" : "the extract duct loss cannot be measured"}. Create it again on the Rooms page with the type ${kind === "stove" ? "Source (stove)" : "Duct measurement only (extract)"}.`}</p>;
    if (status === "no_data") return <p className="settings-help">{da ? "Rummet findes, men Home Assistant sender ingen frisk temperatur fra det lige nu." : "The room exists, but Home Assistant sends no fresh temperature from it right now."}</p>;
    return null;
  };
  const roomHint = rooms.length ? null : <Help>{lang === "da" ? "Ingen målerum endnu. Opret rummet på siden Rum med typen Kilde, Ude eller Kun kanalmåling (udsugning)." : "No measuring rooms yet. Create the room on the Rooms page with the type Source, Outdoor or Duct measurement only (extract)."}</Help>;

  const runningProfiles = controller.profiles as Profiles | undefined;
  const steps = stepCount(controller);
  const levels = levelList(controller);
  const maxLevel = levels.length;
  const fans = fanSettings(form);
  const fanError = steps === 4 ? fanSettingsError(fans, lang) : null;
  const setFan = (key: keyof FanSettings) => (e: { target: { value: string } }) => set("fan_settings", { ...fans, [key]: Number(e.target.value) });
  // Steps before the balance, following the form while typing.
  const sixBase = baseProfiles({ ...form, fan_step_count: steps });
  const editedProfiles = (steps === 4
    ? (fanError ? baseProfiles(controller) : danthermLadder(fans))
    : sixBase && Object.fromEntries(Object.entries(sixBase).map(([level, values]) => [level, { ...values, ...(sixEdits[level] ?? {}) }]))) as AirflowProfiles | undefined;
  const stepRows = effectiveProfiles(form, editedProfiles);
  const switchSteps = async (count: 4 | 6) => {
    if (!csrf || count === steps) return;
    const question = lang === "da"
      ? `Skift til ${count} trin? Alle trinvalg (normaltrin, maks. ved luftkvalitet, nat, ferie, ugeplan, udtørring, frikøling, PM2.5 og boost) flyttes til det trin, der har nærmest samme ventilatorgear. Du kan skifte tilbage når som helst. Ikke-gemte ændringer på siden forsvinder.`
      : `Switch to ${count} steps? Every step choice (normal level, air quality maximum, night, vacation, week plan, drying, free cooling, PM2.5 and boost) moves to the step with the nearest fan gears. You can switch back at any time. Unsaved changes on the page are lost.`;
    if (!window.confirm(question)) return;
    setBusy(true);
    try {
      const next = await postJson<Data>("/api/controller/config", { fan_step_count: count }, csrf);
      setController(next); setForm(next); setSixEdits({});
      setNotice(lang === "da" ? `Anlægget kører nu med ${count} trin.` : `The unit now runs ${count} steps.`);
    } catch (e) { setNotice(`${t(T.error)}: ${e instanceof Error ? e.message : "?"}`); }
    finally { setBusy(false); }
  };
  const setSix = (level: number, side: "extract" | "supply", value: string) => setSixEdits(v => ({ ...v, [String(level)]: { ...(v[String(level)] ?? {}), [side]: Number(value) } }));
  const plan = (airflowPlan(form, editedProfiles) ?? controller.airflow_plan ?? {}) as Plan;
  const airflow = (form.airflow_measured ?? {}) as Airflow;
  // A measured value belongs to the fan percentage it was measured at; it
  // starts as the percentage the level runs now.
  const setAirflow = (level: number, side: "supply" | "extract", value: string) => {
    const current = airflow[level] ?? {};
    const percentKey = side === "supply" ? "supply_percent" : "extract_percent";
    const running = Number(runningProfiles?.[String(level)]?.[side]);
    const next: Airflow = { ...airflow, [level]: { ...current, [side]: value === "" ? undefined : Number(value),
      [percentKey]: value === "" ? undefined : current[percentKey] ?? (Number.isFinite(running) ? running : undefined) } };
    set("airflow_measured", next);
  };
  const setAirflowPercent = (level: number, side: "supply" | "extract", value: string) => {
    const percentKey = side === "supply" ? "supply_percent" : "extract_percent";
    set("airflow_measured", { ...airflow, [level]: { ...(airflow[level] ?? {}), [percentKey]: value === "" ? undefined : Number(value) } });
  };
  const sizing = form.sizing_enabled === true;

  const balanceOn = form.balance_enabled === true;
  const balanced = balancedProfiles(form, editedProfiles);
  const balanceStatus = (controller.balance ?? {}) as BalanceStatus;
  const live = (controller.balance_live ?? {}) as BalanceLive;
  const learned = balanceStatus.learned ?? {};
  const ducts = sideConstants(form, runningProfiles as AirflowProfiles | undefined);
  const signed = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? `${value > 0 ? "+" : ""}${fmt(value, lang, 1)} %` : "—";
  const sourceLabel: Record<RatioSource, Text> = {
    measured: { da: "målt ved ventilerne", en: "measured at the valves" },
    learned: { da: "lært af varmebalancen", en: "learned from the heat balance" },
    fixed: { da: "fast værdi", en: "fixed value" },
  };
  const confidenceLabel: Record<string, Text> = {
    ok: { da: "sikker", en: "trusted" }, low: { da: "foreløbig", en: "preliminary" }, none: { da: "ingen målinger endnu", en: "no readings yet" },
  };
  const liveText = (() => {
    if (live.state === "settling") return lang === "da" ? `Venter på stabil drift (${Math.ceil(n(live.remaining_seconds, 0) / 60)} min)` : `Waiting for steady running (${Math.ceil(n(live.remaining_seconds, 0) / 60)} min)`;
    if (live.state === "measuring") return lang === "da" ? `Måler (${n(live.progress_percent, 0)} %)` : `Measuring (${n(live.progress_percent, 0)} %)`;
    return s(live.reason, "—");
  })();
  const resetLearning = async () => {
    if (!csrf) return;
    setBusy(true);
    try {
      const next = await postJson<Data>("/api/controller/config", { balance_learning_reset: true }, csrf);
      setController(next); setForm(v => ({ ...v, balance_learned: next.balance_learned }));
      setNotice(lang === "da" ? "Læringen af kanalforholdet starter forfra." : "Learning of the duct ratio starts over.");
    } catch (e) { setNotice(`${t(T.error)}: ${e instanceof Error ? e.message : "?"}`); }
    finally { setBusy(false); }
  };

  type OneWireSensor = { id: string; temperature: number | null; role: string; name: string };
  const onewire = (Array.isArray(controller.onewire_sensors) ? controller.onewire_sensors : []) as OneWireSensor[];
  const onewireRoles = (form.onewire_roles ?? {}) as Record<string, { role: string; name: string }>;
  const setOnewire = (id: string, patch: Partial<{ role: string; name: string }>) => {
    const current = onewireRoles[id] ?? { role: onewire.find(sensor => sensor.id === id)?.role ?? "none", name: "" };
    const next = { ...onewireRoles, [id]: { ...current, ...patch } };
    // Only one sensor can be T2; picking a new one frees the old.
    // T2, flow and return belong to one sensor each; taking one frees it elsewhere.
    if (patch.role && ["t2", "water_flow", "water_return"].includes(patch.role)) {
      for (const sensor of onewire) if (sensor.id !== id && (next[sensor.id]?.role ?? sensor.role) === patch.role) next[sensor.id] = { name: next[sensor.id]?.name ?? "", role: "none" };
    }
    set("onewire_roles", next);
  };
  const roleLabel: Record<string, Text> = {
    none: { da: "Ikke i brug", en: "Not used" },
    t2: { da: "T2 · før eftervarme", en: "T2 · before afterheat" },
    attic: { da: "Loftrum", en: "Loft space" },
    water_flow: { da: "Eftervarme · frem", en: "Afterheat · flow" },
    water_return: { da: "Eftervarme · retur", en: "Afterheat · return" },
    other: { da: "Andet (eget navn)", en: "Other (own name)" },
  };
  const sections: Record<SectionId, ReactNode> = {
    sensors: <Card title={lang === "da" ? "1-Wire-følere" : "1-Wire sensors"} lead={lang === "da" ? "DS18B20-følere på Pi'ens 1-Wire-bus (GPIO4)" : "DS18B20 sensors on the Pi's 1-Wire bus (GPIO4)"} icon={Activity}>
      <p className="settings-help">{lang === "da" ? "Nye følere dukker op her af sig selv, når de er loddet på 1-Wire-bussen (3,3 V, GND og data parallelt med de eksisterende). Giv hver føler en rolle: T2 bruges i tegningen, genvindingen og som målt T2; Loftrum og egne navne vises her og i Home Assistant. Eftervarme frem/retur bestemmer, hvilke følere 1-Wire-tjenesten sender til Home Assistant som vandets frem- og returtemperatur. Er de forvekslet, så byt rollerne her." : "New sensors appear here by themselves once soldered onto the 1-Wire bus (3.3 V, GND and data in parallel with the existing ones). Give each a role: T2 is used in the drawing, the recovery and as measured T2; Loft space and own names show here and in Home Assistant. Afterheat flow/return decide which sensors the 1-Wire service reports to Home Assistant as the water flow and return. Swap the roles here if they are mixed up."}</p>
      {onewire.length === 0
        ? <p className="settings-warning">{lang === "da" ? "Ingen følere fundet endnu. Tjek lodningerne og at føleren er på samme bus som frem/retur." : "No extra sensors found yet. Check the soldering and that the sensor is on the same bus as flow/return."}</p>
        : <div className="settings-table-wrap"><table className="settings-table"><thead><tr><th>{lang === "da" ? "Føler-id" : "Sensor id"}</th><th>{lang === "da" ? "Temperatur" : "Temperature"}</th><th>{lang === "da" ? "Rolle" : "Role"}</th><th>{lang === "da" ? "Navn" : "Name"}</th></tr></thead>
          <tbody>{onewire.map(sensor => (() => { const entry = onewireRoles[sensor.id] ?? { role: sensor.role ?? "none", name: "" }; return <tr key={sensor.id}>
            <td><code>{sensor.id}</code></td>
            <td>{fmt(sensor.temperature, lang, 1, " °C")}</td>
            <td><select aria-label={`${sensor.id} ${lang === "da" ? "rolle" : "role"}`} value={entry.role} onChange={e => setOnewire(sensor.id, { role: e.target.value })}>{Object.entries(roleLabel).map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}</select></td>
            <td><input className="settings-table-name" aria-label={`${sensor.id} ${lang === "da" ? "navn" : "name"}`} maxLength={32} placeholder={entry.role === "none" ? "" : t(roleLabel[entry.role] ?? roleLabel.other)} value={entry.name} onChange={e => setOnewire(sensor.id, { name: e.target.value })}/></td>
          </tr>; })())}</tbody></table></div>}
      <p className="settings-help">{lang === "da" ? "T2-føleren skal sidde i indblæsningskanalen mellem enheden og varmefladen, mindst 20–30 cm fra fladen. Loftføleren skal hænge i skygge væk fra tagpladerne." : "The T2 sensor belongs in the supply duct between the unit and the afterheat coil, at least 20–30 cm from the coil. Hang the loft sensor in shade away from the roof."}</p>
          <div className="settings-grid">
        <label>{lang === "da" ? "Udsugningsrum (gennemsnit)" : "Extract rooms (average)"}<select value={s(form.duct_extract_source)} onChange={e => set("duct_extract_source", e.target.value)}><option value="">{t(T.none)}</option>{roomOptions(form.duct_extract_source)}</select>{sourceStatus(controller.duct_extract_source_status, "extract")}<Help>{lang === "da" ? "Et rum fra siden Rum (typen Kun kanalmåling (udsugning)) med gennemsnittet af rummene med udsugningsventil. Sammenholdt med T3 viser oversigten, hvor meget udsugningsluften køles af på vej til anlægget – i grader og som andel af forskellen mellem rummene og loftet. En fast andel uanset luftmængde peger på utætheder; en isoleret kanal giver kun få procent." : "A room from the Rooms page (type Extract room) with the average of the rooms with an extract valve. Against T3 the overview shows how much the extract air cools on its way to the unit, in degrees and as a share of the difference between the rooms and the loft. A steady share at every airflow points to leaks; an insulated duct gives only a few percent."}</Help></label>
        <label>{lang === "da" ? "Korrektion udsugningsrum" : "Extract rooms correction"}<input type="number" min="-3" max="3" step="0.05" value={n(form.duct_rooms_offset_k, 0)} onChange={num("duct_rooms_offset_k")}/><span>K</span><Help>{lang === "da" ? "Lægges til rumtemperaturen, kun i beregningen af kanalafkølingen. Måler rumfølerne fx 0,6 °C for højt, skrives -0,6." : "Added to the room temperature, only for the duct cooling. If the room sensors read 0.6 °C high, enter -0.6."}</Help></label>
        <label>{lang === "da" ? "Korrektion T3" : "T3 correction"}<input type="number" min="-3" max="3" step="0.05" value={n(form.duct_t3_offset_k, 0)} onChange={num("duct_t3_offset_k")}/><span>K</span><Help>{lang === "da" ? "Lægges til T3, kun i beregningen af kanalafkølingen. Måler T3 fx 0,7 °C for lavt, skrives 0,7." : "Added to T3, only for the duct cooling. If T3 reads 0.7 °C low, enter 0.7."}</Help></label>
        <label>{lang === "da" ? "Korrektion T2" : "T2 correction"}<input type="number" min="-3" max="3" step="0.05" value={n(form.t2_offset_k, 0)} onChange={num("t2_offset_k")}/><span>K</span><Help>{lang === "da" ? "Lægges kun til T2 i beregningen af vekslerens indblæsningsside og forskellen til T2AH. T2AH og rå følermålinger ændres ikke." : "Added to T2 only for exchanger and coil calculations. T2AH and raw sensor readings stay unchanged."}</Help></label>
      </div>
    </Card>,
    house: <>
      <Card title={lang === "da" ? "Trinstyring" : "Step control"} lead={steps === 4
        ? (lang === "da" ? "Dantherms 4 trin: indregulér trin 3, resten følger" : "Dantherm's 4 steps: commission step 3, the rest follows")
        : (lang === "da" ? "6 frie trin med egne værdier" : "6 free steps with their own values")} icon={Fan}>
        <div className="settings-grid">
          <div className="settings-mode-row"><span>{lang === "da" ? "Antal trin" : "Number of steps"}</span>
            <div className="settings-segment" role="radiogroup" aria-label={lang === "da" ? "Trinstyring" : "Step control"}>
              <button type="button" role="radio" aria-checked={steps === 4} className={steps === 4 ? "is-active" : undefined} disabled={busy || !csrf} onClick={() => void switchSteps(4)}>{lang === "da" ? "4 trin · Dantherm" : "4 steps · Dantherm"}</button>
              <button type="button" role="radio" aria-checked={steps === 6} className={steps === 6 ? "is-active" : undefined} disabled={busy || !csrf} onClick={() => void switchSteps(6)}>{lang === "da" ? "6 trin" : "6 steps"}</button>
            </div>
            <strong>{steps === 4
              ? (lang === "da" ? `Trin 1–2 ligger ${fans.offset} gear under trin 3 · trin 4 er maksimum` : `Steps 1–2 lie ${fans.offset} gears below step 3 · step 4 is maximum`)
              : (lang === "da" ? "Hvert trin har sine egne gear" : "Every step has its own gears")}</strong>
          </div>
          <Help>{lang === "da"
            ? `Skiftet sker med det samme og flytter alle trinvalg til det trin, der har nærmest samme ventilatorgear. Den anden models værdier gemmes, så du kan skifte tilbage. 4 trin følger Dantherms servicemanual for HCH 5 (HCP4-panel og HRC 2): trin 3 er den nominelle luftmængde, som huset indreguleres til. Trin 2 og 1 ligger én og to gearafstande under, og trin 4 er maksimum. Vælger du trin 4 manuelt, kører det i ${MAX_LEVEL_HOURS} timer og går så tilbage til trin 3, som på Dantherms panel.`
            : `Switching happens at once and moves every step choice to the step with the nearest fan gears. The other model's values are kept, so you can switch back. 4 steps follow Dantherm's service manual for the HCH 5 (HCP4 panel and HRC 2): step 3 is the nominal airflow the house is commissioned to. Steps 2 and 1 lie one and two offsets below, and step 4 is maximum. Chosen by hand, step 4 runs for ${MAX_LEVEL_HOURS} hours and then returns to step 3, as on Dantherm's panel.`}</Help>
          {steps === 4 && <>
            <label>{lang === "da" ? "Udsugning, trin 3" : "Extract, step 3"}<input type="number" min={NOMINAL_RANGE[0]} max={NOMINAL_RANGE[1]} value={fans.extract} onChange={setFan("extract")}/><span>gear</span><Help>{lang === "da" ? `Gear ${NOMINAL_RANGE[0]}–${NOMINAL_RANGE[1]}. Indstilles, så udsugningen på trin 3 giver husets krav (se Beregning).` : `Gear ${NOMINAL_RANGE[0]}–${NOMINAL_RANGE[1]}. Set so extract at step 3 gives the house's requirement (see Calculation).`}</Help></label>
            <label>{lang === "da" ? "Indblæsning, trin 3" : "Supply, step 3"}<input type="number" min={NOMINAL_RANGE[0]} max={NOMINAL_RANGE[1]} disabled={balanceOn} value={balanceOn ? n(stepRows?.["3"]?.supply, fans.supply) : fans.supply} onChange={setFan("supply")}/><span>gear</span><Help>{balanceOn ? (lang === "da" ? "Styres af luftbalancen (Auto herunder). Slå den over på Manuel for at indregulere indblæsningen selv." : "Set by the air balance (Auto below). Switch it to Manual to commission supply yourself.") : (lang === "da" ? "Må aldrig give mere luft end udsugningen (Dantherm)." : "Must never give more air than extract (Dantherm).")}</Help></label>
            <label>{lang === "da" ? "Gearafstand" : "Offset"}<input type="number" min={OFFSET_RANGE[0]} max={OFFSET_RANGE[1]} value={fans.offset} onChange={setFan("offset")}/><span>gear</span><Help>{lang === "da" ? `Afstanden ned til trin 2 og igen til trin 1 på begge ventilatorer. Fra fabrikken 25 gear; HRC 2 tillader ${OFFSET_RANGE[0]}–${OFFSET_RANGE[1]}.` : `The step down to step 2 and again to step 1 on both fans. Factory setting 25 gears; the HRC 2 allows ${OFFSET_RANGE[0]}–${OFFSET_RANGE[1]}.`}</Help></label>
            <label>{lang === "da" ? "Udsugning, trin 4" : "Extract, step 4"}<input type="number" min={fans.extract} max={100} value={fans.max_extract} onChange={setFan("max_extract")}/><span>gear</span><Help>{lang === "da" ? "Maksimum. Fra trin 3 op til gear 100 (fabrik: 100)." : "Maximum. From step 3 up to gear 100 (factory: 100)."}</Help></label>
            <label>{lang === "da" ? "Indblæsning, trin 4" : "Supply, step 4"}<input type="number" min={fans.supply} max={100} disabled={balanceOn} value={balanceOn ? n(stepRows?.["4"]?.supply, fans.max_supply) : fans.max_supply} onChange={setFan("max_supply")}/><span>gear</span><Help>{balanceOn ? (lang === "da" ? "Styres af luftbalancen." : "Set by the air balance.") : (lang === "da" ? "Sæt den lavere end udsugningen, hvis indblæsningen ellers giver mest luft." : "Set it below extract if supply would otherwise give the most air.")}</Help></label>
          </>}
        </div>
        {fanError && <p className="settings-warning">{fanError}</p>}
        {stepRows && <div className="settings-table-wrap"><table className="settings-table"><thead><tr><th>{t(T.level)}</th><th>{lang === "da" ? "Navn" : "Name"}</th><th>{lang === "da" ? "Udsugning" : "Extract"}</th><th>{lang === "da" ? "Indblæsning" : "Supply"}</th><th>{lang === "da" ? "Luft ind / ud" : "Air in / out"}</th><th>{lang === "da" ? "Balance" : "Balance"}</th>{steps === 4 && <th>{lang === "da" ? "Bruges til" : "Used for"}</th>}</tr></thead>
          <tbody>{levels.map(level => { const row = stepRows[String(level)] ?? {}; const air = plan.levels?.[String(level)] ?? {}; return <tr key={level} className={steps === 4 && level === 3 ? "is-base" : undefined}>
            <td>{level}</td>
            <td>{steps === 4 ? t(LEVEL_NAMES[level]) : s(row.name)}</td>
            <td>{steps === 6 ? <input aria-label={`${t(T.level)} ${level} ${lang === "da" ? "udsugning" : "extract"}`} type="number" min="1" max="100" value={n(row.extract, 0)} onChange={e => setSix(level, "extract", e.target.value)}/> : `${s(row.extract)} gear`}</td>
            <td>{steps === 6 && !balanceOn ? <input aria-label={`${t(T.level)} ${level} ${lang === "da" ? "indblæsning" : "supply"}`} type="number" min="1" max="100" value={n(row.supply, 0)} onChange={e => setSix(level, "supply", e.target.value)}/> : `${s(row.supply)} gear`}</td>
            <td>{fmt(air.supply_m3h, lang, 0)} / {fmt(air.extract_m3h, lang, 0, " m³/h")}</td>
            {(() => { const excess = air.supply_m3h && air.extract_m3h ? (air.extract_m3h / air.supply_m3h - 1) * 100 : null; return <td className={excess !== null && excess < 0 ? "settings-table-warn" : undefined} title={lang === "da" ? "Udsugning over indblæsning i m³/h. Under 0 = overtryk i huset." : "Extract above supply in m³/h. Below 0 = overpressure in the house."}>{excess === null ? "—" : `${lang === "da" ? "udsugning" : "extract"} ${signed(Math.round(excess * 10) / 10)}`}</td>; })()}
            {steps === 4 && <td>{({
              1: { da: "Fravær og ferie", en: "Away and vacation" },
              2: { da: "Nat og lav belastning", en: "Night and low load" },
              3: { da: "Husets behov – indreguleres her", en: "The house's need – commissioned here" },
              4: { da: `Boost, udtørring · ${MAX_LEVEL_HOURS} t ved manuelt valg`, en: `Boost, drying · ${MAX_LEVEL_HOURS} h when chosen by hand` },
            } as Record<number, Text>)[level][lang]}</td>}
          </tr>; })}</tbody></table></div>}
        {!balanceOn && <p className="settings-help">{lang === "da" ? "Luftbalance på Manuel: justér balancen som på Dantherms panel med indblæsningens og udsugningens gear hver for sig. Kolonnen Balance viser straks, hvor meget mere luft udsugningen giver (anbefalet 0–10 %). Tallene er skønnet ud fra omdrejninger og kanalforhold; målte luftmængder fra indreguleringen er altid bedre." : "Air balance on Manual: adjust the balance as on Dantherm's panel with the supply and extract gears separately. The Balance column shows at once how much more air extract gives (recommended 0–10 %). The figures are estimated from speed and duct ratio; airflow measured at commissioning is always better."}</p>}
        {steps === 6 && balanceOn && <p className="settings-help">{lang === "da" ? "Luftbalancen er på Auto, så indblæsningen regnes ud fra udsugningen på hvert trin." : "The air balance is on Auto, so supply is worked out from extract on every step."}</p>}
      </Card>
      <Card title={lang === "da" ? "Grænser og reaktionstid" : "Limits and response time"} lead={lang === "da" ? "Laveste og højeste trin i automatik, og hvor hurtigt der skiftes" : "Lowest and highest automatic level, and how fast it changes"} icon={Timer}>
        <div className="settings-grid">
          <label>{lang === "da" ? "Laveste trin" : "Lowest level"}<input type="number" min="1" max={maxLevel} value={n(form.local_min_level, 1)} onChange={num("local_min_level")}/><Help>{lang === "da" ? "Automatikken går aldrig under dette trin, heller ikke om natten eller på ferie." : "Automatic control never goes below this level, not even at night or on vacation."}</Help></label>
          <label>{lang === "da" ? "Højeste trin" : "Highest level"}<input type="number" min="1" max={maxLevel} value={n(form.local_max_level, maxLevel)} onChange={num("local_max_level")}/><Help>{lang === "da" ? "Automatikken går aldrig over dette trin. Manuel drift og boost er ikke begrænset." : "Automatic control never goes above this level. Manual mode and boost are not limited."}</Help></label>
          <label>{lang === "da" ? "Vent før der skrues ned" : "Wait before stepping down"}<input type="number" min="30" max="3600" step="30" value={n(form.downshift_delay_seconds, 300)} onChange={num("downshift_delay_seconds")}/><span>s</span><Help>{lang === "da" ? "Så længe skal luften have været god, før trinnet sænkes igen. Forhindrer at anlægget skifter op og ned." : "How long the air must have been good before the level is lowered again. Stops the unit from flapping."}</Help></label>
          <label>{lang === "da" ? "Vent før der skrues ét trin op" : "Wait before stepping one level up"}<input type="number" min="0" max="1800" step="30" value={n(form.upshift_delay_seconds, 0)} onChange={num("upshift_delay_seconds")}/><span>s</span><Help>{lang === "da" ? "Gælder Smart Auto. Så længe skal behovet for ét trin mere vare, før der skrues op, så en enkelt høj måling ikke starter blæseren. Skal der to trin op eller tørres et badeværelse ud, sker det med det samme. 0 = med det samme (som før)." : "Applies to Smart Auto. How long the need for one more level must last before stepping up, so a single high reading does not start the fan. Two levels up or drying a bathroom happen at once. 0 = at once (as before)."}</Help></label>
          <label>{lang === "da" ? "Hold et højt trin mindst" : "Hold a high level at least"}<input type="number" min="60" max="3600" step="60" value={n(form.boost_hold_seconds, 600)} onChange={num("boost_hold_seconds")}/><span>s</span><Help>{lang === "da" ? "Når luftkvaliteten har løftet til et højt trin, holdes det mindst så længe." : "Once air quality has raised to a high level, it is held at least this long."}</Help></label>
        </div>
      </Card>
      <Card title={lang === "da" ? "Hus og luftmængde" : "House and airflow"} lead={lang === "da" ? "Bygningsreglementet (BR18) kræver mindst 0,3 l/s pr. m² plus udsugning fra køkken og vådrum." : "The Danish building regulations (BR18) require at least 0.3 l/s per m² plus extract from kitchen and wet rooms."} icon={House}>
        <div className="settings-grid">
          <div className="settings-mode-row"><span>{lang === "da" ? "Grundtrin" : "Base level"}</span>
            <div className="settings-segment" role="radiogroup" aria-label={lang === "da" ? "Grundtrin" : "Base level"}>
              <button type="button" role="radio" aria-checked={sizing} className={sizing ? "is-active" : undefined} onClick={() => set("sizing_enabled", true)}>{lang === "da" ? "Auto" : "Auto"}</button>
              <button type="button" role="radio" aria-checked={!sizing} className={!sizing ? "is-active" : undefined} onClick={() => set("sizing_enabled", false)}>{lang === "da" ? "Manuel" : "Manual"}</button>
            </div>
            {sizing
              ? <strong>{lang === "da" ? `Trin ${s(plan.base_level, "—")} fra husets størrelse, laveste trin ${s(plan.min_level, "—")}` : `Level ${s(plan.base_level, "—")} from the house size, lowest level ${s(plan.min_level, "—")}`}</strong>
              : <label className="settings-inline">{lang === "da" ? "Normaltrin" : "Normal level"}<input type="number" min="1" max={maxLevel} value={n(form.local_normal_level, 3)} onChange={num("local_normal_level")}/></label>}
          </div>
          <Help>{lang === "da" ? "Auto: controlleren bruger det beregnede grundtrin i stedet for Normaltrin, og nat, ferie og tør luft kan aldrig gå under det reducerede minimum. Manuel: du vælger selv Normaltrin, og beregningen er kun vejledende. Manuel ventilatordrift påvirkes ikke." : "Auto: the controller uses the calculated base level instead of Normal level, and night, vacation and dry-air protection can never go below the reduced minimum. Manual: you pick Normal level yourself and the calculation is advisory only. Manual fan mode is not affected."}</Help>
          <label>{lang === "da" ? "Opvarmet boligareal" : "Heated floor area"}<input type="number" min="20" max="1000" step="1" value={n(form.house_area_m2, 150)} onChange={num("house_area_m2")}/><span>m²</span><Help>{lang === "da" ? "Fra BBR eller tegningen. Kravet er 0,3 l/s pr. m²." : "From the building register or drawings. The requirement is 0.3 l/s per m²."}</Help></label>
          <label>{lang === "da" ? "Loftshøjde" : "Ceiling height"}<input type="number" min="1.8" max="6" step="0.1" value={n(form.ceiling_height_m, 2.5)} onChange={num("ceiling_height_m")}/><span>m</span><Help>{lang === "da" ? "Bruges til husets luftvolumen og luftskifte pr. time." : "Used for the air volume and air changes per hour."}</Help></label>
          <label>{lang === "da" ? "Badeværelser" : "Bathrooms"}<input type="number" min="0" max="10" value={n(form.house_bathrooms, 1)} onChange={num("house_bathrooms")}/><Help>{lang === "da" ? "Hvert badeværelse kræver 15 l/s udsugning." : "Each bathroom needs 15 l/s extract."}</Help></label>
          <label>{lang === "da" ? "Bryggers og separate toiletter" : "Utility rooms and separate toilets"}<input type="number" min="0" max="10" value={n(form.house_utility_rooms, 1)} onChange={num("house_utility_rooms")}/><Help>{lang === "da" ? "10 l/s hver. Køkkenet (20 l/s) regnes altid med." : "10 l/s each. The kitchen (20 l/s) is always included."}</Help></label>
          <label>{lang === "da" ? "Anlæggets maks. luftmængde" : "Unit maximum airflow"}<input type="number" min="100" max="1500" value={n(form.airflow_max_m3h, 375)} onChange={num("airflow_max_m3h")}/><span>m³/h</span><Help>{lang === "da" ? "HCH5 er 375 m³/h ifølge databladet. Bruges til at skønne luftmængden på trin uden en målt værdi." : "HCH5 is 375 m³/h according to the datasheet. Used to estimate airflow for levels without a measured value."}</Help></label>
          <label>{lang === "da" ? "Reduceret minimum" : "Reduced minimum"}<input type="number" min="30" max="100" value={n(form.sizing_reduced_percent, 50)} onChange={num("sizing_reduced_percent")}/><span>%</span><Help>{lang === "da" ? "Laveste luftmængde i procent af kravet, som nat, ferie og tør luft må gå ned til." : "Lowest airflow, as a share of the requirement, that night, vacation and dry-air protection may use."}</Help></label>
        </div>
      </Card>
      <Card title={lang === "da" ? "Luftbalance" : "Air balance"} lead={lang === "da" ? "Svagt undertryk i m³/h på alle trin" : "Slight underpressure in m³/h on every level"} icon={Scale}>
        <div className="settings-grid">
          <div className="settings-mode-row"><span>{lang === "da" ? "Indblæsning" : "Supply"}</span>
            <div className="settings-segment" role="radiogroup" aria-label={lang === "da" ? "Luftbalance" : "Air balance"}>
              <button type="button" role="radio" aria-checked={balanceOn} className={balanceOn ? "is-active" : undefined} onClick={() => set("balance_enabled", true)}>Auto</button>
              <button type="button" role="radio" aria-checked={!balanceOn} className={!balanceOn ? "is-active" : undefined} onClick={() => set("balance_enabled", false)}>{lang === "da" ? "Manuel" : "Manual"}</button>
            </div>
            <strong>{balanceOn
              ? (lang === "da" ? `Udsugning ${fmt(n(form.balance_extract_excess_percent, 5), lang, 1)} % over indblæsning på alle trin` : `Extract ${fmt(n(form.balance_extract_excess_percent, 5), lang, 1)} % above supply on every level`)
              : (lang === "da" ? "Indblæsningens egne gear" : "The supply fan's own gears")}</strong>
          </div>
          <Help>{lang === "da" ? "Auto: hvert trin beholder sin udsugning, og controlleren regner indblæsningen ud, så udsugningen er den valgte andel større end indblæsningen i m³/h – ikke i procent. Ventilatorernes omdrejninger starter ved ca. 550 omdr./min ved 0 %, så en fast forskel i procent giver meget forskellig balance på laveste og højeste trin. Manuel: indblæsningens egne gear bruges, som de står under Trinstyring." : "Auto: every level keeps its extract, and the controller works out the supply so extract is the chosen share above supply in m³/h – not in percent. The fans turn at about 550 rpm at 0 %, so a fixed gap in percent gives a very different balance at the lowest and highest level. Manual: the supply fan's own gears under Step control are used as they are."}</Help>
          <label>{lang === "da" ? "Udsugning over indblæsning" : "Extract above supply"}<input type="number" min="0" max="20" step="0.5" value={n(form.balance_extract_excess_percent, 5)} onChange={num("balance_extract_excess_percent")}/><span>%</span><Help>{lang === "da" ? "Anbefalet 0–10 % i boliger: et svagt undertryk, så fugtig indeluft ikke presses ud i vægge og tag. Overtryk bruges kun kortvarigt af pejsefunktionen." : "Recommended 0–10 % in homes: a slight underpressure so moist indoor air is not pushed into walls and roof. Only the fireplace function uses overpressure, briefly."}</Help></label>
          <label>{lang === "da" ? "Kanalforhold" : "Duct ratio"}<select value={s(form.balance_ratio_mode, "auto")} onChange={e => set("balance_ratio_mode", e.target.value)}><option value="auto">{lang === "da" ? "Lær af varmebalancen" : "Learn from the heat balance"}</option><option value="fixed">{lang === "da" ? "Fast værdi" : "Fixed value"}</option></select><Help>{lang === "da" ? "Kanalerne giver ikke lige meget luft pr. omdrejning. Controlleren måler forholdet ud fra varmebalancen i veksleren (kræver T2-føleren før eftervarmen) og bruger det, når to nætter giver samme resultat. Indtil da bruges den faste værdi. Målte luftmængder fra indreguleringen går altid forud." : "The ducts do not give the same airflow per rpm. The controller measures the ratio from the heat balance of the exchanger (needs the T2 sensor before the afterheat) and uses it once two nights agree. Until then the fixed value is used. Airflow measured at commissioning always wins."}</Help></label>
          <label>{lang === "da" ? "Fast kanalforhold" : "Fixed duct ratio"}<input type="number" min="0.7" max="1.5" step="0.01" value={n(form.balance_duct_ratio, 1)} onChange={num("balance_duct_ratio")}/><Help>{lang === "da" ? "Indblæsningens luftmængde pr. omdrejning delt med udsugningens. 1,00 = ens kanaler; 1,10 = indblæsningen giver 10 % mere luft ved samme omdrejninger." : "Supply airflow per rpm divided by extract airflow per rpm. 1.00 = alike ducts; 1.10 = supply moves 10 % more air at the same speed."}</Help></label>
        </div>
        <div className="settings-summary">
          <span>{lang === "da" ? "Kanalforhold i brug" : "Duct ratio in use"}<strong>{ducts.ratio.toLocaleString(lang === "en" ? "en-GB" : "da-DK", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} · {t(sourceLabel[ducts.source])}</strong></span>
          <span>{lang === "da" ? "Varmebalancen" : "Heat balance"}<strong>{liveText}</strong>{typeof live.excess_now_percent === "number" && <small>{lang === "da" ? `Lige nu (vejledende): udsugning ${signed(live.excess_now_percent)}` : `Right now (indicative): extract ${signed(live.excess_now_percent)}`}</small>}</span>
          <span>{lang === "da" ? "Lært af varmebalancen" : "Learned from the heat balance"}<strong>{typeof learned.ratio === "number"
            ? `${fmt(learned.ratio, lang, 2)} · ${learned.count ?? 0} ${learned.count === 1 ? (lang === "da" ? "måling" : "reading") : (lang === "da" ? "målinger" : "readings")} · ${learned.nights ?? 0} ${learned.nights === 1 ? (lang === "da" ? "nat" : "night") : (lang === "da" ? "nætter" : "nights")} (${t(confidenceLabel[s(learned.confidence, "none")] ?? confidenceLabel.none)})`
            : t(confidenceLabel.none)}</strong>{learned.last?.reason && <small>{lang === "da" ? "Seneste: " : "Latest: "}{learned.last.reason}</small>}</span>
          <span>{lang === "da" ? "Anlægget kører nu" : "The unit runs now"}<strong>{typeof controller.balance_running_excess_percent === "number" ? (lang === "da" ? `Udsugning ${signed(controller.balance_running_excess_percent)}` : `Extract ${signed(controller.balance_running_excess_percent)}`) : "—"}</strong></span>
        </div>
        {balanceStatus.error && <p className="settings-warning">{balanceStatus.error}</p>}
        {balanced && <div className="settings-table-wrap"><table className="settings-table"><thead><tr><th>{t(T.level)}</th><th>{lang === "da" ? "Udsugning" : "Extract"}</th><th>{balanceOn ? (lang === "da" ? "Indblæsning" : "Supply") : (lang === "da" ? "Indblæsning med Auto" : "Supply with Auto")}</th><th>{lang === "da" ? "Luft ind / ud" : "Air in / out"}</th><th>{lang === "da" ? "Udsugning over indbl." : "Extract above supply"}</th><th>{lang === "da" ? "Kører nu" : "Running now"}</th></tr></thead>
          <tbody>{levels.map(level => { const row = balanced[String(level)]; const running = runningProfiles?.[String(level)]; const current = balanceStatus.levels?.[String(level)]; if (!row) return null; return <tr key={level}>
            <td>{level}</td>
            <td>{row.extract} %</td>
            <td>{row.supply} %</td>
            <td>{row.supply_m3h} / {row.extract_m3h} m³/h</td>
            <td className={row.reached ? undefined : "settings-table-warn"}>{signed(row.excess_percent)}</td>
            <td>{running ? `${s(running.supply)}/${s(running.extract)} % · ${signed(current?.current_excess_percent)}` : "—"}</td>
          </tr>; })}</tbody></table></div>}
        <p className="settings-help">{lang === "da"
          ? "Varmebalancen: veksleren kan kun give indblæsningen den varme, udsugningen afleverer. Luftmængdernes forhold er derfor forholdet mellem udsugningens temperaturfald (T3 − T4) og indblæsningens temperaturstigning (T2 − T1). Kun rolige perioder tæller: samme trin i 30 minutter, mindst 8 °C mellem inde og ude, ingen bypass, pejs, frost eller kondens i veksleren. Halvdelen af ventilatorernes varme trækkes fra og regnes med i usikkerheden. Luft ind/ud er skønnet ud fra omdrejningerne og maks. luftmængden."
          : "Heat balance: the exchanger can only give the supply air the heat the extract air hands over. The airflow ratio is therefore the ratio of the extract temperature drop (T3 − T4) to the supply temperature rise (T2 − T1). Only calm periods count: the same level for 30 minutes, at least 8 °C between inside and outside, no bypass, fireplace, frost or condensation in the core. Half of the fans' heat is taken out and counted in the uncertainty. Air in/out is estimated from the fan speed and the maximum airflow."}</p>
        <div className="diag-actions"><button type="button" className="secondary-action" disabled={busy || !csrf} onClick={() => void resetLearning()}><RotateCcw size={14}/>{lang === "da" ? "Nulstil læring" : "Reset learning"}</button></div>
      </Card>
      <Card title={lang === "da" ? "Beregning" : "Calculation"} lead={lang === "da" ? "Følger felterne, mens du taster. Styringen bruger tallene, når du gemmer." : "Follows the fields as you type. Control uses them once you save."} icon={Gauge}>
        <div className="settings-summary">
          <span>{lang === "da" ? "Luftvolumen" : "Air volume"}<strong>{fmt(plan.volume_m3, lang, 0, " m³")}</strong></span>
          <span>{lang === "da" ? "Krav, indblæsning" : "Required supply"}<strong>{fmt(plan.supply_required_m3h, lang, 0, " m³/h")}</strong></span>
          <span>{lang === "da" ? "Krav, udsugning" : "Required extract"}<strong>{fmt(plan.extract_required_m3h, lang, 0, " m³/h")}</strong></span>
          <span>{lang === "da" ? "Luftskifte ved kravet" : "Air changes at requirement"}<strong>{fmt(plan.required_air_changes_per_hour, lang, 2, " /h")}</strong></span>
          <span>{lang === "da" ? "Beregnet grundtrin" : "Calculated base level"}<strong>{s(plan.base_level, "—")}</strong></span>
          <span>{lang === "da" ? "Laveste tilladte trin" : "Lowest allowed level"}<strong>{s(plan.min_level, "—")}</strong></span>
        </div>
        {plan.reachable === false && <p className="settings-warning">{lang === "da" ? `Selv trin ${maxLevel} når ikke kravet med de nuværende tal. Tjek areal, maks. luftmængde eller indtast målte værdier.` : `Even level ${maxLevel} does not reach the requirement with the current figures. Check the area, maximum airflow or enter measured values.`}</p>}
        {plan.estimated && <p className="settings-help">{lang === "da"
          ? `Luftmængder uden målt værdi er skønnet ud fra ventilatorernes omdrejninger: luftmængden følger omdrejningstallet (ventilatorloven), og omdrejningerne stiger lineært med procenten fra ca. ${Math.round(plan.fan_curve?.rpm_at_0 ?? 557)} omdr./min ved 0 % plus ${(plan.fan_curve?.rpm_per_percent ?? 24).toLocaleString("da-DK", { maximumFractionDigits: 1 })} pr. %. ${plan.fan_curve?.learned ? "Kurven er lært fra dit anlæg." : "Kurven er HCH5-standard, indtil Pi'en har lært dit anlæg at kende (kræver målinger på mindst tre trin)."} Luftbalancens kanalforhold (${(plan.duct_ratio ?? 1).toLocaleString("da-DK", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}) fordeler luften mellem indblæsning og udsugning. Kanaltrykket er ikke med, så målte luftmængder fra indreguleringsrapporten er altid bedre – indtast dem herunder.`
          : `Airflows without a measured value are estimated from fan speed: airflow follows speed (fan law), and speed rises linearly with the percentage from about ${Math.round(plan.fan_curve?.rpm_at_0 ?? 557)} rpm at 0 % plus ${(plan.fan_curve?.rpm_per_percent ?? 24).toFixed(1)} per %. ${plan.fan_curve?.learned ? "The curve is learned from your unit." : "The curve is the HCH5 default until the Pi has learned your unit (needs readings on at least three levels)."} The duct ratio of the air balance (${(plan.duct_ratio ?? 1).toFixed(2)}) splits the air between supply and extract. Duct pressure is not included, so measured airflows from the commissioning report are always better – enter them below.`}</p>}
        <div className="settings-table-wrap"><table className="settings-table"><thead><tr><th>{t(T.level)}</th><th>{lang === "da" ? "Indblæsning" : "Supply"}</th><th>{lang === "da" ? "Udsugning" : "Extract"}</th><th>{lang === "da" ? "Luftskifte" : "Air changes"}</th><th>{lang === "da" ? "Opfylder krav" : "Meets requirement"}</th><th>{lang === "da" ? "Målt indbl. · ved %" : "Measured supply · at %"}</th><th>{lang === "da" ? "Målt udsug. · ved %" : "Measured extract · at %"}</th></tr></thead>
          <tbody>{levels.map(level => { const row = plan.levels?.[String(level)] ?? {}; return <tr key={level} className={level === plan.base_level ? "is-base" : undefined}>
            <td>{level}</td><td>{fmt(row.supply_m3h, lang, 0, " m³/h")}{row.measured ? "" : " *"}</td><td>{fmt(row.extract_m3h, lang, 0, " m³/h")}</td><td>{fmt(row.air_changes_per_hour, lang, 2)}</td>
            <td>{row.meets_requirement ? "✓" : row.meets_reduced ? (lang === "da" ? "Reduceret" : "Reduced") : "—"}</td>
            <td><input aria-label={`${t(T.level)} ${level} supply`} type="number" min="10" max="1500" placeholder={row.estimate_supply_m3h === undefined ? undefined : String(row.estimate_supply_m3h)} value={airflow[level]?.supply ?? ""} onChange={e => setAirflow(level, "supply", e.target.value)}/><input className="settings-table-percent" aria-label={`${t(T.level)} ${level} supply %`} type="number" min="1" max="100" placeholder={s(runningProfiles?.[String(level)]?.supply)} value={airflow[level]?.supply_percent ?? ""} onChange={e => setAirflowPercent(level, "supply", e.target.value)}/></td>
            <td><input aria-label={`${t(T.level)} ${level} extract`} type="number" min="10" max="1500" placeholder={row.estimate_extract_m3h === undefined ? undefined : String(row.estimate_extract_m3h)} value={airflow[level]?.extract ?? ""} onChange={e => setAirflow(level, "extract", e.target.value)}/><input className="settings-table-percent" aria-label={`${t(T.level)} ${level} extract %`} type="number" min="1" max="100" placeholder={s(runningProfiles?.[String(level)]?.extract)} value={airflow[level]?.extract_percent ?? ""} onChange={e => setAirflowPercent(level, "extract", e.target.value)}/></td>
          </tr>; })}</tbody></table></div>
        <p className="settings-help">{lang === "da" ? "* skønnet. De grå tal i felterne er skønnet; skriv den målte værdi fra indreguleringsrapporten for at erstatte det, og ventilatorprocenten den blev målt ved. Én målt værdi pr. side er nok til at rette skønnet på alle trin, og målinger på begge sider giver luftbalancen det præcise kanalforhold." : "* estimated. The grey numbers in the fields are the estimate; type the measured value from the commissioning report to replace it, and the fan percentage it was measured at. One measured value per side corrects the estimate on every level, and values on both sides give the air balance the exact duct ratio."}</p>
      </Card>
    </>,
    air: <>
      <Card title={lang === "da" ? "Generel luftkvalitet" : "General air quality"} lead={lang === "da" ? "Gælder Local Auto og Smart Auto" : "Applies to Local Auto and Smart Auto"} icon={Wind}>
        <div className="settings-grid">
          <label>{lang === "da" ? "Normaltrin" : "Normal level"}<input type="number" min="1" max={maxLevel} disabled={sizing} value={sizing ? n(controller.effective_normal_level, 3) : n(form.local_normal_level, 3)} onChange={num("local_normal_level")}/><Help>{sizing ? (lang === "da" ? "Styres lige nu af husets størrelse (se Hus og luftmængde)." : "Currently set by the house size (see House and airflow).") : (lang === "da" ? "Trinnet anlægget kører på, når luften er god." : "The level the unit runs at when the air is good.")}</Help></label>
          <label>{lang === "da" ? "Maks. trin ved CO₂ og fugt" : "Maximum level for CO₂ and humidity"}<input type="number" min="1" max={maxLevel} value={n(form.air_quality_max_level, maxLevel)} onChange={num("air_quality_max_level")}/><Help>{lang === "da" ? "Begrænser hvor højt CO₂ og almindelig fugt kan hæve ventilationen i Local Auto og Smart Auto. Normaltrinnet kan stadig være højere. Badeværelsesudtørring og PM2.5 har egne grænser." : "Limits how far CO₂ and ordinary humidity can raise ventilation in Local Auto and Smart Auto. The normal level may still be higher. Bathroom drying and PM2.5 have their own limits."}</Help></label>
          <label>{lang === "da" ? "Fugt (RH) grænse" : "Humidity (RH) limit"}<input type="number" min="25" max="80" value={n(form.rh_setpoint, 50)} onChange={num("rh_setpoint")}/><span>%</span><Help>{lang === "da" ? "Over denne relative fugt skrues der op (se Fugt pr. trin)." : "Above this relative humidity the fans step up (see Humidity per level)."}</Help></label>
          <label>{lang === "da" ? "Fugt hysterese" : "Humidity hysteresis"}<input type="number" min="1" max="10" value={n(form.rh_hysteresis, 3)} onChange={num("rh_hysteresis")}/><span>%</span><Help>{lang === "da" ? "Hvor langt under grænsen fugten skal ned, før der skrues ned igen. Forhindrer at trinnet hopper frem og tilbage." : "How far below the limit humidity must fall before stepping down again. Stops the level from flapping."}</Help></label>
          <label>{lang === "da" ? "CO₂ grænse" : "CO₂ limit"}<input type="number" min="500" max="2000" step="50" value={n(form.co2_setpoint, 800)} onChange={num("co2_setpoint")}/><span>ppm</span><Help>{lang === "da" ? "Over denne CO₂ skrues der op. Udeluft er ca. 420 ppm, og over 1000 ppm føles luften tung." : "Above this CO₂ the fans step up. Outdoor air is about 420 ppm; above 1000 ppm the air feels stuffy."}</Help></label>
          <label>{lang === "da" ? "CO₂ hysterese" : "CO₂ hysteresis"}<input type="number" min="25" max="500" step="25" value={n(form.co2_hysteresis, 100)} onChange={num("co2_hysteresis")}/><span>ppm</span><Help>{lang === "da" ? "Hvor langt under grænsen CO₂ skal ned, før der skrues ned igen." : "How far below the limit CO₂ must fall before stepping down."}</Help></label>
          <label>{lang === "da" ? "Fugt pr. trin" : "Humidity per level"}<input type="number" min="2" max="20" value={n(form.auto_step_rh, 5)} onChange={num("auto_step_rh")}/><span>%</span><Help>{lang === "da" ? "Så mange procentpoint over grænsen giver et trin mere." : "This many percentage points above the limit give one more level."}</Help></label>
          <label>{lang === "da" ? "CO₂ pr. trin" : "CO₂ per level"}<input type="number" min="50" max="1000" step="50" value={n(form.auto_step_co2, 200)} onChange={num("auto_step_co2")}/><span>ppm</span><Help>{lang === "da" ? "Så mange ppm over grænsen giver et trin mere." : "This many ppm above the limit give one more level."}</Help></label>
          <label>{lang === "da" ? "CO₂ kalibrering" : "CO₂ calibration"}<input type="number" min="-1000" max="1000" step="10" value={n(form.co2_offset, 0)} onChange={num("co2_offset")}/><span>ppm</span><Help>{lang === "da" ? `Lægges til anlæggets egen CO₂-måler, før der styres og vises. Måler den fx 200 for højt i forhold til dine rumfølere, så sæt −200. Lige nu: rå ${fmt(controller.co2_raw, lang, 0, " ppm")}, korrigeret ${fmt(controller.co2_measured, lang, 0, " ppm")}.` : `Added to the unit's own CO₂ sensor before it is used for control and shown. If it reads 200 too high compared with your room sensors, set −200. Now: raw ${fmt(controller.co2_raw, lang, 0, " ppm")}, corrected ${fmt(controller.co2_measured, lang, 0, " ppm")}.`}</Help></label>
          <label>{lang === "da" ? "Timeout for HA-rumdata" : "HA room data timeout"}<input type="number" min="60" max="3600" step="30" value={n(form.ha_timeout_seconds, 300)} onChange={num("ha_timeout_seconds")}/><span>s</span><Help>{lang === "da" ? "Hører controlleren ikke fra Home Assistant så længe, falder Smart Auto tilbage til anlæggets egne følere." : "If the controller hears nothing from Home Assistant for this long, Smart Auto falls back to the unit's own sensors."}</Help></label>
        </div>
      </Card>
      <Card title={lang === "da" ? "Fint støv (PM2.5)" : "Fine dust (PM2.5)"} lead={lang === "da" ? "Valgfrit: rumfølere fra Home Assistant, fx IKEA-sensorer" : "Optional: room sensors from Home Assistant, e.g. IKEA sensors"} icon={Wind}>
        <div className="settings-grid">
          <label className="check-row"><input type="checkbox" checked={form.pm25_enabled === true} onChange={check("pm25_enabled")}/> {lang === "da" ? "Brug PM2.5 til at hæve ventilationen" : "Use PM2.5 to raise the ventilation"}</label>
          <Help>{lang === "da" ? "Gælder Smart Auto. Madlavning, stearinlys og brændeovn giver fint støv; mere luftskifte fjerner det hurtigere. PM2.5 tæller kun, når det er over grænsen, og kan kun hæve trinnet. Er der mere fint støv i luften udenfor (rummet med typen Ude på siden Rum), skrues der ikke op for det. CO₂ og fugt fra samme føler bruges som før." : "Applies to Smart Auto. Cooking, candles and stoves make fine dust; more air change removes it faster. PM2.5 can only raise the level, never lower it, and CO₂ and humidity from the same sensor are used as before."}</Help>
          <label>{lang === "da" ? "Grænse" : "Limit"}<input type="number" min="5" max="200" value={n(form.pm25_setpoint, 25)} onChange={num("pm25_setpoint")}/><span>µg/m³</span><Help>{lang === "da" ? "Over denne værdi skrues der op. WHO anbefaler under 15 µg/m³ i døgngennemsnit; 25 undgår at almindelig støv giver udslag." : "Above this value the level goes up. WHO recommends below 15 µg/m³ as a daily mean; 25 keeps ordinary dust from reacting."}</Help></label>
          <label>{lang === "da" ? "Et trin pr." : "One level per"}<input type="number" min="2" max="100" value={n(form.pm25_step, 15)} onChange={num("pm25_step")}/><span>µg/m³</span><Help>{lang === "da" ? "Hvor meget over grænsen der skal til for hvert ekstra trin." : "How far above the limit each extra level needs."}</Help></label>
          <label>{lang === "da" ? "Hysterese" : "Hysteresis"}<input type="number" min="1" max="50" value={n(form.pm25_hysteresis, 5)} onChange={num("pm25_hysteresis")}/><span>µg/m³</span></label>
          <label>{lang === "da" ? "Højeste trin ved PM2.5" : "Highest level for PM2.5"}<input type="number" min="1" max={maxLevel} value={n(form.pm25_max_level, 5)} onChange={num("pm25_max_level")}/><Help>{lang === "da" ? "Fint støv kan altid skrue mindst ét trin over normaltrinnet. Et højere tal her giver lov til mere." : "Fine dust can always go at least one level above the normal level. A higher number here allows more."}</Help></label>
          {(() => {
            const smart = (controller.smart_rooms ?? {}) as Record<string, { pm25?: number }>;
            const pmRooms = Object.entries(smart).filter(([, values]) => typeof values?.pm25 === "number");
            const ignored = Array.isArray(form.pm25_ignored_rooms) ? form.pm25_ignored_rooms.map(String) : [];
            if (!pmRooms.length) return <p className="settings-help">{lang === "da" ? "Ingen rum sender PM2.5 endnu. Tilføj PM2.5-føleren til rummet i Home Assistant-integrationen (samme rum som CO₂-føleren)." : "No room sends PM2.5 yet. Add the PM2.5 sensor to the room in the Home Assistant integration (same room as the CO₂ sensor)."}</p>;
            return <div className="pm25-rooms">
              <span>{lang === "da" ? "Rum der må styre på PM2.5" : "Rooms allowed to control on PM2.5"}</span>
              {pmRooms.map(([name, values]) => <label key={name} className="check-row pm25-room"><input type="checkbox" checked={!ignored.includes(name)} onChange={e => set("pm25_ignored_rooms", e.target.checked ? ignored.filter(r => r !== name) : [...ignored, name])}/><em>{name}<small>{fmt(values.pm25, lang, 0, " µg/m³")}</small></em></label>)}
            </div>;
          })()}
        </div>
      </Card>
    </>,
    humidity: <>
      <Card title={lang === "da" ? "Fugt efter absolut vandindhold" : "Humidity by absolute water content"} lead={lang === "da" ? "Om sommeren kan udeluften være fugtigere end inde" : "In summer outdoor air can be wetter than indoor air"} icon={Droplets}>
        <div className="settings-grid">
          <label className="check-row"><input type="checkbox" checked={form.humidity_smart_enabled === true} onChange={check("humidity_smart_enabled")}/> {lang === "da" ? "Skru kun op for fugt, når udeluften tørrer" : "Only raise for humidity when outdoor air dries"}</label>
          <Help>{lang === "da" ? "Controlleren regner fugten om til gram vand pr. m³ inde og ude. Indeholder udeluften lige så meget vand, fjerner mere ventilation ikke fugt, og fugtkravet ignoreres. CO₂ virker stadig." : "The controller converts humidity to grams of water per m³ indoors and outdoors. If outdoor air holds as much water, more ventilation removes no moisture and the humidity demand is ignored. CO₂ still applies."}</Help>
          <label>{lang === "da" ? "Udeluftens fugt" : "Outdoor humidity"}<select value={s(form.outdoor_humidity_source)} onChange={e => set("outdoor_humidity_source", e.target.value)}><option value="">{t(T.none)}</option><option value="weather">{lang === "da" ? "Vejr fra Home Assistant (med T1-kontrol)" : "Home Assistant weather (checked against T1)"}</option>{roomOptions(form.outdoor_humidity_source)}</select><Help>{lang === "da" ? "Vælg en weather-entitet i HCH5 Control-integrationen i Home Assistant. Controlleren bruger anlæggets målte T1 og kun frisk vejr-fugt, når vejrkildens temperatur højst afviger 6 °C fra T1. Ved manglende data bruges normal lokal fugtstyring." : "Select a weather entity in the HCH5 Control integration in Home Assistant. The controller uses measured T1 and fresh weather humidity only when its temperature is within 6 °C of T1. Missing data falls back to normal local humidity control."}</Help>{roomHint}</label>
          <label>{lang === "da" ? "Margin" : "Margin"}<input type="number" min="0" max="3" step="0.1" value={n(form.humidity_margin_gm3, 0.5)} onChange={num("humidity_margin_gm3")}/><span>g/m³</span><Help>{lang === "da" ? "Så meget tørrere skal udeluften mindst være, før fugt tæller." : "How much drier outdoor air must be before humidity counts."}</Help></label>
        </div>
        <div className="settings-summary">
          <span>{lang === "da" ? "Inde" : "Indoor"}<strong>{fmt(controller.indoor_absolute_humidity, lang, 1, " g/m³")}</strong></span>
          <span>{lang === "da" ? "Ude" : "Outdoor"}<strong>{fmt(controller.outdoor_absolute_humidity, lang, 1, " g/m³")}</strong></span>
          <span>{lang === "da" ? "Udeluften tørrer" : "Outdoor air dries"}<strong>{controller.humidity_drying === false ? (lang === "da" ? "Nej" : "No") : (lang === "da" ? "Ja" : "Yes")}</strong></span>
        </div>
      </Card>
      <Card title={lang === "da" ? "Beskyttelse mod tør luft" : "Dry-air protection"} lead={lang === "da" ? "Om vinteren kan for meget ventilation udtørre huset" : "In winter too much ventilation can dry out the house"} icon={Droplets}>
        <div className="settings-grid">
          <label className="check-row"><input type="checkbox" checked={form.dry_protection_enabled === true} onChange={check("dry_protection_enabled")}/> {lang === "da" ? "Begræns ventilationen ved tør luft" : "Limit ventilation when the air is dry"}</label>
          <Help>{lang === "da" ? "Når rummene i Home Assistant er tørrere end grænsen, og CO₂ er under sin grænse i alle rum, går anlægget højst op på det valgte trin. Anlæggets egen fugtføler bruges ikke til at afgøre, hvornår begrænsningen gælder, og uden friske rum fra Home Assistant er den slået fra. Stiger CO₂ over sin grænse, eller skal et badeværelse eller et fugtigt rum tørres ud, slipper begrænsningen straks; det gælder også, når anlæggets egen fugtføler beder om udtørring. Efter et CO₂-stop, eller hvis CO₂ har været over grænsen, mens den var slået fra, starter den først igen, når CO₂ er nede under grænsen minus hysteresen." : "When the Home Assistant rooms are drier than the limit and CO₂ is below its limit in every room, the unit runs at most at the chosen level. The unit's own humidity sensor is not used to decide when the limit applies, and without fresh rooms from Home Assistant it is off. If CO₂ rises over its limit, or a bathroom or a humid room needs drying, the limit is lifted at once; so it is when the unit's own humidity sensor asks for drying. After a CO₂ stop, or when CO₂ has been over its limit while it was off, it only starts again once CO₂ is below the limit minus the hysteresis."}</Help>
          <label>{lang === "da" ? "Tør luft under" : "Dry air below"}<input type="number" min="15" max="45" value={n(form.dry_rh_limit, 30)} onChange={num("dry_rh_limit")}/><span>% RH</span><Help>{lang === "da" ? "Under ca. 30 % RH giver tørre øjne, statisk elektricitet og revner i trægulve." : "Below about 30 % RH causes dry eyes, static and cracks in wooden floors."}</Help></label>
          <label>{lang === "da" ? "Maks. trin ved tør luft" : "Maximum level in dry air"}<input type="number" min="1" max={maxLevel} value={n(form.dry_max_level, 2)} onChange={num("dry_max_level")}/><Help>{lang === "da" ? "Styres grundtrinnet af husets størrelse, går det aldrig under det reducerede minimum." : "If the base level follows the house size, it never goes below the reduced minimum."}</Help></label>
          <label>{lang === "da" ? "Mindste tid før skift" : "Minimum time between changes"}<input type="number" min="1" max="240" value={n(form.dry_hold_minutes, 10)} onChange={num("dry_hold_minutes")}/><span>min</span><Help>{lang === "da" ? "Så længe holdes begrænsningen slået til eller fra, før den må skifte igen. Den slutter dog straks ved høj CO₂ eller fugt, der skal tørres ud. Den stopper, når rummene er grænsen plus vinterfunktionens hysterese." : "How long the limit stays on or off before it may change again. High CO₂ or humidity that needs drying still ends it at once. It stops when the rooms reach the limit plus the winter mode's hysteresis."}</Help></label>
        </div>
      </Card>
      <Card title={lang === "da" ? "Vinter: tør luft" : "Winter: dry air"} lead={lang === "da" ? "Lavere grundtrin, når det fryser og huset er tørt" : "A lower base level when it freezes and the house is dry"} icon={Droplets}>
        <div className="settings-grid">
          <label className="check-row"><input type="checkbox" checked={form.winter_dry_enabled === true} onChange={check("winter_dry_enabled")}/> {lang === "da" ? "Sænk grundtrinnet i koldt, tørt vejr" : "Lower the base level in cold, dry weather"}</label>
          <Help>{lang === "da" ? "Kold udeluft indeholder næsten ingen fugt. Når T1 er under grænsen og luften inde er tørrere end grænsen, kører Auto på grundtrinnet herunder. CO₂ og fugt løfter stadig trinnet; høj CO₂, badeværelse der tørres ud, høj fugt, Quick Boost, manuel styring og husets minimum vinder altid." : "Cold outdoor air holds almost no moisture. When T1 is below the limit and indoor air is drier than the limit, Auto runs at the base level below. CO₂ and humidity still lift the level; high CO₂, bathroom drying, high humidity, Quick Boost, manual control and the house minimum always win."}</Help>
          <label>{lang === "da" ? "Koldt ude under (T1)" : "Cold outside below (T1)"}<input type="number" min="-25" max="15" step="0.5" value={n(form.winter_outdoor_max, 3)} onChange={num("winter_outdoor_max")}/><span>°C</span></label>
          <label>{lang === "da" ? "Tør luft inde under" : "Dry indoor air below"}<input type="number" min="20" max="55" value={n(form.winter_rh_limit, 38)} onChange={num("winter_rh_limit")}/><span>% RH</span></label>
          <label>{lang === "da" ? "Grundtrin om vinteren" : "Winter base level"}<input type="number" min="1" max={maxLevel} value={n(form.winter_level, 2)} onChange={num("winter_level")}/></label>
          <label>{lang === "da" ? "Indendørs fugt" : "Indoor humidity"}<select value={s(form.winter_rh_basis, "average")} onChange={e => set("winter_rh_basis", e.target.value)}><option value="average">{lang === "da" ? "Gennemsnit af rummene" : "Average of the rooms"}</option><option value="lowest">{lang === "da" ? "Tørreste rum" : "Driest room"}</option></select><Help>{lang === "da" ? "Rummene fra Home Assistant uden badeværelser og rum med styring slået fra. Anlæggets egen fugtføler viser alt for lavt og bruges ikke: uden friske rum venter vinterfunktionen." : "The Home Assistant rooms without bathrooms and rooms with control off. The unit's own humidity sensor reads far too low and is not used: without fresh rooms winter mode waits."}</Help></label>
          <label>{lang === "da" ? "Hysterese ude" : "Outdoor hysteresis"}<input type="number" min="0.5" max="10" step="0.5" value={n(form.winter_outdoor_hysteresis, 2)} onChange={num("winter_outdoor_hysteresis")}/><span>°C</span></label>
          <label>{lang === "da" ? "Hysterese fugt" : "Humidity hysteresis"}<input type="number" min="1" max="10" value={n(form.winter_rh_hysteresis, 3)} onChange={num("winter_rh_hysteresis")}/><span>%</span></label>
          <label>{lang === "da" ? "Mindste tid før skift" : "Minimum time between changes"}<input type="number" min="5" max="240" value={n(form.winter_hold_minutes, 30)} onChange={num("winter_hold_minutes")}/><span>min</span><Help>{lang === "da" ? "Også hvor længe en pause for CO₂ eller fugt varer, efter at årsagen er væk." : "Also how long a pause for CO₂ or humidity lasts after the cause is gone."}</Help></label>
        </div>
        <div className="settings-summary">
          <span>{lang === "da" ? "Status" : "Status"}<strong>{controller.winter_dry_active === true ? (lang === "da" ? "Aktiv" : "Active") : controller.winter_dry_state === "paused" ? (lang === "da" ? "På pause" : "Paused") : (lang === "da" ? "Ikke aktiv" : "Not active")}</strong></span>
          <span>{lang === "da" ? "Fugt inde" : "Indoor humidity"}<strong>{fmt(controller.winter_indoor_rh, lang, 0, " %")}</strong></span>
        </div>
      </Card>
      <BathroomCards lang={lang} form={form} controller={controller} set={set} maxLevel={maxLevel}/>
    </>,
    night: <Card title={lang === "da" ? "Natdrift" : "Night mode"} lead={lang === "da" ? "Nat og luftkvalitet kæmper ikke mod hinanden" : "Night mode and air quality never fight"} icon={Moon}>
      <div className="settings-grid">
        <label className="check-row"><input type="checkbox" checked={form.night_enabled === true} onChange={check("night_enabled")}/> {lang === "da" ? "Natsænkning" : "Night reduction"}</label>
        <Help>{lang === "da" ? "Sænker ventilationen i tidsrummet, så anlægget er mere stille." : "Lowers ventilation during the period so the unit is quieter."}</Help>
        <label>{lang === "da" ? "Start" : "Start"}<input type="time" value={s(form.night_start, "22:00")} onChange={e => set("night_start", e.target.value)}/></label>
        <label>{lang === "da" ? "Slut" : "End"}<input type="time" value={s(form.night_end, "06:00")} onChange={e => set("night_end", e.target.value)}/></label>
        <label>{lang === "da" ? "Nat-trin" : "Night level"}<input type="number" min="1" max={maxLevel} value={n(form.night_level, 2)} onChange={num("night_level")}/><Help>{lang === "da" ? "Trinnet om natten, når luften er god." : "The level at night when the air is good."}</Help></label>
        <label>{lang === "da" ? "Maks. ved dårlig luft" : "Maximum with poor air"}<input type="number" min="1" max={maxLevel} value={n(form.night_air_quality_max_level, 4)} onChange={num("night_air_quality_max_level")}/><Help>{lang === "da" ? "Om natten må fugt og CO₂ højst løfte til dette trin. Udtørring af badeværelset efter et bad er undtaget og må køre fuldt op." : "At night humidity and CO₂ can raise ventilation to this level at most. Drying a bathroom out after a shower is exempt and may run at full level."}</Help></label>
      </div>
    </Card>,
    afterheat: <>
      <Card title={lang === "da" ? "Eftervarme" : "Afterheat"} lead={lang === "da" ? "HAC1 styrer selv ventil, aktuator og frostsikring" : "HAC1 handles the valve, actuator and frost protection itself"} icon={Thermometer}>
        <div className="settings-grid">
          <label>{lang === "da" ? "Indblæsning, grundsetpunkt" : "Supply air base setpoint"}<input type="number" min="10" max="35" value={n(form.afterheat_setpoint, 20)} onChange={num("afterheat_setpoint")}/><span>°C</span><Help>{lang === "da" ? "Temperaturen luften varmes op til efter varmefladen. Er Mål fra rummene slået til, er det kun udgangspunktet." : "The temperature the air is heated to after the coil. With the target from the rooms on, this is only the starting point."}</Help></label>
          <label>{lang === "da" ? "Varmeflade" : "Heating coil"}<select value={form.afterheat_coil === "water" ? "water" : "electric"} onChange={e => set("afterheat_coil", e.target.value)}><option value="electric">{lang === "da" ? "Elvarmeflade" : "Electric coil"}</option><option value="water">{lang === "da" ? "Vandbåren varmeflade" : "Water coil"}</option></select><Help>{lang === "da" ? "Ændrer kun tegningen på forsiden." : "Only changes the drawing on the overview."}</Help></label>
          <p className="settings-help">{lang === "da" ? "HAC1 varmer aldrig, når udetemperaturen er 15 °C eller mere. Det er anlæggets egen spærre." : "HAC1 never heats when it is 15 °C or warmer outside. That is the unit's own lockout."}</p>
        </div>
      </Card>
      <RoomTargetCard lang={lang} form={form} controller={controller} set={set} roomOptions={roomOptions}/>
      {typeof controller.afterheat_control_mode === "string" && <AfterheatControlCard lang={lang} form={form} controller={controller} set={set}/>}
      {(readFrostLock(controller).known || typeof controller.frost_lock_enabled === "boolean") && <FrostLockCard lang={lang} form={form} controller={controller} set={set}/>}
    </>,
    cooling: <><Card title={lang === "da" ? "Frikøling" : "Free cooling"} lead={t(sectionInfo("cooling").lead)} icon={Snowflake}>
      <div className="settings-grid">
        <label className="check-row"><input type="checkbox" checked={form.cooling_enabled === true} onChange={check("cooling_enabled")}/> {lang === "da" ? "Frikøling" : "Free cooling"}</label>
        <Help>{lang === "da" ? "Er det for varmt inde og køligere ude, åbnes bypass, så udeluften går uden om varmeveksleren. Bypass-spjældet er langsomt (ca. 3 minutter), derfor venter controlleren og holder minimumstider." : "If it is too warm inside and cooler outside, the bypass opens so outdoor air skips the heat exchanger. The damper is slow (about 3 minutes), so the controller waits and keeps minimum times."}</Help>
        <label>{lang === "da" ? "Start ved rumtemperatur" : "Start at room temperature"}<input type="number" min="18" max="30" step="0.5" value={n(form.cooling_room_setpoint, 23)} onChange={num("cooling_room_setpoint")}/><span>°C</span></label>
        <label>{lang === "da" ? "Hysterese" : "Hysteresis"}<input type="number" min="0.2" max="3" step="0.1" value={n(form.cooling_hysteresis, 0.5)} onChange={num("cooling_hysteresis")}/><span>°C</span></label>
        <label>{lang === "da" ? "Laveste udetemperatur" : "Lowest outdoor temperature"}<input type="number" min="-10" max="25" step="0.5" value={n(form.cooling_outdoor_min, 12)} onChange={num("cooling_outdoor_min")}/><span>°C</span><Help>{lang === "da" ? "Koldere udeluft end dette blæses ikke ind uden varmegenvinding." : "Outdoor air colder than this is never blown in without heat recovery."}</Help></label>
        <label>{lang === "da" ? "Mindste forskel inde/ude" : "Minimum indoor/outdoor difference"}<input type="number" min="0.5" max="10" step="0.5" value={n(form.cooling_min_delta, 1.5)} onChange={num("cooling_min_delta")}/><span>°C</span></label>
        <label>{lang === "da" ? "Trin under frikøling" : "Level during free cooling"}<input type="number" min="1" max={maxLevel} value={n(form.cooling_level, 4)} onChange={num("cooling_level")}/></label>
        <label>{lang === "da" ? "Vent før start" : "Wait before start"}<input type="number" min="0" max="1800" step="30" value={n(form.cooling_start_delay_seconds, 180)} onChange={num("cooling_start_delay_seconds")}/><span>s</span></label>
        <label>{lang === "da" ? "Mindste tid tændt" : "Minimum on time"}<input type="number" min="0" max="3600" step="60" value={n(form.cooling_min_on_seconds, 600)} onChange={num("cooling_min_on_seconds")}/><span>s</span></label>
        <label>{lang === "da" ? "Mindste tid slukket" : "Minimum off time"}<input type="number" min="0" max="3600" step="60" value={n(form.cooling_min_off_seconds, 300)} onChange={num("cooling_min_off_seconds")}/><span>s</span></label>
      </div>
    </Card>
      <NightCoolCard lang={lang} form={form} controller={controller} set={set} maxLevel={maxLevel}/>
    </>,
    fireplace: <Card title={t(sectionInfo("fireplace").title)} lead={lang === "da" ? "Holder anlæggets pejsefunktion, så længe der fyres" : "Holds the unit's fireplace mode while the stove burns"} icon={Flame}>
      <div className="settings-grid">
        <label className="check-row"><input type="checkbox" checked={form.fireplace_auto_enabled === true} onChange={check("fireplace_auto_enabled")}/> {lang === "da" ? "Automatisk pejsefunktion" : "Automatic fireplace mode"}</label>
        <Help>{lang === "da" ? "Pejsefunktionen giver overtryk i huset, så røgen ikke suges ind. Den startes af brændeovnens føler eller af en ekstern switch (Home Assistant, knap, automation). Bypass lukkes, mens den er aktiv. Slukker du pejsefunktionen manuelt, venter automatikken, til signalet har været væk." : "Fireplace mode gives positive pressure so smoke is not drawn in. It is started by the stove sensor or by an external switch (Home Assistant, button, automation). The bypass is closed while it is active. If you turn fireplace mode off manually, the automation waits until the signal has cleared."}</Help>
        <label>{lang === "da" ? "Brændeovnens føler" : "Stove sensor"}<select value={s(form.fireplace_auto_source)} onChange={e => set("fireplace_auto_source", e.target.value)}><option value="">{lang === "da" ? "Ingen – kun ekstern switch" : "None – external switch only"}</option>{roomOptions(form.fireplace_auto_source)}</select><Help>{lang === "da" ? "Et målerum fra Home Assistant med temperaturen ved brændeovnen." : "A measurement room from Home Assistant with the temperature at the stove."}</Help>{roomHint}{sourceStatus(controller.fireplace_auto_source_status, "stove")}</label>
        <label>{lang === "da" ? "Start ved" : "Start at"}<input type="number" min="15" max="400" step="0.5" value={n(form.fireplace_auto_on_temp, 25)} onChange={num("fireplace_auto_on_temp")}/><span>°C</span></label>
        <label>{lang === "da" ? "Stop under" : "Stop below"}<input type="number" min="10" max="399" step="0.5" value={n(form.fireplace_auto_off_temp, 24)} onChange={num("fireplace_auto_off_temp")}/><span>°C</span><Help>{lang === "da" ? "Skal være lavere end start, så den ikke slår til og fra." : "Must be lower than start so it does not flap on and off."}</Help></label>
        <label>{lang === "da" ? "Efterløb" : "Afterrun"}<input type="number" min="0" max="120" step="5" value={n(form.fireplace_afterrun_minutes, 15)} onChange={num("fireplace_afterrun_minutes")}/><span>min</span><Help>{lang === "da" ? "Så længe fortsætter pejsefunktionen, efter signalet er væk." : "How long fireplace mode continues after the signal is gone."}</Help></label>
        <label>{lang === "da" ? "Maks. varighed" : "Maximum duration"}<input type="number" min="1" max="24" value={n(form.fireplace_max_hours, 6)} onChange={num("fireplace_max_hours")}/><span>{lang === "da" ? "timer" : "hours"}</span><Help>{lang === "da" ? "Sikkerhed hvis en føler eller switch hænger. Derefter stopper den, til signalet har været væk." : "Safety if a sensor or switch gets stuck. It then stops until the signal has cleared."}</Help></label>
        <label className="check-row"><input type="checkbox" checked={form.fireplace_cold_guard_enabled === true} onChange={check("fireplace_cold_guard_enabled")}/> {lang === "da" ? "Stop pejsefunktionen, når luften ind i huset bliver for kold" : "Stop fireplace mode when the air into the house gets too cold"}</label>
        <Help>{lang === "da" ? "Under pejsefunktionen står udsugningen stille, så den varme luft fra huset ikke varmer den friske luft op. Slået til stopper pejsefunktionen, når luften ind i rummene har været under 9 °C i 5 minutter, og den starter ikke automatisk igen, før luften er over 10 °C." : "In fireplace mode the extract fan stands still, so the warm house air does not warm the fresh air. On, fireplace mode stops when the air into the rooms has been below 9 °C for 5 minutes, and it does not start automatically again until the air is above 10 °C."}</Help>
      </div>
      <div className="settings-summary">
        <span>{lang === "da" ? "Brændeovn" : "Stove"}<strong>{fmt(controller.stove_temperature, lang, 1, " °C")}</strong></span>
        <span>{lang === "da" ? "Automatik" : "Automation"}<strong>{controller.fireplace_auto_active === true ? t(T.on) : controller.fireplace_auto_reason === "cold_supply" ? (lang === "da" ? "Venter: for kold luft ind" : "Waiting: supply too cold") : controller.fireplace_auto_reason === "source_missing" ? (lang === "da" ? "Mangler brændeovnens rum" : "Stove room missing") : t(T.off)}</strong></span>
      </div>
    </Card>,
    ui: <Card title={t(sectionInfo("ui").title)} lead={lang === "da" ? "Gemmes kun i denne browser" : "Stored in this browser only"} icon={Monitor}>
      <div className="settings-grid">
        <label>{lang === "da" ? "Sprog" : "Language"}<select value={language} onChange={e => setLanguage(e.target.value === "en" ? "en" : "da")}><option value="da">Dansk</option><option value="en">English</option></select><Help>{lang === "da" ? "Indstillinger og forklaringer vises på det valgte sprog." : "Settings and explanations are shown in the chosen language."}</Help></label>
        <label>{lang === "da" ? "Stil" : "Style"}<select value={skin} onChange={e => { const next = e.target.value === "aurora" ? "aurora" : "command"; setSkin(next); setTheme(SKIN_PRESETS[next].theme); setNavPosition(SKIN_PRESETS[next].nav); }}><option value="command">{lang === "da" ? "Command – mørk med sidemenu" : "Command – dark, side menu"}</option><option value="aurora">{lang === "da" ? "Aurora – lys med topmenu" : "Aurora – light, top menu"}</option></select><Help>{lang === "da" ? "Samme to stilarter som Fleetdeck. Tema og menu kan bagefter vælges hver for sig." : "The same two styles as Fleetdeck. Theme and menu can then be changed separately."}</Help></label>
        <label>{lang === "da" ? "Menu" : "Menu"}<select value={navPosition} onChange={e => setNavPosition(e.target.value === "top" ? "top" : "side")}><option value="side">{lang === "da" ? "I siden" : "At the side"}</option><option value="top">{lang === "da" ? "I toppen" : "At the top"}</option></select></label>
        <label>{lang === "da" ? "Tema" : "Theme"}<select value={theme} onChange={e => setTheme(e.target.value)}><option value="system">System</option><option value="light">{lang === "da" ? "Lyst" : "Light"}</option><option value="dark">{lang === "da" ? "Mørkt" : "Dark"}</option></select></label>
        <label>{lang === "da" ? "Sidemenu" : "Sidebar"}<select value={collapsed ? "collapsed" : "expanded"} onChange={e => setCollapsed(e.target.value === "collapsed")}><option value="expanded">{lang === "da" ? "Udvidet" : "Expanded"}</option><option value="collapsed">{lang === "da" ? "Sammenfoldet" : "Collapsed"}</option></select></label>
        <label>{lang === "da" ? "Animation" : "Animation"}<select value={motion} onChange={e => setMotion(e.target.value)}><option value="normal">Normal</option><option value="reduced">{lang === "da" ? "Reduceret" : "Reduced"}</option></select></label>
        <button className="secondary-action" onClick={uiSave}>{t(T.saveUi)}</button>
      </div>
    </Card>,
    security: <Card title={t(sectionInfo("security").title)} lead={t(sectionInfo("security").lead)} icon={ShieldCheck}>
      <AccountPanel lang={lang}/>
      {can("configure") && <><div className="settings-summary">
        <span>Master<strong>{s(controller.active_master, "—")}</strong></span>
        <span>{lang === "da" ? "Skrivninger" : "Writes"}<strong>{controller.hardware_writes_allowed === true ? (lang === "da" ? "Tilladt" : "Allowed") : (lang === "da" ? "Blokeret" : "Blocked")}</strong></span>
      </div>
      <p className="settings-help">{lang === "da" ? "Pi'en skriver kun til anlægget, når den er master. Sidder HCP4-panelet på bussen, vinder det altid." : "The Pi only writes to the unit when it is master. If the HCP4 panel is on the bus, it always wins."}</p></>}
    </Card>,
    users: <Card title={t(sectionInfo("users").title)} lead={lang === "da" ? "Giv familien, teknikeren og dig selv hver deres login og rolle" : "Give the family, the technician and yourself their own login and role"} icon={Users}>
      <UsersPanel lang={lang}/>
    </Card>,
    mail: <Card title={t(sectionInfo("mail").title)} lead={lang === "da" ? "Mail ved fejl på anlægget og link til ny adgangskode" : "Mail on unit faults and links for a new password"} icon={Mail}>
      <MailPanel lang={lang}/>
    </Card>,
  };

  const visible = SECTIONS.filter(item => sectionAllowed(item.id, can));
  const active = visible.find(item => item.id === section) ?? visible[0];
  return <section className="dashboard-overview page-enter">
    <header className="overview-heading-row"><div><span className="eyebrow">{t(T.eyebrow)}</span><h1>{t(T.title)}</h1><p>{t(T.intro)}</p></div></header>
    <div className="settings-layout">
      <nav className="settings-nav" aria-label={t(T.eyebrow)}>{visible.map((item, index) => { const Icon = item.icon; const first = index === 0 || visible[index - 1].group !== item.group; return <div key={item.id} className="settings-nav-item">
        {first && <span className="settings-nav-group">{t(item.group)}</span>}
        <button type="button" aria-current={item.id === active.id ? "page" : undefined} onClick={() => setSection(item.id)}><Icon size={16}/><span><strong>{t(item.title)}</strong><small>{t(item.lead)}</small></span></button>
        {item.id === "sensors" && can("configure") && <a className="settings-nav-link" href="#/rooms"><DoorOpen size={16}/><span><strong>{lang === "da" ? "Rum" : "Rooms"}</strong><small>{lang === "da" ? "Rum og følere fra Home Assistant" : "Rooms and sensors from Home Assistant"}</small></span></a>}
      </div>; })}</nav>
      <div className="settings-content" data-section={active.id}>{sections[active.id]}</div>
    </div>
    {!SELF_SAVING.includes(active.id) && <div className="settings-savebar"><span>{notice || t(T.saveHint)}</span><button className="primary-action" disabled={busy || !csrf} onClick={() => void save()}><Save size={15}/>{busy ? t(T.saving) : t(T.save)}</button></div>}
  </section>;
}
