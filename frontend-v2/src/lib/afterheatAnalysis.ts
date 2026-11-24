/** Observed performance; gaps and off periods never count as stable operation. */
export type HeatSample = Record<string, number | null>;
export function afterheatAnalysis(samples: HeatSample[]) {
  let covered = 0, inBand = 0, absoluteError = 0, overshoot = 0, undershoot = 0, changes = 0;
  let previous: HeatSample | null = null, stableSince: number | null = null, changedAt: number | null = null;
  const settling: number[] = [];
  const valid = (s: HeatSample) => Number.isFinite(s.ts) && typeof s.afterheat_target === "number"
    && Number.isFinite(s.afterheat_target) && typeof s.heating_coil_after_temperature === "number"
    && Number.isFinite(s.heating_coil_after_temperature) && typeof s.afterheat_active === "number"
    && Number.isFinite(s.afterheat_active) && s.afterheat_active > 0;
  for (const sample of [...samples].sort((a, b) => Number(a.ts) - Number(b.ts))) {
    if (!valid(sample)) { previous = null; stableSince = changedAt = null; continue; }
    const ts = Number(sample.ts), target = Number(sample.afterheat_target), air = Number(sample.heating_coil_after_temperature);
    const dt = previous ? ts - Number(previous.ts) : 0;
    if (!previous || dt <= 0 || dt > 300) { previous = sample; stableSince = changedAt = null; continue; }
    if (Math.abs(target - Number(previous.afterheat_target)) > 0.25) {
      changedAt = ts; stableSince = null; previous = sample; continue;
    }
    const error = Number(previous.heating_coil_after_temperature) - Number(previous.afterheat_target);
    const activeTime = dt * Math.min(1, Math.max(0, Number(previous.afterheat_active)));
    covered += activeTime; absoluteError += Math.abs(error) * activeTime;
    if (Math.abs(error) <= 0.5) inBand += activeTime;
    overshoot = Math.max(overshoot, error, air - target);
    undershoot = Math.max(undershoot, -error, target - air);
    if (Math.abs(air - target) <= 0.5 && sample.afterheat_active === 1 && previous.afterheat_active === 1) {
      stableSince ??= ts;
      if (changedAt !== null && ts - stableSince >= 600) {
        settling.push((stableSince - changedAt) / 60); changedAt = null;
      }
    } else stableSince = null;
    if (typeof previous.afterheat_level === "number" && typeof sample.afterheat_level === "number"
      && Math.abs(sample.afterheat_level - previous.afterheat_level) >= 0.5) changes += 1;
    previous = sample;
  }
  return { minutes: covered / 60, inBandPercent: covered ? 100 * inBand / covered : null,
    meanError: covered ? absoluteError / covered : null, overshoot: covered ? overshoot : null,
    undershoot: covered ? undershoot : null, observedValveChanges: changes,
    settlingMinutes: settling.length ? settling.reduce((a, b) => a + b, 0) / settling.length : null };
}
