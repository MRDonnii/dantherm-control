import { describe, expect, it } from "vitest";
import { AFTERHEAT_CONTROL_SETTINGS, controlLabel, formatError, formatTick, levelText, nextActionText, readAfterheatControl, readValvePercent, valveText } from "./afterheatControl";

const PI = {
  afterheat_control_mode: "pi", afterheat_control_active: "pi", afterheat_level: 3, afterheat_level_wanted: 3.44, afterheat_level_target: 4,
  afterheat_level_max: 10, afterheat_next_action: "up", afterheat_next_tick_seconds: 12, afterheat_target: 21.5, afterheat_hac1_setpoint: 22,
  afterheat_control_error: -0.42, afterheat_control_reason: "Går et trin op: 21,1 °C under mål",
};

describe("readAfterheatControl", () => {
  it("reads every snapshot key", () => {
    const control = readAfterheatControl(PI);
    expect(control).toEqual({
      known: true, mode: "pi", active: "pi", level: 3, valvePercent: 30, levelWanted: 3.44, levelTarget: 4, levelMax: 10, nextAction: "up",
      nextTickSeconds: 12, target: 21.5, hac1Setpoint: 22, error: -0.42, reason: "Går et trin op: 21,1 °C under mål",
    });
  });

  it("is unknown on older firmware and tolerates nulls", () => {
    const control = readAfterheatControl({ afterheat_setpoint: 20 });
    expect(control.known).toBe(false);
    expect(control.level).toBeNull();
    expect(control.levelMax).toBe(10);
    const nulls = readAfterheatControl({ afterheat_control_mode: "native", afterheat_control_active: "fallback", afterheat_level: null, afterheat_next_action: null, afterheat_next_tick_seconds: null, afterheat_control_reason: " " });
    expect(nulls.known).toBe(true);
    expect(nulls.level).toBeNull();
    expect(nulls.nextAction).toBeNull();
    expect(nulls.nextTickSeconds).toBeNull();
    expect(nulls.reason).toBeNull();
    // A zero or broken maximum falls back to HAC1's ten levels.
    expect(readAfterheatControl({ afterheat_control_active: "pi", afterheat_level_max: 0 }).levelMax).toBe(10);
  });

  it("ignores unknown values and clamps levels to the range", () => {
    const control = readAfterheatControl({ afterheat_control_mode: "turbo", afterheat_control_active: "pi", afterheat_level: 14, afterheat_level_wanted: -2, afterheat_next_action: "sideways", afterheat_next_tick_seconds: -3 });
    expect(control.mode).toBeNull();
    expect(control.level).toBe(10);
    expect(control.levelWanted).toBe(0);
    expect(control.nextAction).toBeNull();
    expect(control.nextTickSeconds).toBe(0);
  });

  it("is saved with the controller settings", () => {
    expect(AFTERHEAT_CONTROL_SETTINGS).toContain("afterheat_control_mode");
    expect(AFTERHEAT_CONTROL_SETTINGS).toContain("afterheat_pi_band");
    expect(AFTERHEAT_CONTROL_SETTINGS).toContain("afterheat_pi_min_dwell_ticks");
  });
});

describe("valve opening", () => {
  it("is HAC1's level × 10 %, 0..100, and the gateway's own figure wins", () => {
    expect(readValvePercent({ actual_afterheat_level: 4 })).toBe(40);
    expect(readValvePercent({ afterheat_level: 10 })).toBe(100);
    expect(readValvePercent({ afterheat_level: 0 })).toBe(0);
    expect(readValvePercent({ actual_afterheat_valve_percent: 60, actual_afterheat_level: 4 })).toBe(60);
    expect(readValvePercent({ actual_afterheat_level: 14 })).toBe(100);
    // The on/off flag (register 209) is never an opening.
    expect(readValvePercent({ actual_afterheat: true })).toBeNull();
    expect(readValvePercent({ actual_afterheat_level: null })).toBeNull();
  });

  it("reads as Danish text", () => {
    expect(valveText(30)).toBe("Ventilåbning 30 %");
    expect(valveText(null)).toBe("Ventilåbning —");
  });
});

describe("afterheat control texts", () => {
  it("names who regulates", () => {
    expect(controlLabel({ active: "pi", mode: "pi" })).toBe("Pi-styring");
    expect(controlLabel({ active: "native", mode: "native" })).toBe("HAC1 styrer selv");
    expect(controlLabel({ active: "fallback", mode: "pi" })).toBe("HAC1 midlertidigt");
    expect(controlLabel({ active: null, mode: "pi" })).toBe("Pi-styring");
    expect(controlLabel({ active: null, mode: null })).toBe("—");
  });

  it("formats the countdown", () => {
    expect(formatTick(null)).toBe("");
    expect(formatTick(0.3)).toBe("nu");
    expect(formatTick(12.4)).toBe("om 12 s");
    expect(formatTick(65)).toBe("om 1 min 5 s");
    expect(formatTick(120)).toBe("om 2 min");
  });

  it("describes the level, the next action and the error", () => {
    const control = readAfterheatControl(PI);
    expect(levelText(control)).toBe("Trin 3/10");
    expect(levelText({ level: null, levelMax: 10 })).toBe("Trin —/10");
    expect(nextActionText(control, 12)).toBe("Et trin op til 4 om 12 s");
    expect(nextActionText({ nextAction: "hold", levelTarget: 3 }, 40)).toBe("Holder trinnet om 40 s");
    expect(nextActionText({ nextAction: null, levelTarget: null }, 5)).toBe("—");
    expect(formatError(-0.42)).toBe("−0,42 K");
    expect(formatError(0.2)).toBe("+0,20 K");
    expect(formatError(null)).toBe("—");
  });
});
