import { describe, expect, it } from "vitest";
import { nearestIndex, niceScale, timeTicks, typicalGap } from "./chartScale";

describe("niceScale", () => {
  it("covers the data with clean steps", () => {
    const scale = niceScale(18.8, 25.3);
    expect(scale.lo).toBeLessThanOrEqual(18.8);
    expect(scale.hi).toBeGreaterThanOrEqual(25.3);
    expect(scale.ticks).toEqual([18, 20, 22, 24, 26]);
  });
  it("widens a flat series to the minimum span", () => {
    const scale = niceScale(21, 21.2, 4);
    expect(scale.hi - scale.lo).toBeGreaterThanOrEqual(4);
  });
  it("handles negative temperatures", () => {
    expect(niceScale(-3.2, 1.9).ticks).toEqual([-4, -2, 0, 2]);
  });
});

describe("timeTicks", () => {
  it("places hourly ticks on whole hours", () => {
    const from = new Date(2026, 10, 20, 0, 13).getTime() / 1000;
    const ticks = timeTicks(from, from + 6 * 3600, 600);
    expect(ticks.length).toBeGreaterThanOrEqual(3);
    for (const tick of ticks) expect(new Date(tick * 1000).getMinutes()).toBe(0);
  });
  it("uses midnights for week ranges", () => {
    const from = new Date(2026, 10, 13, 15, 0).getTime() / 1000;
    for (const tick of timeTicks(from, from + 7 * 86400, 900)) expect(new Date(tick * 1000).getHours()).toBe(0);
  });
});

describe("sample helpers", () => {
  it("finds the nearest sample and the typical gap", () => {
    const times = [0, 60, 120, 180, 600];
    expect(nearestIndex(times, 130)).toBe(2);
    expect(nearestIndex(times, 500)).toBe(4);
    expect(typicalGap(times)).toBe(60);
  });
});

describe("timeTicks across the end of summer time", () => {
  it("keeps every day tick on local midnight and gives each day once", () => {
    const env = (globalThis as unknown as { process: { env: Record<string, string | undefined> } }).process.env;
    const before = env.TZ;
    env.TZ = "Europe/Copenhagen";
    try {
      const from = new Date(2027, 9, 25, 12).getTime() / 1000;
      const to = new Date(2027, 10, 5, 12).getTime() / 1000;
      const ticks = timeTicks(from, to, 900);
      const days = ticks.map(tick => new Date(tick * 1000).getDate());
      for (const tick of ticks) expect(new Date(tick * 1000).getHours()).toBe(0);
      expect(new Set(days).size).toBe(days.length);
      // Six-hour ticks stay on 00/06/12/18 after the change too.
      for (const tick of timeTicks(new Date(2027, 9, 30, 0).getTime() / 1000, new Date(2027, 10, 1, 12).getTime() / 1000, 900)) expect(new Date(tick * 1000).getMinutes()).toBe(0);
    } finally {
      if (before === undefined) delete env.TZ; else env.TZ = before;
    }
  });
});
