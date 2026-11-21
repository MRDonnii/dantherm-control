// The Pi's newer automatic rules as short Danish status lines for Oversigt and
// Teknik: the afterheat target from the rooms, the night cooling from the
// forecast and the ventilation after occupancy. The Pi decides; this only reads
// the snapshot. Older firmware without the keys gives no lines.

type Data = Record<string, unknown>;

export const ROOM_TARGET_SETTINGS = [
  "afterheat_room_enabled", "afterheat_room_source", "afterheat_room_target", "afterheat_room_gain",
  "afterheat_room_min", "afterheat_room_max", "afterheat_room_step_minutes", "afterheat_room_integral_minutes",
  "afterheat_room_max_step", "afterheat_room_band_k", "afterheat_room_offset_k", "afterheat_room_weighted",
] as const;
export const NIGHT_COOL_SETTINGS = [
  "night_cool_enabled", "night_cool_start", "night_cool_end", "night_cool_forecast_c", "night_cool_today_c",
  "night_cool_season_c", "night_cool_delta_k", "night_cool_min_indoor_c", "night_cool_level",
] as const;
export const PRESENCE_SETTINGS = [
  "presence_away_enabled", "presence_away_level", "presence_busy_enabled", "presence_busy_rooms",
  "presence_on_minutes", "presence_off_minutes",
] as const;

function finite(value: unknown): number | null {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function da(value: number, digits = 1): string {
  return value.toLocaleString("da-DK", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** "Mål fra rummene: 22,4 °C i rummene → indblæsning 21,6 °C" (with the duct offset when set), or null when off. */
export function roomTargetText(c: Data): string | null {
  if (c.afterheat_room_enabled !== true) return null;
  const room = finite(c.afterheat_room_temperature);
  const rooms = finite(c.afterheat_room_rooms_target);
  const supply = finite(c.afterheat_room_supply_target);
  if (room === null || rooms === null) return "Mål fra rummene: venter på rumtemperatur (fast setpunkt bruges)";
  const offset = finite(c.afterheat_room_offset_k) ?? 0;
  let text = `Mål fra rummene: ${da(room)} °C i rummene → indblæsning ${da(rooms)} °C`;
  if (offset && supply !== null) text += ` (${offset > 0 ? "+" : "−"}${da(Math.abs(offset))} K kanaltab = ${da(supply)} °C ved anlægget)`;
  const state = String(c.afterheat_room_state ?? "");
  if (state === "saturated") text += " · eftervarmen kan ikke følge med";
  else if (state === "at_limit") text += " · ved grænsen";
  else if (state === "locked") text += " · holdes";
  return text;
}

/** The night cooling line: the Pi's reason while active or waiting in the window, else null. */
export function nightCoolText(c: Data): string | null {
  const state = String(c.night_cool_state ?? "");
  if (!state || state === "disabled" || state === "outside_window") return null;
  const reason = typeof c.night_cool_reason === "string" ? c.night_cool_reason : "";
  return reason || null;
}

const HOME_LABEL: Record<string, string> = { home: "hjemme", away: "ingen hjemme" };

/** "3 rum i brug · hjemme (4 personer hjemme)" and the rule that acts, or null when both rules are off. */
export function presenceText(c: Data): string | null {
  const state = String(c.presence_state ?? "");
  if (!state || state === "disabled") return null;
  const count = finite(c.occupied_count) ?? 0;
  const home = HOME_LABEL[String(c.home_state ?? "")] ?? "hjemme/ude ukendt";
  const why = typeof c.home_reason === "string" && c.home_reason ? ` (${c.home_reason})` : "";
  const reason = typeof c.presence_reason === "string" && c.presence_reason ? ` · ${c.presence_reason}` : "";
  return `Tilstedeværelse: ${count} rum i brug · ${home}${why}${reason}`;
}

/** All active automation lines, for the "Aktiv beslutning" box. */
export function automationLines(c: Data): string[] {
  return [roomTargetText(c), nightCoolText(c), presenceText(c)].filter((line): line is string => Boolean(line));
}

export const SOURCE_LABELS: Record<string, string> = {
  night_cooling: "natkøling", presence_away: "ingen hjemme", presence_busy: "mange rum i brug",
};
