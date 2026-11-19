import { useEffect, useState } from "react";

/** Udseende som i Fleetdeck: stil (Command/Aurora), tema og menuens placering.
 *  Gemmes kun i denne browser (localStorage), ligesom tema og sidemenu i forvejen. */
export type Skin = "command" | "aurora";
export type NavPosition = "side" | "top";
export type ThemeMode = "system" | "light" | "dark";

export const SKIN_KEY = "hch5-v2-skin";
export const NAV_KEY = "hch5-v2-nav";
export const THEME_KEY = "hch5-v2-theme";
export const PREFERENCES_EVENT = "hch5-ui-preferences";

/** Forslag A (standard): Command, mørkt, sidemenu. Forslag B: Aurora, lyst, topmenu. */
export const SKIN_PRESETS: Record<Skin, { theme: ThemeMode; nav: NavPosition }> = {
  command: { theme: "dark", nav: "side" },
  aurora: { theme: "light", nav: "top" },
};

function read(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
function write(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* privat tilstand: valget gælder kun nu */ }
}

export function readSkin(): Skin { return read(SKIN_KEY) === "aurora" ? "aurora" : "command"; }
export function readNav(): NavPosition { return read(NAV_KEY) === "top" ? "top" : "side"; }
export function readTheme(): ThemeMode {
  const value = read(THEME_KEY) ?? "dark";
  return value === "light" || value === "dark" ? value : "system";
}

export function saveAppearance(next: { skin?: Skin; nav?: NavPosition; theme?: ThemeMode }) {
  if (next.skin) write(SKIN_KEY, next.skin);
  if (next.nav) write(NAV_KEY, next.nav);
  if (next.theme) write(THEME_KEY, next.theme);
  window.dispatchEvent(new Event(PREFERENCES_EVENT));
}

/** Vælg en stil med dens tema og menu (forslag A eller B). Tema og menu kan bagefter ændres hver for sig. */
export function chooseSkin(skin: Skin) {
  saveAppearance({ skin, ...SKIN_PRESETS[skin] });
}


/** Den valgte stil, opdateret når den ændres i menuen Udseende. */
export function useSkin(): Skin {
  const [skin, setSkin] = useState<Skin>(readSkin);
  useEffect(() => {
    const sync = () => setSkin(readSkin());
    window.addEventListener(PREFERENCES_EVENT, sync);
    return () => window.removeEventListener(PREFERENCES_EVENT, sync);
  }, []);
  return skin;
}
