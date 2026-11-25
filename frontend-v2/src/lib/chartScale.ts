// Scales and tick helpers for the history charts.

export interface NiceScale { lo: number; hi: number; ticks: number[] }

/** Round step sizes: 1, 2, 2.5, 5 × 10^n. */
function niceStep(rough: number): number {
  const power = Math.pow(10, Math.floor(Math.log10(rough)));
  const fraction = rough / power;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
  return nice * power;
}

/** A y-domain with clean ticks that always contains [min, max] and at least `minSpan`. */
export function niceScale(min: number, max: number, minSpan = 0, target = 4): NiceScale {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { lo: 0, hi: 1, ticks: [0, 1] };
  if (max - min < minSpan) {
    const middle = (min + max) / 2;
    min = middle - minSpan / 2;
    max = middle + minSpan / 2;
  }
  if (max === min) { min -= 1; max += 1; }
  const step = niceStep((max - min) / target);
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let value = lo; value <= hi + step / 2; value += step) ticks.push(Math.round(value / step) * step);
  return { lo, hi, ticks };
}

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;
const TIME_STEPS = [5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 5 * DAY, 7 * DAY];

/** Tick timestamps (seconds) on local clock boundaries, about one per `minGapPx`. */
export function timeTicks(from: number, to: number, widthPx: number, minGapPx = widthPx < 480 ? 62 : 84): number[] {
  const span = Math.max(1, to - from);
  const wanted = Math.max(2, Math.floor(widthPx / minGapPx));
  const step = TIME_STEPS.find(candidate => span / candidate <= wanted) ?? TIME_STEPS[TIME_STEPS.length - 1];
  // Day steps are calendar days: a day is 23 or 25 hours when the clock changes,
  // so a fixed 86400 s would drift an hour (and a day label) after the change.
  if (step >= DAY) {
    const days = Math.round(step / DAY);
    const cursor = new Date(from * 1000);
    cursor.setHours(0, 0, 0, 0);
    if (cursor.getTime() / 1000 < from) cursor.setDate(cursor.getDate() + 1);
    const ticks: number[] = [];
    // Multi-day steps count from the first tick, as before.
    for (let ts = cursor.getTime() / 1000; ts <= to; ts = (cursor.setDate(cursor.getDate() + days), cursor.setHours(0, 0, 0, 0), cursor.getTime() / 1000)) ticks.push(ts);
    return ticks;
  }
  // Sub-day steps are walked on the wall clock, so 06:00 / 12:00 stay put across a clock change.
  const offset = -new Date(from * 1000).getTimezoneOffset() * 60;
  const ticks: number[] = [];
  for (let wall = Math.ceil((from + offset) / step) * step; ; wall += step) {
    const w = new Date(wall * 1000);
    const ts = new Date(w.getUTCFullYear(), w.getUTCMonth(), w.getUTCDate(), w.getUTCHours(), w.getUTCMinutes()).getTime() / 1000;
    if (ts > to) break;
    if (ts >= from && (ticks.length === 0 || ts > ticks[ticks.length - 1])) ticks.push(ts);
  }
  return ticks;
}

export function spanSeconds(from: number, to: number) { return to - from; }

export function formatTick(ts: number, span: number): string {
  const date = new Date(ts * 1000);
  if (span > 2 * DAY) return `${date.toLocaleDateString("da-DK", { weekday: "short" }).replace(".", "")} ${date.getDate()}.`;
  return date.toLocaleTimeString("da-DK", { hour: "2-digit", minute: "2-digit" });
}

export function formatMoment(ts: number, span: number): string {
  const date = new Date(ts * 1000);
  if (span > DAY) return date.toLocaleString("da-DK", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  return `kl. ${date.toLocaleTimeString("da-DK", { hour: "2-digit", minute: "2-digit" })}`;
}

export function formatNumber(value: number, decimals: number): string {
  return value.toLocaleString("da-DK", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

/** Index of the sample nearest to `ts` (samples sorted by ts). */
export function nearestIndex(times: number[], ts: number): number {
  if (times.length === 0) return -1;
  let low = 0;
  let high = times.length - 1;
  while (high - low > 1) {
    const middle = (low + high) >> 1;
    if (times[middle] < ts) low = middle; else high = middle;
  }
  return Math.abs(times[low] - ts) <= Math.abs(times[high] - ts) ? low : high;
}

/** Typical spacing between samples; a jump above 3× this breaks a line. */
export function typicalGap(times: number[]): number {
  if (times.length < 3) return Infinity;
  const gaps = times.slice(1).map((ts, index) => ts - times[index]).sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)];
}
