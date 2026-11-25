import { RoomAirOverview } from "../components/RoomAirOverview";
import { useCallback, useEffect, useMemo, useState } from "react";
import { DoorOpen, Plus, RotateCcw, Save, Trash2, Wand2 } from "lucide-react";
import { RoomSensorQuality, roomQuality, qualityLabel } from "../components/RoomSensorQuality";
import { postJson, requestJson } from "../lib/api";
import {
  KIND_LABELS, KIND_UNITS, MAX_ROOMS, PRIORITIES, PRIORITY_LABELS, ROOM_TYPES, SENSOR_KINDS, TYPE_HELP, TYPE_LABELS,
  addAllFromCatalog, blankRoom, isIndoor, liveValues, roomProblems, sensorGroups, sensorName, toPayload,
  withArea, withType, withUnitSensor,
  type CatalogArea, type Room, type RoomsView, type SensorKind,
} from "../lib/rooms";
import "../styles/panels.css"; import "../styles/management.css"; import "../styles/rooms.css";
import { type DraftCheck, keepDraftNow, usePullRefresh } from "../lib/pullRefresh";

type State = Record<string, unknown>;

function fmt(value: number | undefined, kind: SensorKind) {
  if (value === undefined) return null;
  const digits = kind === "temperature" ? 1 : 0;
  return `${value.toLocaleString("da-DK", { maximumFractionDigits: digits, minimumFractionDigits: digits })} ${KIND_UNITS[kind]}`;
}

const numberOrUndefined = (text: string) => (text.trim() === "" ? undefined : Number(text.replace(",", ".")));

/** Values the Pi uses for the room right now, as small chips. */
function LiveChips({ state, room }: { state: State; room: Room }) {
  const live = liveValues(state, room.name);
  const quality = roomQuality(state, room.name);
  const chips = SENSOR_KINDS.map(kind => [kind, fmt(live?.[kind], kind)] as const).filter(([, text]) => text);
  if (!chips.length) return <><span className="room-live room-live-empty">{room.type === "excluded" ? "Ignoreres" : "Ingen data lige nu"}</span><RoomSensorQuality state={state} name={room.name}/></>;
  return <><span className="room-live">{chips.map(([kind, text]) => <span key={kind} className="room-chip" data-kind={kind} title={quality[kind] ? `${KIND_LABELS[kind]} · ${qualityLabel(quality[kind])}` : KIND_LABELS[kind]}>{text}</span>)}
    {live?.unit_sensor && <span className="room-chip room-chip-unit" title="Udfyldt med anlæggets egen føler">Anlæg</span>}</span><RoomSensorQuality state={state} name={room.name}/></>;
}

function SensorPicker({ kind, room, catalog, onChange }: { kind: SensorKind; room: Room; catalog: CatalogArea[]; onChange: (value: string | null) => void }) {
  const groups = sensorGroups(catalog, kind, room.area_id, room.type === "outdoor");
  const current = room.sensors[kind];
  const known = !current || groups.some(group => group.sensors.some(sensor => sensor.entity_id === current));
  return <label>{KIND_LABELS[kind]}
    <select aria-label={`${room.name} ${KIND_LABELS[kind]}`} value={current ?? ""} onChange={e => onChange(e.target.value || null)}>
      <option value="">Ingen</option>
      {!known && <option value={current ?? ""}>{current} (ikke i Home Assistant nu)</option>}
      {groups.map(group => <optgroup key={group.label} label={group.label}>
        {group.sensors.map(sensor => <option key={sensor.entity_id} value={sensor.entity_id}>{sensor.name}</option>)}
      </optgroup>)}
    </select>
  </label>;
}

