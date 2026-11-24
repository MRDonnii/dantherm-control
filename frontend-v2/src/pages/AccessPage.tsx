import { useCallback, useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, Check, Copy, KeyRound, Lock, LogIn, Plus, RefreshCw, Trash2, Users, X } from "lucide-react";
import { postJson, requestJson } from "../lib/api";
import { copyText } from "../lib/clipboard";
import { useSession } from "../lib/session";
import "../styles/panels.css";
import "../styles/access.css";
import { usePullRefresh } from "../lib/pullRefresh";

export type Scope = "read" | "control" | "admin";

export interface ApiKey {
  id: string;
  name: string;
  scope: Scope;
  prefix: string;
  hint: string;
  created: number | null;
  created_by: string;
  moved: boolean;
  moved_from: string;
  lan_only: boolean;
  expires: number | null;
  last_used: number | null;
  last_ip: string;
  uses: number;
  legacy?: boolean;
}

export interface KeyListing {
  keys: ApiKey[];
  rejected: { at: number; ip: string; hint: string; reason: string; name?: string } | null;
  store: "ok" | "legacy";
  store_problem: string;
}

export interface SsoStatus {
  enabled: boolean;
  readable: boolean;
  fingerprint: string;
  created_at: number | null;
  created_by: string;
  peers: string[];
  users: string[];
  last_used: { at: number; ip: string; user: string } | null;
  rejected_at: number | null;
  rejected_ip: string;
  rejected_text: string;
}

interface UserRow { username: string; role: string }

export const SCOPES: { value: Scope; label: string; detail: string }[] = [
  { value: "read", label: "Kun læse", detail: "Se status og målinger. Kan ikke ændre noget." },
  { value: "control", label: "Læse og styre", detail: "Også styre driften, fx ventilationstrin, bypass og ugeplan. Det, Home Assistant skal bruge." },
  { value: "admin", label: "Administrator", detail: "Også alle indstillinger for anlægget. Brug den kun, hvis programmet virkelig skal." },
];
const SCOPE_LABEL: Record<Scope, string> = { read: "Kun læse", control: "Læse og styre", admin: "Administrator" };
const EXPIRY = [{ days: 0, label: "Aldrig" }, { days: 30, label: "Om 30 dage" }, { days: 90, label: "Om 90 dage" }, { days: 365, label: "Om et år" }];
const UNUSED_DAYS = 90;

const errorText = (error: unknown) => error instanceof Error ? error.message : "Ukendt fejl";

export function dayText(at: number | null | undefined) {
  if (!at) return "—";
  return new Date(at * 1000).toLocaleDateString("da-DK", { day: "numeric", month: "short", year: "numeric" });
}

/** "for 12 sek.", "for 4 min.", "i dag 08:14", "i går 22:10", "for 94 dage siden". */
export function agoText(at: number | null | undefined, now = Date.now() / 1000) {
  if (!at) return "Aldrig brugt endnu";
  const seconds = Math.max(0, Math.round(now - at));
  if (seconds < 60) return `for ${seconds} sek.`;
  if (seconds < 3600) return `for ${Math.round(seconds / 60)} min.`;
  const then = new Date(at * 1000), today = new Date(now * 1000);
  const clock = then.toLocaleTimeString("da-DK", { hour: "2-digit", minute: "2-digit" });
  const days = Math.floor((new Date(today.toDateString()).getTime() - new Date(then.toDateString()).getTime()) / 86400000);
  if (days <= 0) return `i dag ${clock}`;
  if (days === 1) return `i går ${clock}`;
  return `for ${days} dage siden`;
}

