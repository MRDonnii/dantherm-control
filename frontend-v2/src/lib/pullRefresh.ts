import { useEffect, useRef } from "react";

/** "Træk ned for at opdatere": sider melder her, hvordan de henter deres data igen.
 *  Har den viste side ingen, genindlæses hele siden (location.reload).
 *  En side med ugemte ændringer melder `dirty`; så kaldes dens funktion med `keepDraft: true`
 *  og henter kun det, der ikke er kladden. Ugemte ændringer går aldrig tabt ved et træk. */
export type PullRefreshContext = { keepDraft: boolean };
type Entry = { refresh: (context: PullRefreshContext) => unknown; dirty: boolean };
type Handler = { current: Entry };
const handlers = new Set<Handler>();

/** Kaldes af en side eller et kort med den funktion, der henter deres data igen. Den nyeste udgave bruges altid. */
export function usePullRefresh(refresh: (context: PullRefreshContext) => unknown, options: { dirty?: boolean } = {}) {
  const ref = useRef<Entry>({ refresh, dirty: options.dirty === true });
  ref.current = { refresh, dirty: options.dirty === true };
  useEffect(() => {
    const handler: Handler = ref;
    handlers.add(handler);
    return () => { handlers.delete(handler); };
  }, []);
}

/** "kept" = data hentet, men mindst én side beholdt sine ugemte ændringer. */
export async function runPullRefresh(reload: () => void = () => window.location.reload()): Promise<"data" | "kept" | "reload"> {
  if (!handlers.size) { reload(); return "reload"; }
  const entries = [...handlers].map(handler => handler.current);
  await Promise.allSettled(entries.map(entry => Promise.resolve().then(() => entry.refresh({ keepDraft: entry.dirty }))));
  return entries.some(entry => entry.dirty) ? "kept" : "data";
}

/** The keys whose draft value differs from the saved one (compared as JSON). */
export function changedKeys<T extends Record<string, unknown>>(draft: T, saved: T, keys: readonly string[]): Partial<T> {
  return Object.fromEntries(keys.filter(key => draft[key] !== undefined && JSON.stringify(draft[key]) !== JSON.stringify(saved[key]))
    .map(key => [key, draft[key]])) as Partial<T>;
}

/** Berøringer, som et andet element selv bruger: felter, skydere, grafer, dialoger, menuer og alt der kan rulle. */
const OWN_TOUCH = [
  "input", "select", "textarea", "[contenteditable]", "[role=\"slider\"]", "[role=\"dialog\"]", "[role=\"alertdialog\"]",
  "[aria-modal=\"true\"]", ".fd-sheet", ".fd-drop", ".fd-search", ".topbar-weather-picker", ".hx-chart", ".thermostat-dial",
  ".week-track", "[data-no-pull-refresh]",
].join(",");

export function touchHandledElsewhere(target: EventTarget | null): boolean {
  if (typeof document === "undefined") return true;
  if (document.querySelector("[aria-modal=\"true\"], .fd-sheet[data-open]")) return true;
  for (let el = target instanceof Element ? target : null; el && el !== document.body && el !== document.documentElement; el = el.parentElement) {
    if (el.matches(OWN_TOUCH)) return true;
    const style = getComputedStyle(el);
    if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 1) return true;
    if (/(auto|scroll)/.test(style.overflowX) && el.scrollWidth > el.clientWidth + 1) return true;
  }
  return false;
}

export function pageScrollTop(): number {
  return Math.max(window.scrollY || 0, document.scrollingElement?.scrollTop ?? 0, document.documentElement.scrollTop || 0);
}

/** Hvor langt indikatoren følger fingeren (med modstand), og hvornår et slip opdaterer. */
export const PULL_THRESHOLD = 70;
export const PULL_MAX = 110;
export const pullDistance = (fingerDy: number) => Math.max(0, Math.min(PULL_MAX, fingerDy * 0.6));
