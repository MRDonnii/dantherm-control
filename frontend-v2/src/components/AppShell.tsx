import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { postJson, requestJson } from "../lib/api";
import { usePollHealth } from "../lib/connection";
import { TopbarNoticeContext } from "../lib/topbar-notice";
import { haLinkText, readHaLink } from "../lib/haLink";
import { ROLE_NAMES, SessionContext, sessionCan, type AuthStatus, type Permission } from "../lib/session";
import { PREFERENCES_EVENT, chooseSkin, readNav, readSkin, readTheme, saveAppearance, type NavPosition, type Skin, type ThemeMode } from "../lib/appearance";
import { TopbarWeather, type WeatherSummary } from "./TopbarWeather";
import { TablerIcon, type TablerName } from "./TablerIcon";

type NavItem = readonly [to: string, label: string, icon: TablerName, permission: Permission | null, group: string | null, short?: string];

// Fleetdeck-menuen: tre faste punkter, derefter grupperne "Teknik og drift" og "Opsætning".
const navigation = [
  ["/overview", "Overblik", "home", null, null],
  ["/history", "Historik", "history", null, null],
  ["/schedule", "Ugeplan", "calendar-time", null, null],
  ["/technique", "Teknik", "gauge", "diagnostics", "Teknik og drift"],
  ["/system", "System", "box", "system", "Teknik og drift"],
  ["/home-assistant", "Home Assistant", "bolt", "configure", "Teknik og drift", "HA"],
  ["/diagnostics", "Diagnostik", "tool", "diagnostics", "Teknik og drift"],
  ["/balancing", "Indregulering", "ruler", "configure", "Teknik og drift"],
  ["/updates", "Opdateringer", "refresh", "system", "Teknik og drift"],
  ["/settings", "Indstillinger", "settings", null, "Opsætning"],
] as const satisfies readonly NavItem[];

/** Pages that need a role above a plain user. */
export const ROUTE_PERMISSIONS: Record<string, Permission> = Object.fromEntries(
  navigation.filter(item => item[3] !== null).map(item => [item[0], item[3] as Permission]),
);

const routeTitles: Record<string, [string, string]> = {
  "/overview": ["Overblik", "Aktuel drift og status for dit HCH5 ventilationsanlæg"],
  "/history": ["Historik", "Udvikling i temperaturer, luftkvalitet og drift"],
  "/schedule": ["Ugeplan", "Ugens rytme, perioder og ferie"],
  "/technique": ["Teknik", "Controller, bus og hardwarestatus"],
  "/system": ["System", "Raspberry Pi, services og gateway"],
  "/home-assistant": ["Home Assistant", "Integration og smart-data"],
  "/diagnostics": ["Diagnostik", "Fejlsøgning og rå systemdata"],
  "/balancing": ["Indregulering", "Luftmængder pr. rum, måling og rapport"],
  "/updates": ["Opdateringer", "Software, kanal og failsafe-opdatering"],
  "/settings": ["Indstillinger", "Udseende, brugere, mail og lokale præferencer"],
};

/** Ekstra søgeord, så søgefeltet også finder sider ud fra sensorer og indstillinger. */
const searchWords: Record<string, string> = {
  "/overview": "drift status boost bypass pejs bål køl frikøling eftervarme co2 fugt filter temperatur trin",
  "/history": "graf temperatur co2 omdrejninger genvinding vand fremløb retur",
  "/schedule": "ugeplan perioder nat ferie tid",
  "/technique": "controller bus rs485 hardware modbus sensorer",
  "/system": "raspberry pi wifi netværk strøm services gateway genstart",
  "/home-assistant": "ha integration token mqtt smart auto",
  "/diagnostics": "fejl log hændelser alarmer rapport",
  "/balancing": "luftmængde rum måling rapport indregulering",
  "/updates": "version opdatering beta stable kanal",
  "/settings": "udseende tema sprog brugere mail adgangskode konto eftervarme ventilatortrin",
};