function Dialog({ title, icon, tone = "blue", onClose, children, wide }: { title: string; icon: ReactNode; tone?: "blue" | "green" | "red"; onClose?: () => void; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    if (!onClose) return;
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [onClose]);
  // On body, so the page's own stacking (animations, fixed bars) never covers it.
  return createPortal(<div className="ak-backdrop" role="presentation">
    <section className={`ak-dialog surface${wide ? " is-wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby="ak-dialog-title">
      <header className="ak-dialog-head">
        <span className={`ak-icon tone-${tone}`}>{icon}</span>
        <h2 id="ak-dialog-title">{title}</h2>
        {onClose && <button type="button" className="ak-close" aria-label="Luk" onClick={onClose}><X size={18}/></button>}
      </header>
      {children}
    </section>
  </div>, document.body);
}

function NewKeyDialog({ busy, onCancel, onCreate }: { busy: boolean; onCancel: () => void; onCreate: (draft: { name: string; scope: Scope; lan_only: boolean; expires_days: number }) => void }) {
  const [name, setName] = useState("");
  const [scope, setScope] = useState<Scope>("read");
  const [lan, setLan] = useState(true);
  const [days, setDays] = useState(0);
  return <Dialog title="Ny API-nøgle" icon={<KeyRound size={18}/>} onClose={onCancel}>
    <form className="ak-form" onSubmit={event => { event.preventDefault(); if (name.trim()) onCreate({ name: name.trim(), scope, lan_only: lan, expires_days: days }); }}>
      <label className="ak-field">
        <span>Navn</span>
        <input autoFocus value={name} maxLength={48} placeholder="Fx Kontrolrum" onChange={event => setName(event.target.value)}/>
        <small>Navnet på programmet, der skal bruge nøglen. Det står i listen og i loggen.</small>
      </label>
      <fieldset className="ak-scopes">
        <legend>Rettighed</legend>
        {SCOPES.map(item => <label key={item.value} className="ak-scope" data-selected={scope === item.value ? "" : undefined}>
          <input type="radio" name="scope" value={item.value} checked={scope === item.value} onChange={() => setScope(item.value)}/>
          <span><strong>{item.label}</strong><small>{item.detail}</small></span>
        </label>)}
      </fieldset>
      <div className="ak-form-row">
        <label className="ak-switch">
          <input type="checkbox" role="switch" checked={lan} onChange={event => setLan(event.target.checked)}/>
          <span className="ak-switch-track" aria-hidden="true"/>
          <span><strong>Kun fra hjemmenetværket</strong><small>Afvis kald udefra, også gennem en tunnel eller proxy.</small></span>
        </label>
        <label className="ak-field">
          <span>Udløber</span>
          <select value={days} onChange={event => setDays(Number(event.target.value))}>
            {EXPIRY.map(item => <option key={item.days} value={item.days}>{item.label}</option>)}
          </select>
        </label>
      </div>
      <div className="ak-actions">
        <button type="button" className="secondary-action" onClick={onCancel}>Annullér</button>
        <button type="submit" className="primary-action" disabled={busy || !name.trim()}><Plus size={15}/>Lav nøglen</button>
      </div>
    </form>
  </Dialog>;
}

export function ShownOnceDialog({ title, secret, meta, usage, savedText, onDone }: { title: string; secret: string; meta: string; usage: string; savedText: string; onDone: () => void }) {
  const [saved, setSaved] = useState(false);
  const [copied, setCopied] = useState("");
  return <Dialog title={title} icon={<Check size={18}/>} tone="green">
    <div className="ak-once-warning" role="alert">
      <AlertTriangle size={20}/>
      <div><strong>Kopiér nøglen nu – den vises kun én gang</strong><p>Bagefter kan hverken du eller Dantherm Control se den igen. Mister du den, sletter du den og laver en ny.</p></div>
    </div>
    <div className="ak-field"><span>Nøgle</span><small>{meta}</small></div>
    <div className="ak-secret-row">
      <code className="ak-secret" data-testid="shown-key">{secret}</code>
      <button type="button" className="primary-action" onClick={() => void copyText(secret).then(ok => setCopied(ok ? "Kopieret" : "Markér nøglen og kopiér selv"))}><Copy size={15}/>Kopiér</button>
    </div>
    {copied && <p className="ak-copied" role="status">{copied}</p>}
    <div className="ak-field"><span>Sådan bruges den</span></div>
    <code className="ak-usage">{usage}</code>
    <label className="ak-check"><input type="checkbox" checked={saved} onChange={event => setSaved(event.target.checked)}/>{savedText}</label>
    <div className="ak-actions"><button type="button" className="primary-action" disabled={!saved} onClick={onDone}>Færdig</button></div>
  </Dialog>;
}

function ConfirmDialog({ title, text, action, busy, onCancel, onConfirm }: { title: string; text: string; action: string; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  return <Dialog title={title} icon={<Trash2 size={18}/>} tone="red" onClose={onCancel}>
    <p className="ak-confirm-text">{text}</p>
    <div className="ak-actions">
      <button type="button" className="secondary-action" onClick={onCancel}>Annullér</button>
      <button type="button" className="ak-danger" disabled={busy} onClick={onConfirm}><Trash2 size={15}/>{action}</button>
    </div>
  </Dialog>;
}

function SsoUsersDialog({ sso, users, busy, onClose, onUser, onPeers }: { sso: SsoStatus; users: UserRow[]; busy: boolean; onClose: () => void; onUser: (name: string, allowed: boolean) => void; onPeers: (ips: string) => void }) {
  const [ips, setIps] = useState(sso.peers.join(", "));
  return <Dialog title="Hvem må logge ind fra Kontrolrum?" icon={<Users size={18}/>} onClose={onClose}>
    <div className="ak-user-list">
      {users.map(user => <label key={user.username} className="ak-check">
        <input type="checkbox" disabled={busy} checked={sso.users.includes(user.username.toLowerCase())} onChange={event => onUser(user.username, event.target.checked)}/>
        <span><strong>{user.username}</strong> <small>{user.role === "admin" ? "Administrator" : user.role === "technician" ? "Tekniker" : "Bruger"}</small></span>
      </label>)}
    </div>
    <form className="ak-form" onSubmit={event => { event.preventDefault(); onPeers(ips); }}>
      <label className="ak-field">
        <span>Kontrolrums adresse</span>
        <input value={ips} placeholder="Fx 10.0.0.10" onChange={event => setIps(event.target.value)}/>
        <small>Login godtages kun fra disse IP-adresser. Adskil flere med komma.</small>
      </label>
      <div className="ak-actions">
        <button type="submit" className="secondary-action" disabled={busy}>Gem adresse</button>
        <button type="button" className="primary-action" onClick={onClose}>Færdig</button>
      </div>
    </form>
  </Dialog>;
}

function SsoCard({ sso, users, error, busy, onChooseUsers, onNewKey, onOff }: { sso: SsoStatus | null; users: UserRow[]; error: string; busy: boolean; onChooseUsers: () => void; onNewKey: () => void; onOff: () => void }) {
  const allowed = sso ? users.filter(user => sso.users.includes(user.username.toLowerCase())).length : 0;
  return <article className="surface ak-card" aria-labelledby="ak-sso-title">
    <header className="ak-card-head">
      <span className="ak-icon tone-purple"><LogIn size={18}/></span>
      <h2 id="ak-sso-title">Login fra Kontrolrum (SSO)</h2>
      {sso && <span className={`ak-badge ${sso.enabled ? "is-green" : "is-muted"}`}>{sso.enabled ? "Slået til" : "Slået fra"}</span>}
      <div className="ak-head-actions">
        <button type="button" className="secondary-action" disabled={!sso || busy} onClick={onChooseUsers}><Users size={15}/>Vælg brugere</button>
        <button type="button" className="secondary-action" disabled={!sso || busy} onClick={onNewKey}><RefreshCw size={15}/>{sso?.enabled ? "Lav ny SSO-nøgle" : "Lav SSO-nøgle"}</button>
        {sso?.enabled && <button type="button" className="ak-danger" disabled={busy} onClick={onOff}>Slå fra</button>}
      </div>
    </header>
    <p className="ak-lead">Browser-login for personer. Nøglen deles kun med Kontrolrum og vises én gang, når den laves. Her ses kun dens fingeraftryk.</p>
    {error && <p className="ak-problem" role="alert">{error}</p>}
    {sso && <div className="ak-tiles">
      <div className="ak-tile"><span>Nøgle</span><strong className="mono">{sso.enabled ? `SSO · ${sso.fingerprint}` : "Ingen"}</strong><small>fingeraftryk, ikke selve nøglen</small></div>
      <div className="ak-tile"><span>Oprettet</span><strong>{dayText(sso.created_at)}</strong><small>{sso.created_by ? `af ${sso.created_by}` : "—"}</small></div>
      <div className="ak-tile"><span>Sidst brugt</span><strong>{sso.last_used ? agoText(sso.last_used.at) : "Ikke brugt endnu"}</strong><small>{sso.last_used ? `af ${sso.last_used.user} fra ${sso.last_used.ip}` : "—"}</small></div>
      <div className="ak-tile"><span>Brugere</span><strong>{allowed} af {users.length}</strong><small>må logge ind fra Kontrolrum</small></div>
    </div>}
    {sso?.rejected_ip && <p className="ak-foot"><AlertTriangle size={15}/>Sidst afvist: {agoText(sso.rejected_at)} fra <span className="mono">{sso.rejected_ip}</span> ({sso.rejected_text})</p>}
  </article>;
}

function scopeBadge(scope: Scope) {
  return <span className={`ak-badge scope-${scope}`}>{SCOPE_LABEL[scope]}</span>;
}

function KeysCard({ listing, error, onNew, onDelete }: { listing: KeyListing | null; error: string; onNew: () => void; onDelete: (key: ApiKey) => void }) {
  const now = Date.now() / 1000;
  const keys = listing?.keys ?? [];
  return <article className="surface ak-card" aria-labelledby="ak-keys-title">
    <header className="ak-card-head">
      <span className="ak-icon tone-blue"><KeyRound size={18}/></span>
      <h2 id="ak-keys-title">API-nøgler</h2>
      <span className="ak-count">{keys.length}</span>
      <div className="ak-head-actions">
        <button type="button" className="primary-action" disabled={!listing || listing.store !== "ok"} onClick={onNew}><Plus size={15}/>Ny API-nøgle</button>
      </div>
    </header>
    <p className="ak-lead">Til programmer. Hver nøgle har sit eget navn og sin egen rettighed og kan slettes alene.</p>
    {error && <p className="ak-problem" role="alert">{error}</p>}
    {listing?.store === "legacy" && <p className="ak-problem" role="alert">Nøglerne kan ikke gemmes lige nu ({listing.store_problem}). Den gamle nøgle virker som før, så Home Assistant styrer stadig anlægget.</p>}
    <div className="ak-table" role="table" aria-label="API-nøgler">
      <div className="ak-row ak-row-head" role="row">
        <span role="columnheader">Navn</span><span role="columnheader">Rettighed</span><span role="columnheader">Oprettet</span><span role="columnheader">Sidst brugt</span><span role="columnheader" className="sr-only">Slet</span>
      </div>
      {keys.length === 0 && listing && <p className="ak-empty">Ingen nøgler endnu. Home Assistant skal have en nøgle med rettigheden Læse og styre.</p>}
      {keys.map(key => {
        const unused = key.last_used ? now - key.last_used > UNUSED_DAYS * 86400 : !!key.created && now - key.created > UNUSED_DAYS * 86400;
        const expired = !!key.expires && key.expires <= now;
        return <div key={key.id} className="ak-row" role="row" data-unused={unused ? "" : undefined}>
          <span role="cell" className="ak-name">
            <strong>{key.name}{key.moved && <span className="ak-badge is-green">Flyttet</span>}</strong>
            <small className="mono">{key.prefix}…{key.hint}{!key.lan_only && <em> · også udefra</em>}</small>
          </span>
          <span role="cell" data-label="Rettighed">{scopeBadge(key.scope)}</span>
          <span role="cell" data-label="Oprettet" className="ak-two">
            <span>{dayText(key.created)}</span>
            <small>{key.moved ? "flyttet fra den gamle nøgle" : key.created_by || "—"}{key.expires ? ` · ${expired ? "udløbet" : `udløber ${dayText(key.expires)}`}` : ""}</small>
          </span>
          <span role="cell" data-label="Sidst brugt" className="ak-two">
            <span className={key.last_used ? "" : "is-muted"}>{key.last_used && <i className={`ak-dot ${unused ? "is-orange" : "is-green"}`}/>}{agoText(key.last_used, now)}{unused && <span className="ak-badge is-orange">Ubrugt i {UNUSED_DAYS} dage</span>}</span>
            {key.last_ip && <small className="mono">{key.last_ip}{key.last_ip === "127.0.0.1" ? " · denne Pi" : ""}</small>}
          </span>
          <span role="cell" className="ak-del">
            {!key.legacy && <button type="button" className={`icon-button${unused ? " is-danger" : ""}`} aria-label={`Slet ${key.name}`} title="Slet" onClick={() => onDelete(key)}><Trash2 size={16}/></button>}
          </span>
        </div>;
      })}
    </div>
    {listing?.rejected && <p className="ak-foot"><AlertTriangle size={15}/>Sidst afvist: {agoText(listing.rejected.at, now)} fra <span className="mono">{listing.rejected.ip}</span> ({listing.rejected.name ? `${listing.rejected.name}: ` : ""}{listing.rejected.reason})</p>}
    <p className="ak-foot"><Lock size={15}/>Nøglerne gemmes kun som SHA-256 og kan ikke vises igen. Send dem som <code>Authorization: Bearer dth_…</code>, aldrig i adressen. Mistet nøgle: slet den og lav en ny.</p>
  </article>;
}

export function AccessPage() {
  const { auth } = useSession();
  const csrf = auth.csrf ?? undefined;
  const [listing, setListing] = useState<KeyListing | null>(null);
  const [keysError, setKeysError] = useState("");
  const [sso, setSso] = useState<SsoStatus | null>(null);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [ssoError, setSsoError] = useState("");
  const [busy, setBusy] = useState(false);
  const [dialog, setDialog] = useState<null | { kind: "new" } | { kind: "shown"; name: string; key: string; meta: string } | { kind: "delete"; key: ApiKey } | { kind: "sso-users" } | { kind: "sso-shown"; key: string } | { kind: "sso-new" } | { kind: "sso-off" }>(null);

  const loadKeys = useCallback(async () => {
    try { setListing(await requestJson<KeyListing>("/api/access/keys")); setKeysError(""); }
    catch (error) { setKeysError(`Kunne ikke hente nøglerne: ${errorText(error)}`); }
  }, []);
  const loadSso = useCallback(async () => {
    const [status, list] = await Promise.allSettled([requestJson<SsoStatus>("/api/auth/kontrolrum/status"), requestJson<{ users: UserRow[] }>("/api/users")]);
    if (status.status === "fulfilled") { setSso(status.value); setSsoError(""); } else setSsoError(`Login fra Kontrolrum er ikke tilgængeligt: ${errorText(status.reason)}`);
    if (list.status === "fulfilled") setUsers(list.value.users ?? []);
  }, []);
  usePullRefresh(() => Promise.all([loadKeys(), loadSso()]));
  useEffect(() => { void loadKeys(); void loadSso(); const timer = setInterval(() => void loadKeys(), 15000); return () => clearInterval(timer); }, [loadKeys, loadSso]);

  const run = async (work: () => Promise<void>, fail: (message: string) => void) => {
    setBusy(true);
    try { await work(); } catch (error) { fail(errorText(error)); } finally { setBusy(false); }
  };
  const create = (draft: { name: string; scope: Scope; lan_only: boolean; expires_days: number }) => run(async () => {
    const result = await postJson<KeyListing & { key: string }>("/api/access/keys/create", draft, csrf);
    setListing(result);
    const expiry = EXPIRY.find(item => item.days === draft.expires_days)?.label.toLowerCase() ?? "aldrig";
    setDialog({ kind: "shown", name: draft.name, key: result.key, meta: `${SCOPE_LABEL[draft.scope]} · ${draft.lan_only ? "kun fra hjemmenetværket" : "også udefra"} · udløber ${expiry}` });
  }, message => { setKeysError(message); setDialog(null); });
  const remove = (key: ApiKey) => run(async () => {
    setListing(await postJson<KeyListing>("/api/access/keys/delete", { id: key.id }, csrf));
    setDialog(null);
  }, message => { setKeysError(message); setDialog(null); });
  const ssoAction = (path: string, body: Record<string, unknown> = {}) => run(async () => {
    const result = await postJson<{ status: SsoStatus; key?: string }>(`/api/auth/kontrolrum/${path}`, body, csrf);
    setSso(result.status);
    if (result.key) setDialog({ kind: "sso-shown", key: result.key });
    else if (path === "off") setDialog(null);
  }, message => setSsoError(message));

  const short = (key: string) => key.length > 16 ? `${key.slice(0, 12)}…${key.slice(-4)}` : key;

  return <section className="dashboard-overview page-enter ak-page">
    <h1 className="sr-only">Adgang for programmer</h1>
    <div className="ak-intro">
      <div className="surface ak-intro-card"><span className="ak-icon tone-purple"><LogIn size={18}/></span><div><strong>Til personer: Login fra Kontrolrum</strong><p>Du er logget ind i Kontrolrum og trykker dig herind. Du får dine egne rettigheder, og der står dit navn i loggen.</p></div></div>
      <div className="surface ak-intro-card"><span className="ak-icon tone-blue"><KeyRound size={18}/></span><div><strong>Til programmer: API-nøgler</strong><p>Home Assistant, Kontrolrum-kort, scripts. Én nøgle pr. program, så du kan slette én, uden at de andre holder op.</p></div></div>
    </div>
    <SsoCard sso={sso} users={users} error={ssoError} busy={busy}
      onChooseUsers={() => setDialog({ kind: "sso-users" })}
      onNewKey={() => sso?.enabled ? setDialog({ kind: "sso-new" }) : void ssoAction("key")}
      onOff={() => setDialog({ kind: "sso-off" })}/>
    <KeysCard listing={listing} error={keysError} onNew={() => setDialog({ kind: "new" })} onDelete={key => setDialog({ kind: "delete", key })}/>

    {dialog?.kind === "new" && <NewKeyDialog busy={busy} onCancel={() => setDialog(null)} onCreate={draft => void create(draft)}/>}
    {dialog?.kind === "shown" && <ShownOnceDialog title={`Nøglen “${dialog.name}” er lavet`} secret={dialog.key} meta={dialog.meta}
      usage={`Authorization: Bearer ${short(dialog.key)}`} savedText="Jeg har gemt nøglen i programmet eller et sikkert sted" onDone={() => setDialog(null)}/>}
    {dialog?.kind === "delete" && <ConfirmDialog title={`Slet nøglen “${dialog.key.name}”?`} busy={busy} action="Slet nøglen"
      text={`${dialog.key.name} kan ikke længere tale med Dantherm Control. De andre nøgler virker som før.${dialog.key.moved ? " Den gamle nøgle fra før version 1.9 holder også op med at virke." : ""}`}
      onCancel={() => setDialog(null)} onConfirm={() => void remove(dialog.key)}/>}
    {dialog?.kind === "sso-users" && sso && <SsoUsersDialog sso={sso} users={users} busy={busy} onClose={() => setDialog(null)}
      onUser={(name, allowed) => void ssoAction("user", { username: name, allowed })} onPeers={ips => void ssoAction("peers", { ips })}/>}
    {dialog?.kind === "sso-new" && <ConfirmDialog title="Lav en ny SSO-nøgle?" busy={busy} action="Lav ny nøgle"
      text="Den gamle SSO-nøgle holder op med at virke med det samme. Login fra Kontrolrum virker først igen, når den nye nøgle er sat ind i Kontrolrum."
      onCancel={() => setDialog(null)} onConfirm={() => void ssoAction("key")}/>}
    {dialog?.kind === "sso-off" && <ConfirmDialog title="Slå login fra Kontrolrum fra?" busy={busy} action="Slå fra"
      text="SSO-nøglen slettes. Ingen kan logge ind fra Kontrolrum, før der laves en ny. API-nøglerne virker som før."
      onCancel={() => setDialog(null)} onConfirm={() => void ssoAction("off")}/>}
    {dialog?.kind === "sso-shown" && <ShownOnceDialog title="SSO-nøglen er lavet" secret={dialog.key} meta="Login fra Kontrolrum · sæt den ind i Kontrolrum"
      usage="Sæt den ind i Kontrolrum ved Dantherm Control" savedText="Jeg har sat nøglen ind i Kontrolrum" onDone={() => setDialog(null)}/>}
  </section>;
}
