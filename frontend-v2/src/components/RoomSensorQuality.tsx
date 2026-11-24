import { KIND_LABELS, type SensorKind } from "../lib/rooms";
export type RoomQuality = Record<string, { state: string; age_seconds: number | null; max_age_seconds: number; used: boolean }>;
export function roomQuality(state: Record<string, unknown>, name: string): RoomQuality {
  const all = (state.smart_room_quality ?? {}) as Record<string, RoomQuality>;
  const key = Object.keys(all).find(key => key.toLocaleLowerCase("da") === name.toLocaleLowerCase("da"));
  return key ? all[key] : {};
}
export function qualityLabel(item: RoomQuality[string]): string {
  const age = item.age_seconds === null ? "ukendt alder" : `${Math.floor(item.age_seconds / 60)} min gammel`;
  if (item.state === "connection_stale") return "HA-forbindelse for gammel";
  if (item.state === "stale") return `For gammel · ${age}`;
  if (item.state === "unknown_age") return item.used ? "Ukendt alder · ældre HA-klient" : "Ukendt alder · bruges ikke";
  return `${age} · udløber efter ${item.max_age_seconds / 3600} timer`;
}
export function RoomSensorQuality({ state, name }: { state: Record<string, unknown>; name: string }) {
  const problems = Object.entries(roomQuality(state, name)).filter(([, q]) => q.state !== "fresh" && q.state !== "unavailable");
  if (!problems.length) return null;
  return <span className="room-quality" aria-label={`${name} målingernes kvalitet`}>{problems.map(([kind, q]) =>
    <span key={kind} className="room-chip room-chip-stale">{KIND_LABELS[kind as SensorKind] ?? kind}: {qualityLabel(q)}</span>)}</span>;
}