function RoomCard({ room, catalog, state, bathroom, onChange, onUnitSensor, onRemove }: {
  room: Room; catalog: CatalogArea[]; state: State; bathroom: { rh: number; hysteresis: number; level: number; maxLevel: number };
  onChange: (room: Room) => void; onUnitSensor: (on: boolean) => void; onRemove: () => void;
}) {
  const indoor = isIndoor(room.type);
  const excluded = room.type === "excluded";
  const set = (patch: Partial<Room>) => onChange({ ...room, ...patch });
  const setSensor = (kind: SensorKind, value: string | null) => set({ sensors: { ...room.sensors, [kind]: value } });
  return <article className="surface panel-card room-setup-card" data-type={room.type} data-off={!room.control || excluded ? "" : undefined}>
    <header className="room-setup-head">
      <input className="room-name" aria-label="Rummets navn" maxLength={64} value={room.name} onChange={e => set({ name: e.target.value })}/>
      <label className="room-toggle" title={indoor ? "Rummet styrer ventilationen" : "Kun måling eller ignoreret"}>
        <input type="checkbox" role="switch" aria-label={`${room.name} styrer ventilationen`} checked={indoor && room.control} disabled={!indoor} onChange={e => set({ control: e.target.checked })}/>
        <span className="room-toggle-track" aria-hidden="true"/>
        <span>{!indoor ? (excluded ? "Ignoreres" : "Kun måling") : room.control ? "Styrer" : "Kun visning"}</span>
      </label>
    </header>
    <LiveChips state={state} room={room}/>
    <div className="settings-grid room-setup-grid">
      <label>Rumtype
        <select aria-label={`${room.name} rumtype`} value={room.type} onChange={e => onChange(withType(room, e.target.value as Room["type"]))}>
          {ROOM_TYPES.map(type => <option key={type} value={type}>{TYPE_LABELS[type]}</option>)}
        </select>
        <small className="settings-field-help">{TYPE_HELP[room.type]}</small>
      </label>
      <label>Prioritet
        <select aria-label={`${room.name} prioritet`} value={room.priority} disabled={!indoor} onChange={e => set({ priority: e.target.value as Room["priority"] })}>
          {PRIORITIES.map(priority => <option key={priority} value={priority}>{PRIORITY_LABELS[priority]}</option>)}
        </select>
        <small className="settings-field-help">Høj og kritisk reagerer tidligere. Meget dårlig luft overses aldrig.</small>
      </label>
      <label>Område i Home Assistant
        <select aria-label={`${room.name} område`} value={room.area_id ?? ""} onChange={e => onChange(withArea(room, catalog.find(area => area.area_id === e.target.value)))}>
          <option value="">Intet område</option>
          {room.area_id && !catalog.some(area => area.area_id === room.area_id) && <option value={room.area_id}>{room.area_name ?? room.area_id}</option>}
          {catalog.map(area => <option key={area.area_id} value={area.area_id}>{area.area_name}</option>)}
        </select>
        <small className="settings-field-help">Vælger områdets bedste følere automatisk. Du kan ændre dem herunder.</small>
      </label>
      {!excluded && SENSOR_KINDS.map(kind => <SensorPicker key={kind} kind={kind} room={room} catalog={catalog} onChange={value => setSensor(kind, value)}/>)}
      {room.type === "bathroom" && <>
        <label>Fugt start<input inputMode="decimal" aria-label={`${room.name} fugt start`} placeholder={String(bathroom.rh)} value={room.rh_setpoint ?? ""} onChange={e => set({ rh_setpoint: numberOrUndefined(e.target.value) })}/><span>%</span></label>
        <label>Hysterese<input inputMode="decimal" aria-label={`${room.name} hysterese`} placeholder={String(bathroom.hysteresis)} value={room.rh_hysteresis ?? ""} onChange={e => set({ rh_hysteresis: numberOrUndefined(e.target.value) })}/><span>%</span></label>
        <label>Højeste trin<input inputMode="numeric" aria-label={`${room.name} højeste trin`} placeholder={String(bathroom.level)} value={room.max_level ?? ""} onChange={e => set({ max_level: numberOrUndefined(e.target.value) })}/><small className="settings-field-help">Tomme felter bruger de fælles værdier under Indstillinger → Fugt og tør luft.</small></label>
      </>}
      {indoor && <label className="check-row room-unit-row"><input type="checkbox" checked={room.unit_sensor} onChange={e => onUnitSensor(e.target.checked)}/> Anlæggets egen føler sidder her
        <small className="settings-field-help">Anlæggets rumføler (T5 og fugt) udfylder de målinger, rummet mangler fra Home Assistant.</small></label>}
    </div>
    <footer className="room-setup-foot">
      <small>{SENSOR_KINDS.map(kind => sensorName(catalog, room.sensors[kind])).filter(Boolean).join(" · ") || (excluded ? "Ingen følere bruges" : "Ingen følere valgt")}</small>
      <button type="button" className="danger-outline" onClick={onRemove} aria-label={`Fjern ${room.name}`}><Trash2 size={15}/>Fjern</button>
    </footer>
  </article>;
}

