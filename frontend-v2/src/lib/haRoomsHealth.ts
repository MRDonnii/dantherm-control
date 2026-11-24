/** Combined status of the HA room measurements (data_health.sensors.ha_rooms).
 * The connection and the age of the measurements are separate: HA can resend an
 * old reading every minute. Older gateways send only `fresh`. */
export type HaRoomsHealth = {
  state?: string; fresh?: boolean | null; connection_online?: boolean;
  fresh_count?: number; stale_count?: number; unknown_age_count?: number;
};
export type HaRoomsStatus = { value: string; tone: "ok" | "warn" | "neutral" };

const count = (v: unknown) => (typeof v === "number" && Number.isFinite(v) && v > 0 ? v : 0);

export function haRoomsStatus(item: HaRoomsHealth | null | undefined): HaRoomsStatus {
  const h = item ?? {};
  if (!h.state) return h.fresh === true ? { value: "Frisk", tone: "ok" } : { value: "Mangler eller for gammel", tone: "warn" };
  if (h.state === "offline") return { value: "Ingen forbindelse", tone: "warn" };
  if (h.state === "no_rooms") return { value: "Ingen HA-rum", tone: "neutral" };
  if (h.state === "no_measurements") return { value: "Ingen målinger", tone: "neutral" };
  const fresh = count(h.fresh_count), stale = count(h.stale_count), unknown = count(h.unknown_age_count);
  const parts = [
    fresh ? `${fresh} ${fresh === 1 ? "frisk" : "friske"}` : "",
    stale ? `${stale} for ${stale === 1 ? "gammel" : "gamle"}` : "",
    unknown ? `${unknown} ukendt alder` : "",
  ].filter(Boolean);
  const value = parts.join(" · ") || "Ingen målinger";
  if (h.state === "fresh") return { value, tone: "ok" };
  if (h.state === "unknown") return { value, tone: "neutral" };
  return { value, tone: "warn" };
}