const TABBAR = ["/overview", "/history", "/schedule", "/technique"];

type UnitState = Record<string, unknown>;
type UpdateInfo = { update_available?: boolean; available_version?: string; update?: { running?: boolean } };

function readStored(key: string, fallback: string): string {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}

function initials(name: string | null | undefined) {
  const parts = String(name ?? "").trim().split(/[\s._@-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? parts[0]?.[1] ?? "")).toUpperCase() || "?";
}

function Logo({ size }: { size: number }) {
  return <span className="fd-logo" style={{ width: size, height: size }} aria-hidden="true"><TablerIcon name="propeller" size={Math.round(size * .62)} stroke={2.2}/></span>;
}

/** Fleetdecks menu "Udseende": stil, tema og menuens placering. */
function AppearanceChoices({ skin, theme, nav, onDone }: { skin: Skin; theme: ThemeMode; nav: NavPosition; onDone?: () => void }) {
  const item = (active: boolean, label: ReactNode, onClick: () => void, icon?: TablerName) => (
    <button type="button" role="menuitemradio" aria-checked={active} className="fd-menu-item" onClick={() => { onClick(); onDone?.(); }}>
      {icon ? <TablerIcon name={icon} size={14}/> : <span className="fd-menu-check">{active && <TablerIcon name="check" size={14}/>}</span>}
      <span>{label}</span>
      {icon && <span className="fd-menu-check right">{active && <TablerIcon name="check" size={14}/>}</span>}
    </button>
  );
  return <>
    <div className="lbl">Stil</div>
    {item(skin === "command", <>Command <small>– mørk med sidemenu</small></>, () => chooseSkin("command"))}
    {item(skin === "aurora", <>Aurora <small>– lys med topmenu</small></>, () => chooseSkin("aurora"))}
    <div className="fd-menu-divider"/>
    <div className="lbl">Tema</div>
    {item(theme === "light", "Lyst", () => saveAppearance({ theme: "light" }), "sun")}
    {item(theme === "dark", "Mørkt", () => saveAppearance({ theme: "dark" }), "moon")}
    {item(theme === "system", "Som systemet", () => saveAppearance({ theme: "system" }), "palette")}
    <div className="fd-menu-divider"/>
    <div className="lbl">Menu</div>
    {item(nav === "side", "I siden", () => saveAppearance({ nav: "side" }), "layout-sidebar")}
    {item(nav === "top", "I toppen", () => saveAppearance({ nav: "top" }), "layout-navbar")}
  </>;
}

