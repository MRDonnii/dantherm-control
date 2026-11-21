/**
 * Room setup for the page "Rum": which Home Assistant sensors belong to each
 * room and how the room counts in the control. The Pi validates the same
 * rules (gateway/room_setup.py); this file keeps the page's own logic testable.
 */

export const SENSOR_KINDS = ["temperature", "humidity", "co2", "pm25"] as const;
export type SensorKind = typeof SENSOR_KINDS[number];

export const ROOM_TYPES = ["normal", "bedroom", "bathroom", "kitchen", "source", "outdoor", "extract", "excluded"] as const;
export type RoomType = typeof ROOM_TYPES[number];
export const PRIORITIES = ["auto", "low", "normal", "high", "critical"] as const;
export type Priority = typeof PRIORITIES[number];

export const MAX_ROOMS = 32;

export const TYPE_LABELS: Record<RoomType, string> = {
  normal: "Normalt rum", bedroom: "Soverum", bathroom: "Badeværelse", kitchen: "Køkken",
  source: "Kilde (brændeovn)", outdoor: "Ude", extract: "Kun kanalmåling (udsugning)", excluded: "Ikke ventileret",
};
export const TYPE_HELP: Record<RoomType, string> = {
  normal: "Fugt, CO₂ og temperatur tæller med i styringen og gennemsnittene.",
  bedroom: "Som et normalt rum. Brug prioritet Høj, hvis luften her skal veje tungere om natten.",
  bathroom: "Udtørring efter bad. Tæller ikke med i husets gennemsnit for fugt og temperatur.",
  kitchen: "Som et normalt rum; hurtige fugtstigninger fra madlavning løfter ventilationen.",
  source: "Kun måling, fx brændeovnens føler til den automatiske pejsefunktion.",
  outdoor: "Kun måling af udeluften, fx til fugtstyring efter absolut vandindhold.",
  extract: "Kun til beregningen af udsugningskanalens afkøling (gennemsnittet af rummene med udsugning). Rummet er IKKE med i styringen: dets fugt, CO₂ og temperatur løfter ikke ventilationen og tæller ikke i husets gennemsnit. Et almindeligt bryggers eller toilet skal være Normalt rum eller Badeværelse.",
  excluded: "Ingen ventilation her (fx garagen). Rummet ignoreres overalt.",
};
export const PRIORITY_LABELS: Record<Priority, string> = { auto: "Auto", low: "Lav", normal: "Normal", high: "Høj", critical: "Kritisk" };
export const KIND_LABELS: Record<SensorKind, string> = { temperature: "Temperatur", humidity: "Fugt (RH)", co2: "CO₂", pm25: "PM2.5" };
export const KIND_UNITS: Record<SensorKind, string> = { temperature: "°C", humidity: "%", co2: "ppm", pm25: "µg/m³" };

export interface Room {
  id: string;
  name: string;
  area_id: string | null;
  area_name?: string | null;
  type: RoomType;
  priority: Priority;
  control: boolean;
  enabled: boolean;
  unit_sensor: boolean;
  sensors: Record<SensorKind, string | null>;
  rh_setpoint?: number;
  rh_hysteresis?: number;
  max_level?: number;
}

export interface CatalogSensor { entity_id: string; name: string; kind: SensorKind; unit?: string }
export interface CatalogArea { area_id: string; area_name: string; sensors: CatalogSensor[] }
export interface RoomsView {
  rooms: Room[];
  migrated?: boolean;
  catalog: CatalogArea[];
  catalog_age_seconds: number | null;
}

export const INDOOR: readonly RoomType[] = ["normal", "bedroom", "bathroom", "kitchen"];
export const isIndoor = (type: RoomType) => INDOOR.includes(type);

export function slug(text: string): string {
  const folded = text.trim().toLowerCase()
    .replace(/æ/g, "ae").replace(/ø/g, "oe").replace(/å/g, "aa")
    .replace(/[üö]/g, m => (m === "ü" ? "u" : "o")).replace(/ä/g, "a").replace(/é/g, "e");
  return (folded.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "rum").slice(0, 40);
}

