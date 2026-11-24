import { useEffect, useRef, useState, type CSSProperties } from "react";
import { ArrowDown, RefreshCw } from "lucide-react";
import { PULL_THRESHOLD, pageScrollTop, pullDistance, runPullRefresh, touchHandledElsewhere } from "../lib/pullRefresh";

type PullState = "idle" | "pulling" | "ready" | "refreshing";
const LABEL: Record<PullState, string> = { idle: "", pulling: "Træk for at opdatere", ready: "Slip for at opdatere", refreshing: "Opdaterer…" };

/** Træk ned for at opdatere på telefonen, også som hjemmeskærm-app hvor iOS ikke selv har det. Kun berøring. */
export function PullToRefresh({ reload }: { reload?: () => void }) {
  const [state, setState] = useState<PullState>("idle");
  const [pull, setPull] = useState(0);
  const busy = useRef(false);

  useEffect(() => {
    let start: { x: number; y: number } | null = null;
    let pulling = false;
    let distance = 0;
    const reset = () => { start = null; pulling = false; distance = 0; };
    const onStart = (event: TouchEvent) => {
      reset();
      if (busy.current || event.touches.length !== 1 || pageScrollTop() > 0 || touchHandledElsewhere(event.target)) return;
      start = { x: event.touches[0].clientX, y: event.touches[0].clientY };
    };
    const onMove = (event: TouchEvent) => {
      if (!start) return;
      if (event.touches.length !== 1) { reset(); setState("idle"); setPull(0); return; }
      const dx = event.touches[0].clientX - start.x;
      const dy = event.touches[0].clientY - start.y;
      if (!pulling) {
        // Først når bevægelsen tydeligt går nedad (og ikke sidelæns) bliver det et træk.
        if (dy < -4 || Math.abs(dx) > 12 && Math.abs(dx) > dy) { start = null; return; }
        if (dy < 10 || Math.abs(dx) > dy) return;
        if (pageScrollTop() > 0) { start = null; return; }
        pulling = true;
      }
      if (event.cancelable) event.preventDefault();
      distance = pullDistance(dy);
      setPull(distance);
      setState(distance >= PULL_THRESHOLD ? "ready" : "pulling");
    };
    const onEnd = () => {
      if (!pulling) { reset(); return; }
      const fire = distance >= PULL_THRESHOLD;
      reset();
      if (!fire) { setState("idle"); setPull(0); return; }
      busy.current = true;
      setState("refreshing");
      setPull(PULL_THRESHOLD * 0.8);
      const started = Date.now();
      void runPullRefresh(reload).finally(() => {
        window.setTimeout(() => { busy.current = false; setState("idle"); setPull(0); }, Math.max(0, 500 - (Date.now() - started)));
      });
    };
    const onCancel = () => { if (pulling) { setState("idle"); setPull(0); } reset(); };
    document.addEventListener("touchstart", onStart, { passive: true });
    document.addEventListener("touchmove", onMove, { passive: false });
    document.addEventListener("touchend", onEnd, { passive: true });
    document.addEventListener("touchcancel", onCancel, { passive: true });
    return () => {
      document.removeEventListener("touchstart", onStart);
      document.removeEventListener("touchmove", onMove);
      document.removeEventListener("touchend", onEnd);
      document.removeEventListener("touchcancel", onCancel);
    };
  }, [reload]);

  if (state === "idle") return null;
  const style = { "--ptr-pull": `${pull}px`, "--ptr-opacity": String(Math.min(1, pull / 40)) } as CSSProperties;
  return <div className="ptr-indicator" data-state={state} role="status" aria-live="polite" style={style}>
    {state === "refreshing" ? <RefreshCw size={16}/> : <ArrowDown size={16}/>}
    <span>{LABEL[state]}</span>
  </div>;
}
