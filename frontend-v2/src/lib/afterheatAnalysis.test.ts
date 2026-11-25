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
    const a = afterheatAnalysis([row(1000,22), row(2000,22), { ...row(2060,22), afterheat_target: null }, row(2120,22), {...row(2180,22), afterheat_active:0, afterheat_level:null}]);
    expect(a.minutes).toBe(0); expect(a.meanError).toBeNull(); expect(a.settlingMinutes).toBeNull();
  });
  it("counts air that is too cold while the valve is shut, and takes the band from the settings", () => {
    const closed = (ts: number, air: number) => ({ ...row(ts, air, 22, 0), afterheat_active: 0 });
    const a = afterheatAnalysis([closed(1000, 20), closed(1060, 20), closed(1120, 21.4)]);
    expect(a.minutes).toBe(2); expect(a.undershoot).toBeCloseTo(2); expect(a.inBandPercent).toBe(0);
    // A 1,5 K band accepts the 1,4 K shortfall of the last minute.
    expect(afterheatAnalysis([closed(1000, 21.4), closed(1060, 21.4), closed(1120, 21.4)], "all", 1.5).inBandPercent).toBe(100);
    // Summer lock: a shut valve says nothing.
    expect(afterheatAnalysis([{ ...closed(1000, 20), outdoor_temp: 18 }, { ...closed(1060, 20), outdoor_temp: 18 }]).minutes).toBe(0);
  });
  it("requires ten continuous stable minutes after an actual target change", () => {
    const samples=[row(1000,21,21),row(1060,21,22), ...Array.from({length:12},(_,i)=>row(1120+i*60,22,22))];
    expect(afterheatAnalysis(samples).settlingMinutes).toBe(1);
    expect(afterheatAnalysis(samples.slice(0,8)).settlingMinutes).toBeNull();
  });
  it("separates operating contexts without bridging omitted samples or old data", () => {
    const samples=[{...row(1000,22),afterheat_context:0},{...row(1060,22),afterheat_context:0},
      {...row(1120,30),afterheat_context:1},{...row(1180,22),afterheat_context:0},
      {...row(1240,22),afterheat_context:0},row(1300,25),row(1360,25)];
    const normal=afterheatAnalysis(samples,"0");
    expect(normal.minutes).toBe(2);expect(normal.overshoot).toBe(0);
    expect(afterheatAnalysis(samples,"unknown").minutes).toBe(1);
    expect(afterheatAnalysis(samples,"1").minutes).toBe(0);
  });
});
