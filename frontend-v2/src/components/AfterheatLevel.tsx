import { useEffect, useRef, useState, type CSSProperties } from "react";
import { ArrowDown, ArrowUp, Minus } from "lucide-react";
import { controlLabel, formatLevel, formatTarget, levelText, nextActionText, type AfterheatControl } from "../lib/afterheatControl";

/**
 * Counts down from the last snapshot value once a second, so the countdown
 * runs smoothly between the two-second polls. A new value from the Pi resets it.
 */
export function useCountdown(seconds: number | null): number | null {
  const base = useRef<{ value: number; at: number } | null>(null);
  const [, setNow] = useState(0);
  if (seconds === null) base.current = null;
  else if (base.current === null || base.current.value !== seconds) base.current = { value: seconds, at: Date.now() };
  useEffect(() => {
    if (seconds === null) return;
    const timer = window.setInterval(() => setNow(value => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [seconds]);
  if (base.current === null) return null;
  return Math.max(0, base.current.value - (Date.now() - base.current.at) / 1000);
}

/** Ten segments for HAC1's levels 1..10, the current level lit and the target marked. */
export function AfterheatLevelBar({ level, target, wanted, max }: { level: number | null; target: number | null; wanted: number | null; max: number }) {
  const segments = Array.from({ length: max }, (_, index) => index + 1);
  const marker = wanted ?? target;
  return <div className="afterheat-level-bar" role="meter" aria-label="HAC1-trin" aria-valuemin={0} aria-valuemax={max}
    aria-valuenow={level ?? undefined} aria-valuetext={level === null ? "Ukendt" : `Trin ${level} af ${max}${target !== null ? `, mål ${target}` : ""}`}>
    {segments.map(step => <i key={step} className={[
      level !== null && step <= level ? "is-lit" : "",
      target !== null && step === target && target !== level ? "is-target" : "",
    ].filter(Boolean).join(" ") || undefined}/>)}
    {marker !== null && <b className="afterheat-level-marker" style={{ "--at": `${(marker / max) * 100}%` } as CSSProperties}
      title={`Mål-trin ${formatLevel(marker, wanted !== null ? 1 : 0)}`}/>}
  </div>;
}

/**
 * Who regulates the afterheat, the level HAC1 runs and what happens at the next
 * tick. Shown under the thermostat on firmware with the Pi level control.
 */
export function AfterheatLevelPanel({ control }: { control: AfterheatControl }) {
  const seconds = useCountdown(control.active === "pi" ? control.nextTickSeconds : null);
  const pi = control.active === "pi";
  const ActionIcon = control.nextAction === "up" ? ArrowUp : control.nextAction === "down" ? ArrowDown : Minus;
  return <section className={`afterheat-level tone-${control.active ?? "unknown"}`} aria-label="Eftervarmens trin">
    <div className="afterheat-level-head">
      <strong>{levelText(control)}</strong>
      <span className="afterheat-level-who">{controlLabel(control)}</span>
    </div>
    <AfterheatLevelBar level={control.level} target={pi ? control.levelTarget : null} wanted={pi ? control.levelWanted : null} max={control.levelMax}/>
    <div className="afterheat-level-facts">
      <span><small>Mål</small><strong>{formatTarget(control.target)}</strong></span>
      {pi && <span><small>Mål-trin</small><strong>{formatLevel(control.levelWanted, 1)}</strong></span>}
      {pi && control.nextAction !== null && <span className="afterheat-level-next"><small>Næste</small><strong><ActionIcon size={13} aria-hidden="true"/>{nextActionText(control, seconds)}</strong></span>}
    </div>
    {control.reason && <p className="afterheat-level-reason">{control.reason}</p>}
  </section>;
}
