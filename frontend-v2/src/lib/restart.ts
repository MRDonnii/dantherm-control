/** Waiting for the Pi after "Genstart Dantherm-styringen" or "Genstart Pi'en".
 *
 *  The page polls a cheap endpoint and reloads once the Pi answers again. It
 *  must not reload while the old process is still answering in the short
 *  moment before it goes down, so it waits until a poll has failed, or until
 *  long enough has passed that the restart is surely over. */
export type RestartKind = "services" | "pi";

export const RESTART_ACTIONS: Record<RestartKind, { action: string; target?: string }> = {
  services: { action: "restart_service", target: "all" },
  pi: { action: "reboot" },
};

/** earliestMs: no reload before this, even after a failed poll (a Pi cannot
 *  reboot faster, so an early failure was a glitch, not the restart).
 *  minWaitMs: reload on an answer after this even if no poll ever failed.
 *  giveUpMs: stop waiting and say so. */
export const RESTART_TIMING: Record<RestartKind, { earliestMs: number; minWaitMs: number; giveUpMs: number }> = {
  // The services are back within seconds; a Pi 3 reboot takes one to two minutes.
  services: { earliestMs: 2_000, minWaitMs: 8_000, giveUpMs: 3 * 60_000 },
  pi: { earliestMs: 15_000, minWaitMs: 45_000, giveUpMs: 6 * 60_000 },
};

export interface RestartWatch {
  kind: RestartKind;
  startedAt: number;
  /** A poll has failed since the restart was sent: the Pi went away. */
  sawDown: boolean;
}

export type RestartOutcome = "wait" | "reload" | "timeout";

export function startRestartWatch(kind: RestartKind, now: number): RestartWatch {
  return { kind, startedAt: now, sawDown: false };
}

/** One poll result in; the new watch state and what the page should do. */
export function restartStep(watch: RestartWatch, answered: boolean, now: number): { watch: RestartWatch; outcome: RestartOutcome } {
  const next = answered || watch.sawDown ? watch : { ...watch, sawDown: true };
  const elapsed = now - watch.startedAt;
  const { earliestMs, minWaitMs, giveUpMs } = RESTART_TIMING[watch.kind];
  if (answered && elapsed >= earliestMs && (next.sawDown || elapsed >= minWaitMs)) return { watch: next, outcome: "reload" };
  if (elapsed >= giveUpMs) return { watch: next, outcome: "timeout" };
  return { watch: next, outcome: "wait" };
}
