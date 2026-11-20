// Afterheat frost lock ("Frostlås"). The water coil sits in a cold attic, so
// while it freezes the Pi keeps the afterheat on at a minimum setpoint and
// rejects attempts to switch it off. The Pi is the source of truth; the WebUI
// only reflects the snapshot keys and stops the user from drafting values the
// Pi would reject anyway. Older firmware without the keys keeps the old UI.

export type AfterheatValue = number | "off";

/** Settings the frost lock adds to the advanced controller settings. */
export const FROST_LOCK_SETTINGS = ["frost_lock_enabled", "frost_lock_outdoor_c", "frost_lock_coil_c", "frost_lock_min_setpoint_c"] as const;

export const FROST_LOCK_DEFAULT_FLOOR = 18;

export interface FrostLock {
  /** The controller reports the frost lock at all (newer firmware). */
  known: boolean;
  locked: boolean;
  /** Danish explanation from the Pi, e.g. "Frost: ude -1,6 °C (grænse 3 °C)". */
  reason: string | null;
  /** Lowest afterheat setpoint allowed while locked. */
  floor: number;
  /** False when the Pi is not bus master, so HCP4 could still switch it off. */
  enforced: boolean;
  /** The frost lock feature is switched on in the settings. */
  enabled: boolean;
}

function finite(value: unknown): number | null {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function readFrostLock(controller: Record<string, unknown>): FrostLock {
  const known = typeof controller.afterheat_frost_lock === "boolean";
  const locked = controller.afterheat_frost_lock === true;
  const reason = typeof controller.afterheat_frost_lock_reason === "string" && controller.afterheat_frost_lock_reason.trim() !== ""
    ? controller.afterheat_frost_lock_reason
    : null;
  const floor = finite(controller.afterheat_frost_lock_floor) ?? finite(controller.frost_lock_min_setpoint_c) ?? FROST_LOCK_DEFAULT_FLOOR;
  return {
    known,
    locked,
    reason,
    floor,
    enforced: controller.afterheat_frost_lock_enforced !== false,
    enabled: controller.frost_lock_enabled !== false,
  };
}

/** The value would switch the afterheat off or put it under the frost floor. */
export function frostLockRefuses(lock: Pick<FrostLock, "locked" | "floor">, value: AfterheatValue): boolean {
  return lock.locked && (value === "off" || value < lock.floor);
}

/** Lowest whole-degree setpoint the thermostat can pick while locked. */
export function frostFloorStep(floor: number): number {
  return Math.ceil(floor - 1e-9);
}

/** Format the floor the way the Pi does in its messages: 18, 18,5. */
export function formatFloor(floor: number): string {
  return floor.toLocaleString("da-DK", { maximumFractionDigits: 1 });
}

export const FROST_OFF_MESSAGE = "Frostsikring: eftervarmen kan ikke slukkes, mens det fryser.";
export const frostFloorMessage = (floor: number) => `Frostsikring: eftervarmen kan ikke sættes under ${formatFloor(floor)} °C, mens det fryser.`;
export const FROST_COOL_MESSAGE = "Frostsikring: Køl kan ikke startes, mens det fryser.";
export const FROST_COOL_HINT = "Spærret: frostsikring";

/** Status line for the technique page and the settings. */
export function frostLockStatus(lock: FrostLock): string {
  if (!lock.known) return "—";
  if (!lock.enabled) return "Slået fra";
  if (!lock.locked) return "Ikke aktiv";
  const base = `Aktiv · min. ${formatFloor(lock.floor)} °C${lock.reason ? ` · ${lock.reason}` : ""}`;
  return lock.enforced ? base : `${base} · ikke håndhævet (Pi er ikke busmaster)`;
}