export function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(() => readStored("hch5-v2-sidebar", "0") === "1");
  const [theme, setTheme] = useState<ThemeMode>(readTheme);
  const [skin, setSkin] = useState<Skin>(readSkin);
  const [nav, setNav] = useState<NavPosition>(readNav);
  const [menu, setMenu] = useState<string | null>(null);
  const [sheet, setSheet] = useState(false);
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const [now, setNow] = useState(new Date());
  const [unit, setUnit] = useState<UnitState>({});
  const unitLabel = unit.unit_generation === "mk2" ? String(unit.unit_model ?? "HCH5 MKII") : "HCH5 MK1";
  const [online, reportPoll] = usePollHealth(2);
  const [version, setVersion] = useState("—");
  const [notice, setNotice] = useState("");
  const [availableUpdate, setAvailableUpdate] = useState<string | null>(null);
  const [auth, setAuth] = useState<AuthStatus>({});
  const [authLoading, setAuthLoading] = useState(true);

  const refreshAuth = useCallback(async () => {
    try { setAuth(await requestJson<AuthStatus>("/api/auth/status", { timeoutMs: 3500 })); }
    catch { /* keep the last known session; the server still enforces every request */ }
    finally { setAuthLoading(false); }
  }, []);
  useEffect(() => { void refreshAuth(); }, [refreshAuth]);
  const session = useMemo(() => ({
    auth, loading: authLoading, refresh: refreshAuth,
    can: (permission: Permission) => sessionCan(auth, permission),
  }), [auth, authLoading, refreshAuth]);
  const haLink = readHaLink(unit);
  const haInfo = haLinkText(haLink);
  const outdoorValue = unit.outdoor_temp ?? unit.outdoor_temperature;
  const outdoorTemp = unit.available === true && typeof outdoorValue === "number" && Number.isFinite(outdoorValue) ? outdoorValue : null;
  const weather = unit.weather && typeof unit.weather === "object" ? unit.weather as WeatherSummary : null;
  // Only shown once Home Assistant is part of the setup (has been seen, or Smart Auto needs it).
  const haVisible = haLink.state !== null && (haLink.state !== "never" || haLink.required);
  const haChipContent = <><span className="live-dot" />HA{haLink.state !== "online" && <small>{haInfo.label}</small>}</>;
  const haChip = !haVisible ? null : sessionCan(auth, "configure")
    ? <NavLink to="/home-assistant" className={`status-chip ha-chip tone-${haInfo.tone}`} title={`Home Assistant: ${haInfo.label}. ${haInfo.detail}`}>{haChipContent}</NavLink>
    : <span className={`status-chip ha-chip tone-${haInfo.tone}`} title={`Home Assistant: ${haInfo.label}. ${haInfo.detail}`}>{haChipContent}</span>;
  const visibleNavigation = navigation.filter(item => item[3] === null || session.can(item[3]));
  const topItems = visibleNavigation.filter(item => item[4] === null);
  const groups = [...new Set(visibleNavigation.map(item => item[4]).filter((group): group is NonNullable<typeof group> => group !== null))]
    .map(group => ({ label: group, items: visibleNavigation.filter(item => item[4] === group) }));
  // Fanebjælken på mobil: fire faste faner (spring dem over, man ikke har adgang til) + "Mere".
  const tabItems = [...TABBAR.map(to => visibleNavigation.find(item => item[0] === to)).filter(Boolean) as NavItem[],
    ...visibleNavigation.filter(item => !TABBAR.includes(item[0]))].slice(0, 4);
  const sheetItems = visibleNavigation.filter(item => !tabItems.includes(item));
  const logout = async () => {
    try { await postJson("/api/auth/logout", {}, auth.csrf ?? undefined); } catch { /* go to login regardless */ }
    window.location.href = "/login";
  };

  const [systemDark, setSystemDark] = useState(() => typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const query = matchMedia("(prefers-color-scheme: dark)");
    const update = () => setSystemDark(query.matches);
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, []);
  const effectiveTheme = theme !== "system" ? theme : systemDark ? "dark" : "light";
  const noticeContext = useMemo(() => ({ notice, setNotice }), [notice]);
  const [title, subtitle] = routeTitles[location.pathname] ?? ["HCH5 Control", "Local ventilation controller"];

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = effectiveTheme;
    root.dataset.skin = skin;
    root.dataset.nav = nav;
    try { localStorage.setItem("hch5-v2-theme", theme); } catch {}
  }, [theme, effectiveTheme, skin, nav]);

  useEffect(() => {
    const syncPreferences = () => {
      setTheme(readTheme());
      setSkin(readSkin());
      setNav(readNav());
      setCollapsed(readStored("hch5-v2-sidebar", "0") === "1");
      document.documentElement.dataset.motion = readStored("hch5-v2-motion", "normal");
    };
    syncPreferences();
    window.addEventListener(PREFERENCES_EVENT, syncPreferences);
    return () => window.removeEventListener(PREFERENCES_EVENT, syncPreferences);
  }, []);

  useEffect(() => {
    document.body.dataset.sidebar = collapsed ? "collapsed" : "expanded";
    try { localStorage.setItem("hch5-v2-sidebar", collapsed ? "1" : "0"); } catch {}
  }, [collapsed]);

  useEffect(() => {
    const tick = window.setInterval(() => setNow(new Date()), 30000);
    return () => window.clearInterval(tick);
  }, []);

  useEffect(() => { setNotice(""); setMenu(null); setSheet(false); setQuery(""); }, [location.pathname]);

  // Menuer lukkes med Escape eller et klik udenfor; Ctrl/Cmd+K åbner søgningen.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setMenu(null); setSheet(false); }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); searchRef.current?.focus(); setMenu("search"); }
    };
    const onDown = (event: MouseEvent) => { if (!(event.target as Element | null)?.closest?.("[data-menu-root]")) setMenu(null); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("mousedown", onDown); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const auth = await requestJson<{ csrf?: string | null }>("/api/auth/status", { timeoutMs: 3500 });
        if (!auth.csrf) return;
        const result = await postJson<UpdateInfo>("/api/admin/action", { action: "check_update", target: "beta" }, auth.csrf);
        if (!cancelled) setAvailableUpdate(result.update?.running ? "Installerer opdatering" : result.update_available ? result.available_version ?? "Opdatering klar" : null);
      } catch {
        // A temporarily unavailable update service must not affect control.
      }
    };
    void check();
    const timer = window.setInterval(() => void check(), 60000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const next = await requestJson<UnitState>("/state.json", { timeoutMs: 2500 });
        if (cancelled) return;
        setUnit(next);
        reportPoll(next.available === true || next.bus_traffic === true);
        const versionValue = next.version ?? next.app_version ?? next.gateway_version;
        if (versionValue) setVersion(String(versionValue));
      } catch {
        if (!cancelled) reportPoll(false);
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [reportPoll]);

  const needle = query.trim().toLowerCase();
  const results = needle ? visibleNavigation.filter(item => `${item[1]} ${routeTitles[item[0]]?.[1] ?? ""} ${searchWords[item[0]] ?? ""}`.toLowerCase().includes(needle)) : [];
  const updateCount = availableUpdate && session.can("system") ? 1 : 0;
  const count = (to: string) => to === "/updates" && updateCount ? <span className="fd-count" data-red>{updateCount}</span> : null;
  const navLink = ([to, label, icon]: NavItem, onClick?: () => void) => (
    <NavLink key={to} to={to} onClick={onClick} className="fd-nav" data-active={location.pathname === to ? "" : undefined} title={collapsed && nav === "side" ? label : undefined}>
      <TablerIcon name={icon} size={18} stroke={1.6}/>
      <span className="fd-nav-label">{label}</span>{count(to)}
    </NavLink>
  );
  const footer = `Dantherm Control ${version !== "—" ? version : ""} · ${unitLabel}`.replace("  ", " ");
  const appearanceIcon = effectiveTheme === "dark" ? "moon" : "sun";

  return (
    <div className="app-shell fd-app" data-collapsed={collapsed && nav === "side" ? "" : undefined}>
      <header className="fd-header topbar-v2 pro-topbar">
        <span className="only-mobile"><span className="fd-mobile-brand"><Logo size={26}/><strong>{title}</strong></span></span>
        {nav === "top"
          ? <>
              <NavLink to="/overview" className="fd-brand only-desktop"><Logo size={26}/><span>Dantherm Control</span></NavLink>
              <nav className="fd-topnav only-desktop sidebar-nav" aria-label="Primær navigation">
                {topItems.map(([to, label]) => <NavLink key={to} to={to} className="fd-top" data-active={location.pathname === to ? "" : undefined}>{label}{count(to)}</NavLink>)}
                {groups.map(group => {
                  const on = group.items.some(item => item[0] === location.pathname);
                  const open = menu === `group:${group.label}`;
                  return <span key={group.label} className="fd-top-group" data-menu-root>
                    <button type="button" className="fd-top" aria-haspopup="menu" aria-expanded={open} data-active={on || open ? "" : undefined} onClick={() => setMenu(open ? null : `group:${group.label}`)}>
                      {group.label}{group.items.some(item => item[0] === "/updates") && updateCount ? <span className="fd-count" data-red>{updateCount}</span> : null}<TablerIcon name="chevron-down" size={14}/>
                    </button>
                    {/* Linkene ligger altid i DOM'en (skjult, når menuen er lukket), så de kan findes med tastatur og test. */}
                    <div className="fd-drop" role="menu" hidden={!open}>
                      {group.items.map(([to, label, icon]) => <NavLink key={to} to={to} role="menuitem" data-active={location.pathname === to ? "" : undefined} onClick={() => setMenu(null)}><TablerIcon name={icon}/>{label}{to === "/updates" && updateCount ? <span className="badge b-blue fd-drop-badge">1</span> : null}</NavLink>)}
                    </div>
                  </span>;
                })}
              </nav>
            </>
          : <div className="fd-search only-desktop" data-menu-root>
              <TablerIcon name="search"/>
              <input ref={searchRef} type="search" value={query} placeholder="Søg efter sider, sensorer, indstillinger…" aria-label="Søg"
                onFocus={() => setMenu("search")} onChange={event => { setQuery(event.target.value); setMenu("search"); }}
                onKeyDown={event => { if (event.key === "Enter" && results[0]) { navigate(results[0][0]); searchRef.current?.blur(); } }}/>
              <kbd>Ctrl K</kbd>
              {menu === "search" && needle && <div className="fd-drop fd-search-results" role="listbox">
                {results.length ? results.map(([to, label, icon]) => <NavLink key={to} to={to} role="option" aria-selected={false} onClick={() => setMenu(null)}><TablerIcon name={icon}/>{label}<small>{routeTitles[to]?.[1]}</small></NavLink>) : <div className="lbl">Ingen sider passer</div>}
              </div>}
            </div>}
        <div className="spacer"/>
        <div className="topbar-actions">
          <TopbarWeather outdoorTemp={outdoorTemp} weather={weather} />
          {availableUpdate && session.can("system") && <NavLink className="topbar-update-tab" to="/updates" title={availableUpdate}><RefreshCw size={15}/><span>{availableUpdate === "Installerer opdatering" ? availableUpdate : "Opdatering klar"}</span>{availableUpdate !== "Installerer opdatering" && <small>{availableUpdate}</small>}</NavLink>}
          <div className="topbar-clock only-desktop"><strong>{now.toLocaleTimeString("da-DK", { hour: "2-digit", minute: "2-digit" })}</strong><span>{now.toLocaleDateString("da-DK", { day: "2-digit", month: "short", year: "numeric" })}</span></div>
          {notice && <div className={`topbar-control-notice${notice.startsWith("Kunne") ? " error" : ""}`} role="status" title={notice}><strong>Seneste ændring</strong><span>{notice}</span></div>}
          <span className={`status-chip fd-activity${online ? " ok" : " muted"}`} title={online ? "Forbindelse til anlægget" : "Venter på data fra anlægget"}><span className="live-dot" /> {online ? "Forbundet" : "Afventer"}</span>
          {haChip}
          <span className="fd-menu-root only-desktop" data-menu-root>
            <button className="fd-icon-btn icon-button" type="button" aria-label="Udseende" aria-haspopup="menu" aria-expanded={menu === "appearance"} onClick={() => setMenu(menu === "appearance" ? null : "appearance")}>
              <TablerIcon name={appearanceIcon} size={17}/>
            </button>
            {menu === "appearance" && <div className="fd-drop fd-drop-right" role="menu"><AppearanceChoices skin={skin} theme={theme} nav={nav} onDone={() => setMenu(null)}/></div>}
          </span>
          {auth.username && <span className="fd-menu-root topbar-user" data-menu-root title={auth.role ? ROLE_NAMES[auth.role].da : undefined}>
            <button type="button" className="fd-avatar" aria-label={`Konto: ${auth.username}`} aria-haspopup="menu" aria-expanded={menu === "user"} onClick={() => setMenu(menu === "user" ? null : "user")}>{initials(auth.username)}</button>
            {menu === "user" && <div className="fd-drop fd-drop-right" role="menu">
              <div className="fd-user-head"><strong>{auth.username}</strong>{auth.role && <small>{ROLE_NAMES[auth.role].da}</small>}</div>
              <div className="fd-menu-divider"/>
              <NavLink to="/settings?section=security" role="menuitem" onClick={() => setMenu(null)}><TablerIcon name="user"/>Min konto</NavLink>
              {auth.enabled !== false && <button type="button" role="menuitem" className="fd-menu-item danger" onClick={() => void logout()}><TablerIcon name="logout" size={14}/><span>Log ud</span></button>}
            </div>}
          </span>}
        </div>
      </header>

      {nav === "side" && <aside className="fd-navbar sidebar" aria-label="Primær navigation">
        <div className="fd-navbar-brand">
          <NavLink to="/overview" className="fd-brand"><Logo size={30}/>{!collapsed && <span>Dantherm Control</span>}</NavLink>
          <button className="fd-collapse icon-button collapse-button" type="button" onClick={() => setCollapsed(value => !value)} aria-label={collapsed ? "Fold menu ud" : "Fold menu sammen"}>
            {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
          </button>
        </div>
        <nav className="grow sidebar-nav">
          {topItems.map(item => navLink(item))}
          {groups.map(group => <div key={group.label} className="fd-nav-group">
            {!collapsed && <div className="fd-group">{group.label}</div>}
            {group.items.map(item => navLink(item))}
          </div>)}
        </nav>
        <div className="fd-foot sidebar-health">
          <span className={`live-dot${online ? "" : " offline"}`} />
          {!collapsed && <span><strong>{online ? "Anlæg online" : "Forbindelse afventer"}</strong><br/>{footer}</span>}
        </div>
      </aside>}

      <SessionContext.Provider value={session}>
        <TopbarNoticeContext.Provider value={noticeContext}>
          <main className="fd-main content-stage">
            <div className="fd-head fd-page-head only-desktop"><div><h1 className="fd-title">{title}</h1><div className="fd-sub">{subtitle}</div></div></div>
            {children}
          </main>
        </TopbarNoticeContext.Provider>
      </SessionContext.Provider>

      <div className="fd-sheet" data-open={sheet ? "" : undefined} aria-hidden={!sheet}>
        {sheetItems.filter(item => item[4] === null).map(item => navLink(item, () => setSheet(false)))}
        {groups.map(group => {
          const items = group.items.filter(item => sheetItems.includes(item));
          return items.length ? <div key={group.label}><div className="fd-group">{group.label}</div>{items.map(item => navLink(item, () => setSheet(false)))}</div> : null;
        })}
        <div className="fd-group">Udseende</div>
        <div className="fd-sheet-appearance" role="menu"><AppearanceChoices skin={skin} theme={theme} nav={nav}/></div>
        {auth.username && <>
          <div className="fd-group">Konto</div>
          <NavLink to="/settings?section=security" className="fd-nav" onClick={() => setSheet(false)}><TablerIcon name="user" size={18} stroke={1.6}/><span>Min konto · {auth.username}</span></NavLink>
          {auth.enabled !== false && <button type="button" className="fd-nav fd-nav-danger" onClick={() => void logout()}><TablerIcon name="logout" size={18} stroke={1.6}/><span>Log ud</span></button>}
        </>}
        <div className="fd-foot">{footer}</div>
      </div>
      <nav className="fd-tabbar" aria-label="Faner">
        {tabItems.map(([to, label, icon, , , short]) => <NavLink key={to} to={to} data-active={location.pathname === to && !sheet ? "" : undefined} onClick={() => setSheet(false)}><span><TablerIcon name={icon}/></span>{short ?? label}</NavLink>)}
        <button type="button" data-active={sheet ? "" : undefined} aria-expanded={sheet} onClick={() => setSheet(value => !value)}><span><TablerIcon name="dots"/>{updateCount && sheetItems.some(item => item[0] === "/updates") ? <b data-red>1</b> : null}</span>Mere</button>
      </nav>
    </div>
  );
}
