/** Plain-Danish summary of what currently decides the unit's ventilation. */
export interface ControlSummary {
  title: string;
  level: number | null;
  reason: string;
  footer: string;
  tone: "normal" | "boost" | "reduced" | "paused";
}

const SOURCE_TITLES: Record<string, string> = {
  local_auto: "Automatik i anlægget",
  local_fallback: "Automatik i anlægget · Home Assistant mangler",
  ha_smart: "Automatik med rummene",
  manual: "Manuel",
  night: "Natsænkning",
  night_air_quality: "Nat · luftkvalitet",
  vacation: "Ferie",
  quick_boost: "Kort boost",
  free_cooling: "Frikøling",
  dry_protection: "Tør luft-beskyttelse",
};

const ROOM_UNIT = "HCH5 / lokale sensorer";
const place = (room: string) => room.trim() === ROOM_UNIT ? "ved anlæggets egen føler" : `i ${room.trim()}`;

// Controller reasons are compact; say them the way a person would.
const PHRASES: [RegExp, (...groups: string[]) => string][] = [
  [/RH rise (.+?) \+([\d.]+)%\/10m(?: \(\w+\))?/g, (room, rise) => `Fugten stiger ${rise.replace(".", ",")} % på 10 min ${place(room)}`],
  [/(?:Badeværelse )?RH (.+?) ([\d.]+)% \/ ([\d.]+)%(?: \(\w+\))?/g, (room, value, limit) => `Fugt ${value.replace(".", ",")} % ${place(room)} (grænse ${limit} %)`],
  [/CO2 (.+?) (\d+)(?: \(\w+\))?(?=;|$)/g, (room, value) => `CO₂ ${value} ppm ${place(room)}`],
  [/PM2\.5 (.+?) ([\d.]+) µg\/m³(?: \(\w+\))?(?=;|$)/g, (room, value) => `Støv ${value.replace(".", ",")} µg/m³ ${place(room)}`],
];

const HOLD_WORDS: [RegExp, string][] = [
  [/Downshift delay/g, "venter før nedgang"],
  [/Boost hold/g, "holder boost"],
  [/Hysteresis/g, "holder igen et øjeblik"],
  [/RH\/CO2 normal/g, "fugt og CO₂ normal"],
  [/HA offline/g, "HA offline"],
  [/\(auto\)|\(low\)|\(normal\)|\(high\)|\(critical\)/g, ""],
];

export function tidy(reason: string) {
  let text = reason;
  for (const [pattern, phrase] of PHRASES) text = text.replace(pattern, (_match, ...groups: string[]) => phrase(...groups));
  for (const [pattern, replacement] of HOLD_WORDS) text = text.replace(pattern, replacement);
  return text.replace(/\s+;/g, ";").replace(/\s{2,}/g, " ").trim();
}

function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * When the smart control sits on the normal step, no room decides anything: the
 * controller still names the room that is closest to its limit, which reads
 * like a cause. Say instead that the air is good, and how close it is.
 */
function calmAirText(state: Record<string, unknown>, level: number): string | null {
  const normal = num(state.effective_normal_level);
  if (normal === null || level !== normal) return null;
  const co2 = num(state.smart_max_co2);
  const limit = num(state.co2_setpoint);
  if (co2 !== null && limit !== null && co2 > limit) return null; // over the limit: the stored reason is the true one
  const base = `Normaltrin ${level} · luften er god i alle rum`;
  if (co2 === null || limit === null) return base;
  const room = typeof state.smart_max_co2_room === "string" && state.smart_max_co2_room.trim() !== "" ? state.smart_max_co2_room.trim() : null;
  const where = room === null ? "" : room === ROOM_UNIT ? " ved anlæggets egen føler" : ` i ${room}`;
  return `${base} (højest CO₂ ${Math.round(co2)} ppm${where}, grænsen er ${Math.round(limit)})`;
}

export function describeControl(state: Record<string, unknown>): ControlSummary {
  const master = String(state.active_master ?? "");
  const levelValue = Number(state.effective_level);
  const level = Number.isFinite(levelValue) && levelValue > 0 ? levelValue : null;
  const source = String(state.effective_source ?? "");
  let reason = tidy(String(state.effective_reason ?? ""));
  const masterText = master === "pi" ? "Pi styrer" : master === "hcp4" ? "HCP4 styrer" : "Afventer master";

  if (master === "hcp4") {
    return { title: "HCP4-panelet styrer", level: null, reason: "Pi'en venter og skriver ikke til anlægget, så længe HCP4 er aktivt på bussen.", footer: masterText, tone: "paused" };
  }
  if (state.fireplace === true) {
    const auto = state.fireplace_auto_active === true;
    return {
      title: "Pejsefunktion",
      level,
      reason: auto ? "Overtryk mens der fyres, holdt af brændeovnens føler eller pejse-signalet. Bypass er lukket." : "Overtryk i pejsetid. Bypass er lukket.",
      footer: masterText,
      tone: "boost",
    };
  }
  if (source === "ha_smart" && level !== null) reason = calmAirText(state, level) ?? reason;
  const title = SOURCE_TITLES[source] ?? (source ? source.replaceAll("_", " ") : "Afventer controller");
  const tone = source === "quick_boost" || (level !== null && level >= 5) ? "boost"
    : ["night", "night_air_quality", "vacation", "dry_protection"].includes(source) ? "reduced" : "normal";
  return { title, level, reason: reason || "Afventer controllerens beslutning", footer: masterText, tone };
}
