import type { ReactNode } from "react";
import { CloudMoon, Thermometer } from "lucide-react";
import { da as num1, nightCoolText } from "../lib/automation";
import type { Lang } from "../lib/i18n";

type Data = Record<string, unknown>;
type Props = { lang: Lang; form: Data; controller: Data; set: (key: string, value: unknown) => void };

function n(v: unknown, f: number) { const x = Number(v); return Number.isFinite(x) ? x : f; }
function s(v: unknown, f = "") { return v === null || v === undefined ? f : String(v); }
function deg(v: unknown, unit = " °C", digits = 1) { const x = typeof v === "number" ? v : Number.NaN; return Number.isFinite(x) ? `${num1(x, digits)}${unit}` : "—"; }

function Card({ title, lead, icon: Icon, children }: { title: string; lead: string; icon: typeof Thermometer; children: ReactNode }) {
  return <article className="surface panel-card settings-card"><div className="pro-card-head compact"><div><h2>{title}</h2><p>{lead}</p></div><Icon size={20}/></div>{children}</article>;
}
function Help({ children }: { children: ReactNode }) { return <small className="settings-field-help">{children}</small>; }

const ROOM_STATE: Record<string, [string, string]> = {
  disabled: ["Slået fra", "Off"], no_room_temperature: ["Ingen rumtemperatur – fast setpunkt", "No room temperature – fixed setpoint"],
  holding: ["Holder", "Holding"], in_band: ["Inden for dødbåndet", "Inside the dead band"], rising: ["Hæver", "Rising"],
  falling: ["Sænker", "Lowering"], at_limit: ["Ved grænsen", "At the limit"], saturated: ["Eftervarmen kan ikke følge med", "Afterheat cannot follow"],
  locked: ["Holdes (frost, Køl, natkøling eller slukket)", "Held (frost, cool, night cooling or off)"],
};

