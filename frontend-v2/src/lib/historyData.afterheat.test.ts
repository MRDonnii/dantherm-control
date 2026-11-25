import { describe, expect, it } from "vitest";
import { AFTERHEAT_CHART, afterheatLayers, valvePercent, withAfterheatLines } from "./historyData";

describe("afterheat history lines", () => {
  it("keeps the user's target as goal and the setpoint sent to HAC1 apart", () => {
    const [pi] = withAfterheatLines([{ ts: 1, afterheat_target: 21, afterheat_setpoint: 26, afterheat_level: 3, afterheat_level_target: 4, afterheat_level_wanted: 3.6 }]);
    expect(pi.afterheat_goal).toBe(21);
    expect(pi.afterheat_sent).toBe(26);
    expect(pi.afterheat_valve_percent).toBe(30);
    // The decision is the Pi's whole target level, not its continuous output.
    expect(pi.afterheat_decision_percent).toBe(40);
  });

  it("falls back to the continuous wanted level for the decision", () => {
    const [sample] = withAfterheatLines([{ ts: 1, afterheat_target: 21, afterheat_level: 3, afterheat_level_wanted: 3.6 }]);
    expect(sample.afterheat_decision_percent).toBe(36);
  });

  it("has no decision while HAC1 steers itself", () => {
    const [native] = withAfterheatLines([{ ts: 1, afterheat_target: 21, afterheat_setpoint: 21, afterheat_level: 2, afterheat_level_target: null, afterheat_level_wanted: null }]);
    expect(native.afterheat_valve_percent).toBe(20);
    expect(native.afterheat_decision_percent).toBeNull();
  });

  it("falls back to the setpoint only for history without target and level", () => {
    const [legacy, off] = withAfterheatLines([
      { ts: 1, afterheat_setpoint: 21 },
      // Afterheat off on new firmware: HAC1's stale register is no target.
      { ts: 2, afterheat_setpoint: 21, afterheat_level: 0 },
    ]);
    expect(legacy.afterheat_goal).toBe(21);
    expect(legacy.afterheat_sent).toBeNull();
    expect(legacy.afterheat_valve_percent).toBeNull();
    expect(off.afterheat_goal).toBeNull();
    expect(off.afterheat_valve_percent).toBe(0);
  });

  it("converts averaged levels to 0..100 % and ignores nonsense", () => {
    expect(valvePercent(2.5)).toBe(25);
    expect(valvePercent(12)).toBe(100);
    expect(valvePercent(-1)).toBe(0);
    expect(valvePercent(null)).toBeNull();
  });

  it("asks for the sent line whenever HAC1 is given something else than the target", () => {
    const same = withAfterheatLines([{ ts: 1, afterheat_target: 21, afterheat_setpoint: 21, afterheat_level: 2 }]);
    expect(afterheatLayers(same)).toEqual({ hasValve: true, hasDecision: false, showSent: false });
    // Natively HAC1 gets whole degrees: a 21,5 target is sent as 22.
    const rounded = withAfterheatLines([{ ts: 1, afterheat_target: 21.5, afterheat_setpoint: 22, afterheat_level: 2 }]);
    expect(afterheatLayers(rounded).showSent).toBe(true);
    const pi = withAfterheatLines([{ ts: 1, afterheat_target: 21, afterheat_setpoint: 25, afterheat_level: 2, afterheat_level_target: 3 }]);
    expect(afterheatLayers(pi)).toEqual({ hasValve: true, hasDecision: true, showSent: true });
  });

  it("names the layers in Danish", () => {
    expect(AFTERHEAT_CHART.goal.label).toBe("Mål");
    expect(AFTERHEAT_CHART.sent.label).toBe("Sendt til HAC1");
    expect(AFTERHEAT_CHART.valve.label).toBe("Trin (0-10)");
    expect(AFTERHEAT_CHART.valve.decisionLabel).toBe("Styringens beslutning");
    expect(AFTERHEAT_CHART.valve.decisionIdle).toBe("HAC1 styrer selv");
  });
});
