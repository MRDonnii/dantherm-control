import { describe, expect, it } from "vitest";
import { FROST_LOCK_SETTINGS, frostFloorMessage, frostLockRefuses, frostLockStatus, readFrostLock } from "./frostLock";

describe("readFrostLock", () => {
  it("treats firmware without the keys as unknown and unlocked", () => {
    const lock = readFrostLock({});
    expect(lock).toMatchObject({ known: false, locked: false, reason: null, floor: 18, enforced: true, enabled: true });
    expect(frostLockStatus(lock)).toBe("—");
  });

  it("reads the snapshot keys from the controller state", () => {
    const lock = readFrostLock({
      afterheat_frost_lock: true, afterheat_frost_lock_reason: "Frost: ude -1,6 °C (grænse 3 °C)",
      afterheat_frost_lock_floor: 19, afterheat_frost_lock_enforced: false, frost_lock_enabled: true, frost_lock_min_setpoint_c: 18,
    });
    expect(lock).toMatchObject({ known: true, locked: true, reason: "Frost: ude -1,6 °C (grænse 3 °C)", floor: 19, enforced: false });
    expect(frostLockStatus(lock)).toBe("Aktiv · min. 19 °C · Frost: ude -1,6 °C (grænse 3 °C) · ikke håndhævet (Pi er ikke busmaster)");
  });

  it("falls back to the minimum setpoint setting for the floor", () => {
    expect(readFrostLock({ afterheat_frost_lock: false, frost_lock_min_setpoint_c: 17.5 }).floor).toBe(17.5);
    expect(frostLockStatus(readFrostLock({ afterheat_frost_lock: false }))).toBe("Ikke aktiv");
    expect(frostLockStatus(readFrostLock({ afterheat_frost_lock: false, frost_lock_enabled: false }))).toBe("Slået fra");
  });

  it("lists the four settings", () => {
    expect([...FROST_LOCK_SETTINGS]).toEqual(["frost_lock_enabled", "frost_lock_outdoor_c", "frost_lock_coil_c", "frost_lock_min_setpoint_c"]);
  });
});

describe("frostLockRefuses", () => {
  it("refuses OFF and values under the floor only while locked", () => {
    const locked = { locked: true, floor: 18 };
    expect(frostLockRefuses(locked, "off")).toBe(true);
    expect(frostLockRefuses(locked, 17)).toBe(true);
    expect(frostLockRefuses(locked, 18)).toBe(false);
    expect(frostLockRefuses(locked, 22)).toBe(false);
    expect(frostLockRefuses({ locked: false, floor: 18 }, "off")).toBe(false);
    expect(frostLockRefuses({ locked: false, floor: 18 }, 12)).toBe(false);
  });

  it("formats the floor message like the Pi", () => {
    expect(frostFloorMessage(18)).toBe("Frostsikring: eftervarmen kan ikke sættes under 18 °C, mens det fryser.");
    expect(frostFloorMessage(18.5)).toBe("Frostsikring: eftervarmen kan ikke sættes under 18,5 °C, mens det fryser.");
  });
});
