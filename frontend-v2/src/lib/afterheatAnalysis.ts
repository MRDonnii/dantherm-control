/** Observed performance; gaps and off periods never count as stable operation. */
export type HeatSample = Record<string, number | null>;
export const AFTERHEAT_CONTEXTS: Record<string, string> = {
  all: "Alle driftstilstande", "0": "Normal drift", "1": "Frostbeskyttelse", "2": "Opstart",
  "3": "Boost", "4": "Eftervarme slukket", "5": "Køling", "6": "Ændret ventilatortrin",
  "7": "Midlertidigt forsøg", "8": "HCP4 / afventer master", unknown: "Ældre eller blandede data",
};
export function heatContext(sample: HeatSample): string {
  const value = sample.afterheat_context;
  return typeof value === "number" && Number.isInteger(value) && String(value) in AFTERHEAT_CONTEXTS ? String(value) : "unknown";
}
/** Stable band used when the controller does not say (afterheat_pi_band is 0,1 - 1,5 K). */
export const DEFAULT_BAND = 0.5;
/** Above this outdoor temperature HAC1 locks the afterheat; a closed valve then says nothing. */
const SUMMER_CUTOFF = 15;

export function afterheatAnalysis(samples: HeatSample[], context = "all", band = DEFAULT_BAND) {
  let covered = 0, inBand = 0, absoluteError = 0, overshoot = 0, undershoot = 0, changes = 0;
  let previous: HeatSample | null = null, stableSince: number | null = null, changedAt: number | null = null;
  const settling: number[] = [];
  const valid = (s: HeatSample) => Number.isFinite(s.ts) && typeof s.afterheat_target === "number"
    && Number.isFinite(s.afterheat_target) && typeof s.heating_coil_after_temperature === "number"
    && Number.isFinite(s.heating_coil_after_temperature) && typeof s.afterheat_active === "number"
    && Number.isFinite(s.afterheat_active)
    // Open valve, or a closed valve while the afterheat is on (HAC1 reports a level): the air may
    // then sit too cold, which is exactly what an analysis of "how well is the target held" must count.
    && (s.afterheat_active > 0 || (typeof s.afterheat_level === "number" && Number.isFinite(s.afterheat_level)
      && !(typeof s.outdoor_temp === "number" && s.outdoor_temp >= SUMMER_CUTOFF)));
  for (const sample of [...samples].sort((a, b) => Number(a.ts) - Number(b.ts))) {
    if (!valid(sample) || (context !== "all" && heatContext(sample) !== context)) { previous = null; stableSince = changedAt = null; continue; }
    const ts = Number(sample.ts), target = Number(sample.afterheat_target), air = Number(sample.heating_coil_after_temperature);
    const dt = previous ? ts - Number(previous.ts) : 0;
    if (!previous || dt <= 0 || dt > 300) { previous = sample; stableSince = changedAt = null; continue; }
    if (Math.abs(target - Number(previous.afterheat_target)) > 0.25) {
      changedAt = ts; stableSince = null; previous = sample; continue;
    }
    const error = Number(previous.heating_coil_after_temperature) - Number(previous.afterheat_target);
    const activeTime = dt;
    covered += activeTime; absoluteError += Math.abs(error) * activeTime;
    if (Math.abs(error) <= band) inBand += activeTime;
    overshoot = Math.max(overshoot, error, air - target);
    undershoot = Math.max(undershoot, -error, target - air);
    if (Math.abs(air - target) <= band) {
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