/** Afterheat target from the rooms (feature: cascade). Section Varme og køl → Eftervarme og frost. */
export function RoomTargetCard({ lang, form, controller, set, roomOptions }: Props & { roomOptions: (current: unknown) => ReactNode }) {
  const d = lang === "da";
  const numKey = (key: string) => (e: { target: { value: string } }) => set(key, Number(e.target.value));
  const on = form.afterheat_room_enabled === true;
  const state = ROOM_STATE[s(controller.afterheat_room_state, "disabled")] ?? [s(controller.afterheat_room_state), s(controller.afterheat_room_state)];
  const rooms = typeof controller.afterheat_room_rooms === "number" ? ` · ${controller.afterheat_room_rooms} rum` : "";
  return <Card title={d ? "Mål fra rummene" : "Target from the rooms"} lead={d ? "Indblæsningens mål flyttes langsomt efter temperaturen i rummene" : "The supply target follows the rooms slowly"} icon={Thermometer}>
    <div className="settings-grid">
      <label className="check-row"><input type="checkbox" checked={on} onChange={e => set("afterheat_room_enabled", e.target.checked)}/> {d ? "Styr indblæsningen efter rummene" : "Steer the supply air by the rooms"}</label>
      <Help>{d
        ? "En langsom ydre regulering flytter eftervarmens mål for indblæsningen mellem laveste og højeste værdi, så gennemsnittet af rummene nærmer sig den ønskede rumtemperatur. Højst ét lille trin ad gangen (fx 0,2 K pr. 10 min). Den stopper op, når eftervarmen ikke kan følge med (fuldt åben og stadig for koldt) eller er låst af frostsikring, Køl eller natkøling; frostsikringens mindste setpunkt vinder altid. Slået fra bruges grundsetpunktet. Ventilationsluften giver kun få hundrede watt, så det er komfort og ikke opvarmning."
        : "A slow outer loop moves the afterheat's supply target between the lowest and highest value so the rooms' average approaches the wanted room temperature, one small step at a time (e.g. 0.2 K per 10 min). It pauses while the afterheat cannot follow or is locked by the frost lock, Cool or night cooling; the frost lock minimum always wins. When off, the base setpoint is used."}</Help>
      <label>{d ? "Rumtemperatur fra" : "Room temperature from"}<select value={s(form.afterheat_room_source, "auto")} onChange={e => set("afterheat_room_source", e.target.value)}>
        <option value="auto">{d ? "Automatisk: rummene der styrer, ellers alle HA-rum, ellers T3" : "Automatic: controlling rooms, else all HA rooms, else T3"}</option>
        <option value="rooms">{d ? "Rummene der styrer (normale rum, soverum, køkken)" : "Controlling rooms (normal, bedroom, kitchen)"}</option>
        <option value="ha_average">{d ? "Gennemsnit af alle HA-rum (ikke bad)" : "Average of all HA rooms (not bathrooms)"}</option>
        <option value="t3">{d ? "Udsugningsluft T3" : "Extract air T3"}</option>
        <option value="t5">{d ? "HRC2-rumføler T5 (upålidelig uden HCP4)" : "HRC2 room sensor T5 (unreliable without HCP4)"}</option>
        {roomOptions(form.afterheat_room_source)}
      </select><Help>{d ? "Rummene der styrer: rum fra siden Rum af typen Normalt rum, Soverum eller Køkken med styring slået til. Badeværelser, kilder, ude, kun kanalmåling og ikke-ventilerede rum tæller ikke med, og et rum hvis føler ikke har meldt sig i 3 timer springes over." : "Controlling rooms: Normal, Bedroom or Kitchen rooms from the Rooms page with control on. Bathrooms, sources, outdoor, duct-only and excluded rooms do not count; a sensor silent for 3 hours is skipped."}</Help></label>
      <label className="check-row"><input type="checkbox" checked={form.afterheat_room_weighted === true} onChange={e => set("afterheat_room_weighted", e.target.checked)}/> {d ? "Vægt rummene efter prioritet" : "Weigh rooms by priority"}</label>
      <Help>{d ? "Lav tæller halvt, Høj 1½ gang og Kritisk dobbelt i gennemsnittet." : "Low counts half, High 1.5× and Critical double."}</Help>
      <label>{d ? "Ønsket rumtemperatur" : "Wanted room temperature"}<input type="number" min="15" max="26" step="0.1" value={n(form.afterheat_room_target, 22)} onChange={numKey("afterheat_room_target")}/><span>°C</span><Help>{d ? "Sæt den lidt under radiatorernes eller gulvvarmens, så de ikke modarbejder hinanden." : "Slightly below the radiators or floor heating so they do not fight."}</Help></label>
      <label>{d ? "Dødbånd om rummålet" : "Dead band around the target"}<input type="number" min="0" max="1.5" step="0.1" value={n(form.afterheat_room_band_k, 0.3)} onChange={numKey("afterheat_room_band_k")}/><span>± K</span><Help>{d ? "Er rummene inden for ± dette af målet, lades indblæsningens mål i fred. Undgår, at små udsving i rumfølerne flytter eftervarmen." : "Within ± this of the target the supply target is left alone."}</Help></label>
      <label>{d ? "Kanaltab / forskydning" : "Duct loss / offset"}<input type="number" min="-3" max="3" step="0.1" value={n(form.afterheat_room_offset_k, 0)} onChange={numKey("afterheat_room_offset_k")}/><span>K</span><Help>{d ? "Lægges til indblæsningens mål ved anlægget, så luften der når rummene har den rigtige temperatur. Køles indblæsningen fx 0,5 K af i en kold loftkanal, skrives +0,5. Laveste og højeste indblæsning gælder luften ved rummene. Det er ikke det samme som Korrektion udsugningsrum under Følere: den retter kun rumfølerne i beregningen af udsugningskanalens afkøling." : "Added to the supply target at the unit so the air reaching the rooms is right (e.g. +0.5 if a cold loft duct cools it 0.5 K). Not the same as the extract rooms correction under Sensors, which only corrects the room sensors in the extract duct figure."}</Help></label>
      <label>{d ? "Laveste indblæsning" : "Lowest supply"}<input type="number" min="10" max="35" step="1" value={n(form.afterheat_room_min, 17)} onChange={numKey("afterheat_room_min")}/><span>°C</span><Help>{d ? "Under ca. 17 °C kan indblæsningen føles som træk." : "Below about 17 °C the supply air can feel draughty."}</Help></label>
      <label>{d ? "Højeste indblæsning" : "Highest supply"}<input type="number" min="10" max="35" step="1" value={n(form.afterheat_room_max, 24)} onChange={numKey("afterheat_room_max")}/><span>°C</span></label>
      <label>{d ? "Forstærkning" : "Gain"}<input type="number" min="0" max="5" step="0.1" value={n(form.afterheat_room_gain, 1.5)} onChange={numKey("afterheat_room_gain")}/><span>K/K</span><Help>{d ? "Grader indblæsning pr. grad rummene ændrer sig." : "Degrees of supply per degree the rooms change."}</Help></label>
      <label>{d ? "Integraltid" : "Integral time"}<input type="number" min="30" max="600" step="10" value={n(form.afterheat_room_integral_minutes, 180)} onChange={numKey("afterheat_room_integral_minutes")}/><span>min</span><Help>{d ? "Hvor hurtigt en vedvarende afvigelse rettes. Længere er roligere; rummene reagerer over timer." : "How fast a lasting deviation is corrected; longer is calmer."}</Help></label>
      <label>{d ? "Største trin" : "Largest step"}<input type="number" min="0.1" max="0.5" step="0.05" value={n(form.afterheat_room_max_step, 0.2)} onChange={numKey("afterheat_room_max_step")}/><span>K</span></label>
      <label>{d ? "Minutter pr. trin" : "Minutes per step"}<input type="number" min="2" max="60" value={n(form.afterheat_room_step_minutes, 10)} onChange={numKey("afterheat_room_step_minutes")}/><span>min</span></label>
    </div>
    <div className="settings-summary">
      <span>{d ? "Rummene" : "Rooms"}<strong>{deg(controller.afterheat_room_temperature)}{rooms}</strong></span>
      <span>{d ? "Indblæsning ved rummene" : "Supply at the rooms"}<strong>{deg(controller.afterheat_room_rooms_target)}</strong></span>
      <span>{d ? "Setpunkt ved anlægget" : "Setpoint at the unit"}<strong>{deg(controller.afterheat_room_supply_target)}</strong></span>
      <span>{d ? "Tilstand" : "State"}<strong>{d ? state[0] : state[1]}</strong></span>
    </div>
    <p className="settings-help">{s(controller.afterheat_room_reason)}</p>
  </Card>;
}

