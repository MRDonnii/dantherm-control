import { describeControl } from "./control";

/** One time the fan step (or who decides it) changed, in plain words. */
export interface FanChange {
  ts: number;
  from: number | null;
  to: number | null;
  /** Seconds until the next change; null for the latest one (still in force). */
  lasted: number | null;
  who: string;
  reason: string;
  room: string | null;
}

interface RawEntry { timestamp?: unknown; master?: unknown; source?: unknown; level?: unknown; reason?: unknown; fireplace?: unknown; controlling_room?: unknown }

const num = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;

/**
 * The controller's decision log lists every change of who decides or which room is
 * closest; most of them do not move the fan. Keep only the moments the step or
 * the deciding part changed, newest first.
 */
export function fanChanges(log: unknown, max = 40): FanChange[] {
  if (!Array.isArray(log)) return [];
  const entries = (log as RawEntry[])
    .filter(entry => entry && num(entry.timestamp) !== null)
    .sort((a, b) => (a.timestamp as number) - (b.timestamp as number));
  const changes: FanChange[] = [];
  let previous: RawEntry | null = null;
  for (const entry of entries) {
    const level = num(entry.level);
    const same = previous !== null && num(previous.level) === level && previous.source === entry.source
      && previous.master === entry.master && Boolean(previous.fireplace) === Boolean(entry.fireplace);
    if (!same) {
      const summary = describeControl({
        active_master: entry.master, effective_source: entry.source, effective_level: level,
        effective_reason: entry.reason, fireplace: entry.fireplace === true,
      });
      const last: FanChange | undefined = changes[changes.length - 1];
      if (last) last.lasted = (entry.timestamp as number) - last.ts;
      changes.push({
        ts: entry.timestamp as number, from: previous === null ? null : num(previous.level), to: level, lasted: null,
        who: summary.title, reason: summary.reason,
        room: typeof entry.controlling_room === "string" && entry.controlling_room.trim() !== "" ? entry.controlling_room.trim() : null,
      });
    }
    previous = entry;
  }
  return changes.reverse().slice(0, max);
}

/** "12 min", "2 t 5 min", "3 dage". */
export function lastedText(seconds: number | null): string {
  if (seconds === null) return "";
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return "under 1 min";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return minutes % 60 ? `${hours} t ${minutes % 60} min` : `${hours} t`;
  return `${Math.round(hours / 24)} dage`;
}
