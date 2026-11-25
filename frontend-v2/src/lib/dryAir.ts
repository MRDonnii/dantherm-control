/** Dynamic dry-air protection: the keys the settings page edits and small read helpers. */
export const DRY_AIR_SETTINGS = [
  "dry_dynamic_enabled", "dry_tier1_rh", "dry_tier1_level", "dry_tier2_rh", "dry_tier2_level",
  "dry_dynamic_rh_hysteresis", "dry_ah_margin_gm3", "dry_ah_hysteresis_gm3", "dry_min_level",
  "dry_co2_soft_enabled", "dry_co2_release_ppm",
] as const;

type Data = Record<string, unknown>;

const num = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;

/** Estimated supply airflow per level from the gateway's airflow plan (skøn), or null when unknown. */
export function levelAirflow(controller: Data, level: number): number | null {
  const plan = controller.airflow_plan as { levels?: Record<string, { supply_m3h?: unknown }> } | undefined;
  return num(plan?.levels?.[String(level)]?.supply_m3h);
}

/**
 * The level a tier really caps at: never under the house floor (dry_min_level,
 * local_min_level and the house sizing minimum). The gateway reports its floor
 * as dry_floor_level; without it the form's two minimums are used.
 */
export function effectiveTierLevel(form: Data, controller: Data, level: number): { level: number; floor: number; raised: boolean } {
  const floor = num(controller.dry_floor_level) ?? Math.max(num(form.dry_min_level) ?? 1, num(form.local_min_level) ?? 1);
  const wanted = Math.max(floor, num(form.dry_min_level) ?? 1);
  return { level: Math.max(level, wanted), floor: wanted, raised: level < wanted };
}

const STATE_DA: Record<string, string> = {
  active: "Aktiv", held: "Aktiv (holder)", standby: "Ikke aktiv", not_drying: "Ikke aktiv: udeluften tørrer ikke",
  humidity_demand: "Slået fra: fugt", co2_high: "Slået fra: CO₂", co2_wait: "Venter på CO₂", waiting: "Starter snart",
  sensor_missing: "Slået fra: mangler data", disabled: "Slået fra",
};
const STATE_EN: Record<string, string> = {
  active: "Active", held: "Active (holding)", standby: "Not active", not_drying: "Not active: outdoor air does not dry",
  humidity_demand: "Off: humidity", co2_high: "Off: CO₂", co2_wait: "Waiting for CO₂", waiting: "Starting soon",
  sensor_missing: "Off: missing data", disabled: "Off",
};

export function dryStateText(controller: Data, lang: "da" | "en"): string {
  const state = String(controller.dry_protection_state ?? "disabled");
  return (lang === "da" ? STATE_DA : STATE_EN)[state] ?? state;
}

export function outdoorSourceText(source: unknown, lang: "da" | "en"): string {
  if (source === "udeluft") return lang === "da" ? "Udeluft-rummet" : "The outdoor room";
  if (source === "t1_weather") return lang === "da" ? "T1 og vejrets fugt" : "T1 and the weather humidity";
  return lang === "da" ? "Ingen friske data" : "No fresh data";
}