export function RoomsPage() {
  const [view, setView] = useState<RoomsView | null>(null);
  const [rooms, setRooms] = useState<Room[]>([]);
  const [saved, setSaved] = useState<string>("[]");
  const [state, setState] = useState<State>({});
  const [csrf, setCsrf] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  // keepDraft: unsaved room edits stay; only the catalog and the session are fetched again.
  const load = useCallback(async (keepDraft: DraftCheck = false) => {
    try {
      const [next, auth] = await Promise.all([
        requestJson<RoomsView>("/api/controller/rooms"),
        requestJson<{ csrf?: string | null }>("/api/auth/status"),
      ]);
      setView(next); setCsrf(auth.csrf ?? ""); setError("");
      if (!keepDraftNow(keepDraft)) { setRooms(next.rooms ?? []); setSaved(JSON.stringify(next.rooms ?? [])); }
    } catch (e) { setError(e instanceof Error ? e.message : "Rummene kunne ikke hentes"); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    let stop = false;
    const poll = async () => {
      try { const next = await requestJson<State>("/api/controller/state?compact=1"); if (!stop) setState(next); } catch { /* live values are optional */ }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 10000);
    return () => { stop = true; window.clearInterval(timer); };
  }, []);

  const catalog = view?.catalog ?? [];
  const dirty = JSON.stringify(rooms) !== saved;
  usePullRefresh(({ keepDraft }) => load(keepDraft), { dirty });
  const problems = useMemo(() => roomProblems(rooms), [rooms]);
  const maxLevel = Number(state.max_level) || 6;
  const bathroom = { rh: Number(state.bathroom_rh_setpoint) || 65, hysteresis: Number(state.bathroom_rh_hysteresis) || 5, level: Number(state.bathroom_max_level) || maxLevel, maxLevel };
  const counts = { control: rooms.filter(room => isIndoor(room.type) && room.control).length, measure: rooms.filter(room => !isIndoor(room.type) && room.type !== "excluded").length, excluded: rooms.filter(room => room.type === "excluded").length };

  const update = (index: number, room: Room) => setRooms(list => list.map((item, i) => (i === index ? room : item)));
  const addAll = () => {
    const result = addAllFromCatalog(rooms, catalog);
    setRooms(result.rooms);
    setNotice(result.added.length ? `Tilføjet: ${result.added.join(", ")}. Tjek typerne og tryk Gem rum.` : "Alle områder med temperatur eller fugt er allerede sat op.");
  };
  const save = async () => {
    if (!csrf || problems.length) return;
    setBusy(true); setNotice("Gemmer…");
    try {
      await postJson("/api/controller/config", { room_setup: toPayload(rooms) }, csrf);
      const next = await requestJson<RoomsView>("/api/controller/rooms");
      setView(next); setRooms(next.rooms); setSaved(JSON.stringify(next.rooms));
      setNotice("Rummene er gemt. Home Assistant sender de valgte følere inden for et minut.");
    } catch (e) { setNotice(`Fejl: ${e instanceof Error ? e.message : "?"}`); }
    finally { setBusy(false); }
  };

  const noCatalog = view !== null && catalog.length === 0;
  return <section className="dashboard-overview page-enter rooms-page">
    <header className="overview-heading-row"><div><span className="eyebrow">OPSÆTNING</span><h1>Rum</h1>
      <p>Vælg hvilke rum og følere fra Home Assistant der er med i styringen. Fugt, CO₂, PM2.5 og temperatur fra alle aktive rum bruges; rum uden ventilation ignoreres.</p></div></header>

    {error && <p className="settings-warning" role="alert">{error}</p>}

    <RoomAirOverview state={state}/>

    {noCatalog && <article className="surface panel-card room-empty" role="status">
      <DoorOpen size={28}/>
      <div><h2>Home Assistant har ikke sendt sine områder endnu</h2>
        <p>Når Dantherm Control-integrationen i Home Assistant er opdateret og forbundet, sender den alle områder med temperatur-, fugt-, CO₂- og PM2.5-følere hertil (ved start og hvert 10. minut). Så kan du vælge dem her.</p>
        <p>Tjek at integrationen er forbundet på siden <a href="#/home-assistant">Home Assistant</a>, og at "Brug Home Assistant-sensorer i Smart Auto" er slået til i integrationen.{rooms.length ? " Rummene herunder virker stadig." : ""}</p></div>
    </article>}

    <div className="room-toolbar">
      <button type="button" className="primary-action" disabled={!catalog.length || rooms.length >= MAX_ROOMS} onClick={addAll}><Wand2 size={15}/>Tilføj alle rum fra HA</button>
      <button type="button" className="secondary-action" disabled={rooms.length >= MAX_ROOMS} onClick={() => setRooms(list => [...list, blankRoom(list)])}><Plus size={15}/>Tilføj rum</button>
      <span className="room-counts">{rooms.length ? `${counts.control} styrer · ${counts.measure} kun måling · ${counts.excluded} ignoreres` : "Ingen rum sat op endnu"}</span>
    </div>

    {view !== null && !rooms.length && !noCatalog && <article className="surface panel-card room-empty" role="status">
      <DoorOpen size={28}/>
      <div><h2>Ingen rum endnu</h2><p>Tryk "Tilføj alle rum fra HA" for at hente alle områder med temperatur eller fugt. Garagen kommer med som ikke ventileret, og anlæggets egen føler placeres i spisestuen.</p></div>
    </article>}

    <div className="room-setup-list">
      {rooms.map((room, index) => <RoomCard key={`${room.id}-${index}`} room={room} catalog={catalog} state={state} bathroom={bathroom}
        onChange={next => update(index, next)} onUnitSensor={on => setRooms(list => withUnitSensor(list, room.id, on))}
        onRemove={() => setRooms(list => list.filter((_, i) => i !== index))}/>)}
    </div>

    {problems.length > 0 && <ul className="settings-warning room-problems">{problems.map(problem => <li key={problem}>{problem}</li>)}</ul>}
    <div className="settings-savebar"><span>{notice || (dirty ? "Ændringer gemmes først, når du trykker Gem rum." : "Kildefølere til pejs, udeluft og udsugningsrum vælges under Indstillinger.")}</span>
      <span className="room-save-actions">
        {dirty && <button type="button" className="secondary-action" disabled={busy} onClick={() => { setRooms(JSON.parse(saved) as Room[]); setNotice(""); }}><RotateCcw size={15}/>Fortryd</button>}
        <button type="button" className="primary-action" disabled={busy || !csrf || !dirty || problems.length > 0} onClick={() => void save()}><Save size={15}/>{busy ? "Gemmer…" : "Gem rum"}</button>
      </span></div>
  </section>;
}
