/** "Luften i rummene": every room's air values against the limits that raise the fan. */
export type Metric = "co2" | "humidity" | "pm25";

export interface MetricRow {
  metric: Metric;
  label: string;
  unit: string;
  value: number;
  limit: number;
  /** value / limit; 1 or more means over the limit. */
  share: number;
}

export interface RoomAir {
  name: string;
  rows: MetricRow[];
  /** Closest to a limit, as a share of that limit (0 when nothing is measured). */
  closest: number;
  over: boolean;
  /** The room the controller names as the one that decides right now. */
  decides: boolean;
}

const num = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;

function isBathroom(name: string, values: Record<string, unknown>): boolean {
  const type = String(values.room_type ?? "auto").toLowerCase();
  if (type === "bathroom") return true;
  if (type === "normal") return false;
  const folded = name.toLocaleLowerCase("da");
  return ["bad", "bath", "shower", "brus"].some(marker => folded.includes(marker));
}

const LABELS: Record<Metric, [string, string]> = { co2: ["CO₂", " ppm"], humidity: ["Fugt", " %"], pm25: ["Støv", " µg/m³"] };

/**
 * Rooms from the controller state (smart_rooms) with the limits it uses: the CO₂ limit,
 * the humidity limit (bathrooms and single rooms may have their own) and the dust limit.
 * Rooms that are only measured (control off) are listed too, but never "decide".
 */
export function roomAirRows(state: Record<string, unknown>): RoomAir[] {
  const rooms = state.smart_rooms;
  if (!rooms || typeof rooms !== "object") return [];
  const co2Limit = num(state.co2_setpoint);
  const rhLimit = num(state.rh_setpoint);
  const bathroomRh = num(state.bathroom_rh_setpoint);
  const pmLimit = state.pm25_enabled === false ? null : num(state.pm25_setpoint);
  const ignored = Array.isArray(state.pm25_ignored_rooms) ? state.pm25_ignored_rooms.map(String) : [];
  const decider = typeof state.smart_controlling_room === "string" ? state.smart_controlling_room : null;
  const result: RoomAir[] = [];
  for (const [name, raw] of Object.entries(rooms as Record<string, Record<string, unknown>>)) {
    if (!raw || typeof raw !== "object" || raw.enabled === false) continue;
    const rows: MetricRow[] = [];
    const add = (metric: Metric, value: number | null, limit: number | null) => {
      if (value === null || limit === null || limit <= 0) return;
      rows.push({ metric, label: LABELS[metric][0], unit: LABELS[metric][1], value, limit, share: value / limit });
    };
    add("co2", num(raw.co2), co2Limit);
    add("humidity", num(raw.humidity), num(raw.rh_setpoint) ?? (isBathroom(name, raw) ? bathroomRh : rhLimit));
    add("pm25", num(raw.pm25), ignored.includes(name) ? null : pmLimit);
    if (rows.length === 0) continue;
    const closest = Math.max(...rows.map(row => row.share));
    result.push({ name, rows, closest, over: closest >= 1, decides: raw.control !== false && decider !== null && decider === name });
  }
  // The deciding room first, then the rooms closest to a limit.
  return result.sort((a, b) => Number(b.decides) - Number(a.decides) || b.closest - a.closest || a.name.localeCompare(b.name, "da"));
}

/** One plain sentence on how the air is, for the card's top. */
export function roomAirSentence(state: Record<string, unknown>, rooms: RoomAir[]): string {
  if (rooms.length === 0) return "Ingen rum sender luftmålinger lige nu.";
  const level = num(state.effective_level);
  const normal = num(state.effective_normal_level);
  const over = rooms.filter(room => room.over);
  if (over.length > 0) {
    const first = over[0];
    const row = [...first.rows].sort((a, b) => b.share - a.share)[0];
    return `${first.name} ligger over grænsen (${word(row.label)} ${fmt(row.value)}${row.unit}, grænsen er ${fmt(row.limit)}). Blæseren sættes op.`;
  }
  const near = rooms[0].decides ? rooms[0] : [...rooms].sort((a, b) => b.closest - a.closest)[0];
  const row = [...near.rows].sort((a, b) => b.share - a.share)[0];
  const calm = level !== null && normal !== null && level === normal ? `Blæseren kører på normaltrin ${normal}. ` : "";
  return `${calm}Luften er god i alle rum. Tættest på grænsen er ${near.name} (${word(row.label)} ${fmt(row.value)}${row.unit} af ${fmt(row.limit)}).`;
}

const word = (label: string): string => label === "CO₂" ? label : label.toLocaleLowerCase("da");
export const fmt = (value: number): string => value.toLocaleString("da-DK", { maximumFractionDigits: 1 });
