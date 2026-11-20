import { useCallback, useEffect, useState } from "react";
import { Copy, KeyRound } from "lucide-react";
import { NavLink } from "react-router-dom";
import { requestJson } from "../lib/api";
import { copyText } from "../lib/clipboard";
import { useSession } from "../lib/session";

interface Integration {
  configured: boolean;
  control_keys: number;
  keys: number;
  store: "ok" | "legacy";
  port: number;
  address: string | null;
}

/** The address Home Assistant needs, and whether a key that can control the unit exists.
 *  Keys themselves live on "Adgang for programmer" and are never shown again. */
export function IntegrationTokenCard() {
  const { can } = useSession();
  const [info, setInfo] = useState<Integration | null>(null);
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    try { setInfo(await requestJson<Integration>("/api/integration")); }
    catch (error) { setNotice(`Kunne ikke hente API-oplysninger: ${error instanceof Error ? error.message : "?"}`); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const copy = async (value: string) => setNotice(await copyText(value) ? "Adressen er kopieret." : "Kunne ikke kopiere. Markér adressen og kopiér selv.");
  const here = `${window.location.protocol}//${window.location.hostname}:${info?.port ?? window.location.port}`;
  const pi = info?.address ? `http://${info.address}:${info.port}` : null;
  const addresses = [...new Set([here, pi].filter(Boolean) as string[])];
  const keyText = !info ? "—"
    : info.control_keys > 0 ? `${info.control_keys} ${info.control_keys === 1 ? "nøgle" : "nøgler"} kan styre anlægget`
    : "Ingen nøgle, der kan styre anlægget";

  return <article className="surface panel-card panel-span-2 integration-card">
    <div className="pro-card-head compact"><div><h2>Forbindelse fra Home Assistant</h2><p>Det Home Assistant-integrationen skal bruge for at styre anlægget</p></div><KeyRound size={20}/></div>
    <div className="integration-fields">
      <div className="integration-field">
        <span>Controllerens adresse</span>
        {addresses.map(address => <div key={address} className="integration-value"><code>{address}</code><button type="button" className="icon-button" aria-label={`Kopiér ${address}`} title="Kopiér" onClick={() => void copy(address)}><Copy size={15}/></button></div>)}
        <small>Port {info?.port ?? "—"} bruges til styring. Rå data fra bussen ligger på port 4196.</small>
      </div>
      <div className="integration-field">
        <span>API-nøgle</span>
        <div className="integration-value"><code>{keyText}</code></div>
        <small>{info?.store === "legacy" ? "Den gamle nøgle virker som før." : "Nøgler gemmes kun som SHA-256 og kan ikke vises igen."}</small>
      </div>
    </div>
    <div className="diag-actions">
      {can("users")
        ? <NavLink className="secondary-action" to="/access"><KeyRound size={14}/>Adgang for programmer</NavLink>
        : <span className="settings-inline-notice">En administrator laver nøglen under Adgang for programmer.</span>}
      {notice && <span className="settings-inline-notice" role="status">{notice}</span>}
    </div>
    <p className="settings-help">I Home Assistant: indtast adressen og en API-nøgle i HCH5-integrationens indstillinger. Lav nøglen under Opsætning → Adgang for programmer → Ny API-nøgle → Læse og styre. Den vises kun én gang. Har du mistet den, så lav en ny og slet den gamle.</p>
  </article>;
}
