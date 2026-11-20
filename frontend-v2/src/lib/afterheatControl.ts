// Pi control of the HAC1 afterheat level (gateway 1.9.0). HAC1 drives its
// water valve in levels 0..10 and moves at most one level per controller tick
// (about once a minute). With "pi" the Pi chooses the level itself from the
// user's target temperature; with "native" HAC1 regulates on its own. The Pi
// is the source of truth: the WebUI only shows the snapshot keys, and older
// firmware without them keeps the old UI.

export type AfterheatControlMode = "native" | "pi";
export type AfterheatControlActive = "pi" | "native" | "fallback";
export type AfterheatAction = "up" | "hold" | "down";

/** Settings the Pi controller adds to the advanced controller settings. */
export const AFTERHEAT_CONTROL_SETTINGS = ["afterheat_control_mode", "afterheat_pi_gain", "afterheat_pi_integral_minutes"] as const;

export const AFTERHEAT_LEVEL_MAX = 10;

export interface AfterheatControl {
  /** The controller reports the afterheat level control at all (newer firmware). */
  known: boolean;
  mode: AfterheatControlMode | null;
  active: AfterheatControlActive | null;
  /** Level HAC1 reports right now, 0..max. */
  level: number | null;
  /** Continuous level the Pi wants (PI output). */
  levelWanted: number | null;
  /** Whole level the Pi steers toward at the next tick. */
  levelTarget: number | null;
  levelMax: number;
  nextAction: AfterheatAction | null;
  nextTickSeconds: number | null;
  /** The user's effective target in °C (fractions allowed). */
  target: number | null;
  /** Whole-degree setpoint the Pi sent to HAC1. */
  hac1Setpoint: number | null;
  /** T2AH minus the target in K. */
  error: number | null;
  reason: string | null;
}

function finite(value: unknown): number | null {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function oneOf<T extends string>(value: unknown, options: readonly T[]): T | null {
  return typeof value === "string" && (options as readonly string[]).includes(value) ? value as T : null;
}

export function readAfterheatControl(controller: Record<string, unknown>): AfterheatControl {
  const mode = oneOf(controller.afterheat_control_mode, ["native", "pi"] as const);
  const active = oneOf(controller.afterheat_control_active, ["pi", "native", "fallback"] as const);
  const reportedMax = finite(controller.afterheat_level_max);
  // A missing or nonsense maximum must not break the bar (division by zero).
  const levelMax = reportedMax !== null && reportedMax >= 1 ? Math.round(reportedMax) : AFTERHEAT_LEVEL_MAX;
  const clampLevel = (value: number | null) => value === null ? null : Math.min(levelMax, Math.max(0, value));
  const reason = typeof controller.afterheat_control_reason === "string" && controller.afterheat_control_reason.trim() !== ""
    ? controller.afterheat_control_reason.trim()
    : null;
  const tick = finite(controller.afterheat_next_tick_seconds);
  return {
    known: mode !== null || active !== null,
    mode,
    active,
    level: clampLevel(finite(controller.afterheat_level)),
    levelWanted: clampLevel(finite(controller.afterheat_level_wanted)),
    levelTarget: clampLevel(finite(controller.afterheat_level_target)),
    levelMax,
    nextAction: oneOf(controller.afterheat_next_action, ["up", "hold", "down"] as const),
    nextTickSeconds: tick === null ? null : Math.max(0, tick),
    target: finite(controller.afterheat_target),
    hac1Setpoint: finite(controller.afterheat_hac1_setpoint),
    error: finite(controller.afterheat_control_error),
    reason,
  };
}

/** Who regulates the afterheat right now. */
export function controlLabel(control: Pick<AfterheatControl, "active" | "mode">): string {
  if (control.active === "pi") return "Pi-styring";
  if (control.active === "fallback") return "HAC1 midlertidigt";
  if (control.active === "native") return "HAC1 styrer selv";
  if (control.mode === "pi") return "Pi-styring";
  if (control.mode === "native") return "HAC1 styrer selv";
  return "—";
}

export function modeLabel(mode: AfterheatControlMode | null): string {
  return mode === "pi" ? "Pi-styring" : mode === "native" ? "HAC1 styrer selv" : "—";
}

export function actionLabel(action: AfterheatAction | null): string {
  if (action === "up") return "Et trin op";
  if (action === "down") return "Et trin ned";
  if (action === "hold") return "Holder trinnet";
  return "—";
}

/** Seconds left to the next HAC1 tick: "om 23 s", "om 1 min 5 s", "nu". */
export function formatTick(seconds: number | null): string {
  if (seconds === null) return "";
  const whole = Math.max(0, Math.round(seconds));
  if (whole === 0) return "nu";
  if (whole < 60) return `om ${whole} s`;
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return rest ? `om ${minutes} min ${rest} s` : `om ${minutes} min`;
}

const da = (value: number, digits: number) => value.toLocaleString("da-DK", { minimumFractionDigits: digits, maximumFractionDigits: digits });

export function formatLevel(value: number | null, digits = 0): string {
  return value === null ? "—" : da(value, digits);
}

export function formatTarget(value: number | null): string {
  return value === null ? "—" : `${value.toLocaleString("da-DK", { maximumFractionDigits: 1 })} °C`;
}

/** "Trin 3/10" with the level reported by HAC1. */
export function levelText(control: Pick<AfterheatControl, "level" | "levelMax">): string {
  return control.level === null ? `Trin —/${control.levelMax}` : `Trin ${da(control.level, 0)}/${control.levelMax}`;
}

/** Next action with the countdown, e.g. "Et trin op om 12 s". */
export function nextActionText(control: Pick<AfterheatControl, "nextAction" | "levelTarget">, seconds: number | null): string {
  if (control.nextAction === null) return "—";
  const base = control.nextAction === "hold" || control.levelTarget === null
    ? actionLabel(control.nextAction)
    : `${actionLabel(control.nextAction)} til ${da(control.levelTarget, 0)}`;
  const when = formatTick(seconds);
  return when ? `${base} ${when}` : base;
}

/** Signed temperature error, e.g. "+0,24 K". */
export function formatError(value: number | null): string {
  if (value === null) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "±";
  return `${sign}${da(Math.abs(value), 2)} K`;
}
