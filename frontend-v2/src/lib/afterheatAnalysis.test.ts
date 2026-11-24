import { describe, expect, it } from "vitest";
import { afterheatAnalysis } from "./afterheatAnalysis";
const row = (ts: number, air: number, target = 22, level = 2) => ({ ts, heating_coil_after_temperature: air, afterheat_target: target, afterheat_level: level, afterheat_active: 1 });
describe("Observed afterheat performance", () => {
  it("weights actual intervals and reports error, excursions and valve changes", () => {
    const a = afterheatAnalysis([row(1000, 22), row(1060, 23, 22, 3), row(1180, 21, 22, 2)]);
    expect(a.minutes).toBe(3); expect(a.inBandPercent).toBeCloseTo(100/3);
    expect(a.meanError).toBeCloseTo(2/3); expect(a.overshoot).toBe(1); expect(a.undershoot).toBe(1);
    expect(a.observedValveChanges).toBe(2);
  });
  it("does not fill gaps, missing targets or OFF intervals", () => {
    const a = afterheatAnalysis([row(1000,22), row(2000,22), { ...row(2060,22), afterheat_target: null }, row(2120,22), {...row(2180,22), afterheat_active:0}]);
    expect(a.minutes).toBe(0); expect(a.meanError).toBeNull(); expect(a.settlingMinutes).toBeNull();
  });
  it("requires ten continuous stable minutes after an actual target change", () => {
    const samples=[row(1000,21,21),row(1060,21,22), ...Array.from({length:12},(_,i)=>row(1120+i*60,22,22))];
    expect(afterheatAnalysis(samples).settlingMinutes).toBe(1);
    expect(afterheatAnalysis(samples.slice(0,8)).settlingMinutes).toBeNull();
  });
});