/** A sensible room type from a room or area name; mirrors guess_type on the Pi. */
export function guessType(name: string): RoomType {
  const folded = name.toLocaleLowerCase("da");
  const words = folded.split(/[^a-z0-9æøå]+/);
  if (folded.includes("garage") || folded.includes("carport")) return "excluded";
  if (["brændeovn", "pejs", "stove", "fireplace"].some(m => folded.includes(m))) return "source";
  if (folded.includes("udsug") || folded.includes("extract")) return "extract";
  if (words.some(w => ["ude", "udenfor", "udeluft", "outdoor", "outside", "have", "terrasse", "terrassen"].includes(w)) || folded.startsWith("udeluft")
    || ["indkørsel", "indkorsel", "driveway", "altan", "balkon", "balcony"].some(m => folded.includes(m))) return "outdoor";
  if (["bad", "bath", "brus", "shower", "toilet"].some(m => folded.includes(m)) || words.includes("wc")) return "bathroom";
  if (folded.includes("køkken") || folded.includes("kokken") || folded.includes("kitchen")) return "kitchen";
  if (folded.includes("sove") || folded.includes("bedroom")) return "bedroom";
  return "normal";
}

export function emptySensors(): Record<SensorKind, string | null> {
  return { temperature: null, humidity: null, co2: null, pm25: null };
}

/** The area's sensors are sorted best first by Home Assistant, so the first one is the automatic choice. */
export function firstSensor(area: CatalogArea | undefined, kind: SensorKind): string | null {
  return area?.sensors.find(sensor => sensor.kind === kind)?.entity_id ?? null;
}

export function autoSensors(area: CatalogArea | undefined): Record<SensorKind, string | null> {
  return Object.fromEntries(SENSOR_KINDS.map(kind => [kind, firstSensor(area, kind)])) as Record<SensorKind, string | null>;
}

function uniqueId(base: string, taken: Set<string>): string {
  let id = base;
  for (let n = 2; taken.has(id); n += 1) id = `${base.slice(0, 36)}_${n}`;
  return id;
}

export function blankRoom(rooms: Room[], name = "Nyt rum"): Room {
  let label = name;
  const names = new Set(rooms.map(room => room.name.toLocaleLowerCase("da")));
  for (let n = 2; names.has(label.toLocaleLowerCase("da")); n += 1) label = `${name} ${n}`;
  return {
    id: uniqueId(slug(label), new Set(rooms.map(room => room.id))), name: label, area_id: null, area_name: null,
    type: "normal", priority: "auto", control: true, enabled: true, unit_sensor: false, sensors: emptySensors(),
  };
}

export function roomFromArea(area: CatalogArea, rooms: Room[]): Room {
  const type = guessType(area.area_name);
  return {
    id: uniqueId(slug(area.area_name), new Set(rooms.map(room => room.id))),
    name: area.area_name, area_id: area.area_id, area_name: area.area_name,
    type, priority: "auto", control: isIndoor(type), enabled: true, unit_sensor: false,
    sensors: autoSensors(area),
  };
}

const hasClimate = (area: CatalogArea) => area.sensors.some(sensor => sensor.kind === "temperature" || sensor.kind === "humidity");

/**
 * "Tilføj alle rum fra HA": every area with a temperature or humidity sensor
 * that is not set up yet (same area or same name). Garages come in as not
 * ventilated; the unit's own sensor goes to the dining room if no room has it.
 */
/** HA sends the weather service's sensors (DMI) as this virtual area: they lead the list for outdoor rooms and never become a room. */
export const WEATHER_AREA_ID = "_weather";

export function addAllFromCatalog(rooms: Room[], catalog: CatalogArea[]): { rooms: Room[]; added: string[] } {
  const next = [...rooms];
  const added: string[] = [];
  const usedAreas = new Set(rooms.map(room => room.area_id).filter(Boolean));
  const usedNames = new Set(rooms.map(room => room.name.toLocaleLowerCase("da")));
  for (const area of catalog) {
    if (next.length >= MAX_ROOMS) break;
    if (area.area_id === WEATHER_AREA_ID || !hasClimate(area) || usedAreas.has(area.area_id) || usedNames.has(area.area_name.toLocaleLowerCase("da"))) continue;
    const room = roomFromArea(area, next);
    next.push(room);
    added.push(room.name);
    usedAreas.add(area.area_id);
  }
  if (!next.some(room => room.unit_sensor)) {
    const dining = next.find(room => isIndoor(room.type) && /spisestue/i.test(`${room.area_id ?? ""} ${room.name}`));
    if (dining) dining.unit_sensor = true;
  }
  return { rooms: next, added };
}