/** Night cooling from the forecast. Section Varme og køl → Frikøling og bypass. */
export function NightCoolCard({ lang, form, controller, set, maxLevel }: Props & { maxLevel: number }) {
  const d = lang === "da";
  const numKey = (key: string) => (e: { target: { value: string } }) => set(key, Number(e.target.value));
  return <Card title={d ? "Natkøling efter vejrudsigt" : "Night cooling from the forecast"} lead={d ? "Køl huset om natten før en varm dag" : "Cool the house at night before a hot day"} icon={CloudMoon}>
    <div className="settings-grid">
      <label className="check-row"><input type="checkbox" checked={form.night_cool_enabled === true} onChange={e => set("night_cool_enabled", e.target.checked)}/> {d ? "Natkøling efter vejrudsigt" : "Night cooling from the forecast"}</label>
      <Help>{d
        ? "I kølesæsonen (udetemperaturens 5-døgns middel over grænsen) og når DMI venter en varm dag i morgen – eller det var varmt i dag – åbnes bypass og ventilationen skrues op om natten, mens udeluften er mindst forskellen køligere end rummene. Eftervarmen holdes nede som ved Køl. Det stopper, når rummene er nede på mindst-temperaturen, udeluften ikke længere er køligere, eller natvinduet slutter. Pejs, standby, bål, ferie, manuelt trin og frostsikring vinder altid. Vejrudsigten sendes fra Home Assistant (integrationens Energi- og vejrdata)."
        : "In the cooling season and before a hot day (or after one), the bypass opens and the fan steps up at night while outdoor air is cooler than the rooms. The afterheat is held down as with Cool. Fireplace, standby, bonfire, vacation, manual level and the frost lock always win. The forecast comes from Home Assistant."}</Help>
      <label>{d ? "Start" : "Start"}<input type="time" value={s(form.night_cool_start, "23:00")} onChange={e => set("night_cool_start", e.target.value)}/></label>
      <label>{d ? "Slut" : "End"}<input type="time" value={s(form.night_cool_end, "07:00")} onChange={e => set("night_cool_end", e.target.value)}/></label>
      <label>{d ? "Varm dag i morgen fra" : "Hot day tomorrow from"}<input type="number" min="15" max="40" step="0.5" value={n(form.night_cool_forecast_c, 24)} onChange={numKey("night_cool_forecast_c")}/><span>°C</span><Help>{d ? "Vejrudsigtens højeste temperatur næste døgn." : "The forecast high for the next 24 hours."}</Help></label>
      <label>{d ? "Eller varm dag i dag fra" : "Or hot day today from"}<input type="number" min="15" max="40" step="0.5" value={n(form.night_cool_today_c, 22)} onChange={numKey("night_cool_today_c")}/><span>°C</span><Help>{d ? "Dagens højeste temperatur (fra Home Assistant, ellers målt ude). Huskes resten af natten." : "Today's high; remembered for the rest of the night."}</Help></label>
      <label>{d ? "Kølesæson over" : "Cooling season above"}<input type="number" min="0" max="25" step="0.5" value={n(form.night_cool_season_c, 12)} onChange={numKey("night_cool_season_c")}/><span>°C</span><Help>{d ? "Udetemperaturens gennemsnit over de seneste 5 døgn." : "Outdoor mean of the last 5 days."}</Help></label>
      <label>{d ? "Ude køligere end inde med mindst" : "Outdoor cooler than indoor by"}<input type="number" min="0.5" max="8" step="0.5" value={n(form.night_cool_delta_k, 2)} onChange={numKey("night_cool_delta_k")}/><span>K</span></label>
      <label>{d ? "Rummene mindst" : "Rooms at least"}<input type="number" min="16" max="26" step="0.5" value={n(form.night_cool_min_indoor_c, 20.5)} onChange={numKey("night_cool_min_indoor_c")}/><span>°C</span><Help>{d ? "Køler ikke rummene under dette (gennemsnit af rummene der styrer)." : "Never cools the rooms below this."}</Help></label>
      <label>{d ? "Trin under natkøling" : "Level during night cooling"}<input type="number" min="1" max={maxLevel} value={n(form.night_cool_level, 4)} onChange={numKey("night_cool_level")}/><Help>{d ? "Er Natdrift slået til, går natkølingen aldrig over Natdriftens Maks. ved dårlig luft." : "With night mode on, never above its maximum with poor air."}</Help></label>
    </div>
    <div className="settings-summary">
      <span>{d ? "Status" : "Status"}<strong>{controller.night_cool_active === true ? (d ? "Køler" : "Cooling") : controller.night_cool_enabled === true ? (d ? "Venter" : "Waiting") : (d ? "Slået fra" : "Off")}</strong></span>
      <span>{d ? "5-døgns middel ude" : "5-day outdoor mean"}<strong>{deg(controller.night_cool_season_mean)}</strong></span>
      <span>{d ? "Højeste i morgen / i dag" : "High tomorrow / today"}<strong>{deg(controller.night_cool_forecast_high, "", 0)} / {deg(controller.night_cool_today_high, " °C", 0)}</strong></span>
      <span>{d ? "Ude / inde" : "Outdoor / indoor"}<strong>{deg(controller.night_cool_outdoor, "")} / {deg(controller.night_cool_indoor)}</strong></span>
    </div>
    <p className="settings-help">{nightCoolText(controller) ?? s(controller.night_cool_reason)}</p>
  </Card>;
}