/** Pick another area: its sensors become the automatic choice for every kind. */
export function withArea(room: Room, area: CatalogArea | undefined): Room {
  return { ...room, area_id: area?.area_id ?? null, area_name: area?.area_name ?? null, sensors: area ? autoSensors(area) : room.sensors };
}

export function withType(room: Room, type: RoomType): Room {
  const next: Room = { ...room, type, control: isIndoor(type) ? (isIndoor(room.type) ? room.control : true) : false };
  if (!isIndoor(type)) next.unit_sensor = false;
  if (type !== "bathroom") { delete next.rh_setpoint; delete next.rh_hysteresis; delete next.max_level; }
  return next;
}

/** Only one room can hold the unit's own sensor. */
export function withUnitSensor(rooms: Room[], id: string, on: boolean): Room[] {
  return rooms.map(room => ({ ...room, unit_sensor: room.id === id ? on : on ? false : room.unit_sensor }));
}

/** Sensor choices for one kind, the room's own area first, then the rest by area. */
export function sensorGroups(catalog: CatalogArea[], kind: SensorKind, areaId: string | null, outdoor = false): { label: string; sensors: CatalogSensor[] }[] {
  const groups = catalog
    .map(area => ({ id: area.area_id, label: area.area_name, sensors: area.sensors.filter(sensor => sensor.kind === kind) }))
    .filter(group => group.sensors.length);
  const rank = (id: string) => (outdoor && id === WEATHER_AREA_ID ? 0 : id === areaId ? 1 : !outdoor && id === WEATHER_AREA_ID ? 3 : 2);
  groups.sort((a, b) => rank(a.id) - rank(b.id) || a.label.localeCompare(b.label, "da"));
  return groups.map(({ label, sensors }) => ({ label, sensors }));
}

export function sensorName(catalog: CatalogArea[], entityId: string | null): string | null {
  if (!entityId) return null;
  for (const area of catalog) for (const sensor of area.sensors) if (sensor.entity_id === entityId) return sensor.name;
  return null;
}

export type LiveValues = Partial<Record<SensorKind, number>> & { unit_sensor?: boolean };

/** The values the Pi uses for the room right now (smart_rooms in the controller state). */
export function liveValues(state: Record<string, unknown>, name: string): LiveValues | null {
  const rooms = state.smart_rooms;
  if (!rooms || typeof rooms !== "object") return null;
  const key = Object.keys(rooms).find(item => item.toLocaleLowerCase("da") === name.toLocaleLowerCase("da"));
  if (!key) return null;
  const raw = (rooms as Record<string, Record<string, unknown>>)[key] ?? {};
  const values: LiveValues = {};
  for (const kind of SENSOR_KINDS) if (typeof raw[kind] === "number") values[kind] = raw[kind] as number;
  if (raw.unit_sensor === true) values.unit_sensor = true;
  return values;
}

/** Problems that stop the list from being saved (the Pi checks the same). */
export function roomProblems(rooms: Room[]): string[] {
  const problems: string[] = [];
  const names = new Map<string, number>();
  for (const room of rooms) {
    const name = room.name.trim();
    if (!name) problems.push("Alle rum skal have et navn.");
    const key = name.toLocaleLowerCase("da");
    names.set(key, (names.get(key) ?? 0) + 1);
  }
  for (const [name, count] of names) if (name && count > 1) problems.push(`Navnet "${rooms.find(r => r.name.trim().toLocaleLowerCase("da") === name)?.name.trim()}" bruges af flere rum.`);
  if (rooms.filter(room => room.unit_sensor).length > 1) problems.push("Anlæggets egen føler kan kun sidde i ét rum.");
  if (rooms.length > MAX_ROOMS) problems.push(`Højst ${MAX_ROOMS} rum.`);
  return [...new Set(problems)];
}

/** The list as the Pi stores it: trimmed names, empty limits left out. */
export function toPayload(rooms: Room[]): Room[] {
  return rooms.map(room => {
    const out: Room = { ...room, name: room.name.trim() };
    for (const key of ["rh_setpoint", "rh_hysteresis", "max_level"] as const) {
      if (out[key] === undefined || out[key] === null || Number.isNaN(out[key])) delete out[key];
    }
    return out;
  });
}
