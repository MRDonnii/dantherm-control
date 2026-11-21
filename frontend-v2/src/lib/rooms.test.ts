import { describe, expect, it } from "vitest";
import {
  TYPE_HELP, TYPE_LABELS, addAllFromCatalog, blankRoom, guessType, liveValues, roomFromArea, roomProblems, sensorGroups, slug, toPayload, withArea,
  withSensor, withType, withUnitSensor,
  type CatalogArea, type Room,
} from "./rooms";
import { WEATHER_AREA_ID } from "./rooms";

const area = (area_id: string, area_name: string, ...sensors: [string, CatalogArea["sensors"][number]["kind"]][]): CatalogArea =>
  ({ area_id, area_name, sensors: sensors.map(([entity_id, kind]) => ({ entity_id, name: entity_id, kind })) });

const CATALOG: CatalogArea[] = [
  area("badevaerelse", "Badeværelse", ["sensor.bad_t", "temperature"], ["sensor.bad_rh", "humidity"]),
  area("garagen", "Garagen", ["sensor.garage_t", "temperature"], ["sensor.garage_co2", "co2"]),
  area("kokkenet", "Køkkenet", ["sensor.kok_t", "temperature"], ["sensor.kok_rh", "humidity"], ["sensor.kok_co2", "co2"], ["sensor.kok_pm", "pm25"]),
  area("lys_grupper", "Lys grupper"),
  area("sovevaerelse", "Soveværelse", ["sensor.sove_t", "temperature"], ["sensor.sove_co2", "co2"]),
  area("spisestue", "Spisestue", ["sensor.spis_t", "temperature"], ["sensor.spis_t2", "temperature"], ["sensor.spis_rh", "humidity"]),
  area("udenfor", "Udenfor", ["sensor.ude_rh", "humidity"]),
  area("terrassen", "Terrassen", ["sensor.terrasse_pm", "pm25"]),
];

describe("room setup helpers", () => {
  it("guesses types like the Pi", () => {
    expect(["Garage", "Badeværelse", "Lille Wc", "Køkkenet", "Soveværelse", "Brændeovn", "Udenfor", "Indkørsel/fordør", "Udsugningsrum", "Stue"].map(guessType))
      .toEqual(["excluded", "bathroom", "bathroom", "kitchen", "bedroom", "source", "outdoor", "outdoor", "extract", "normal"]);
    expect(slug("Soveværelse")).toBe("sovevaerelse");
  });

  it("adds every area with temperature or humidity, garage not ventilated and the unit sensor in the dining room", () => {
    const { rooms, added } = addAllFromCatalog([], CATALOG);
    expect(added).toEqual(["Badeværelse", "Garagen", "Køkkenet", "Soveværelse", "Spisestue", "Udenfor"]);
    const by = Object.fromEntries(rooms.map(room => [room.name, room]));
    expect(by.Garagen.type).toBe("excluded");
    expect(by.Garagen.control).toBe(false);
    expect(by.Badeværelse.type).toBe("bathroom");
    expect(by.Udenfor.type).toBe("outdoor");
    expect(by.Udenfor.control).toBe(false);
    expect(by.Køkkenet.sensors).toEqual({ temperature: "sensor.kok_t", humidity: "sensor.kok_rh", co2: "sensor.kok_co2", pm25: "sensor.kok_pm", presence: null });
    expect(by.Spisestue.sensors.temperature).toBe("sensor.spis_t");
    expect(rooms.filter(room => room.unit_sensor).map(room => room.name)).toEqual(["Spisestue"]);
  });

  it("never adds an area twice and keeps rooms that are already set up", () => {
    const first = addAllFromCatalog([], CATALOG).rooms;
    const again = addAllFromCatalog(first, CATALOG);
    expect(again.added).toEqual([]);
    expect(again.rooms).toHaveLength(first.length);
    const legacy: Room = { ...blankRoom([], "Soveværelse"), area_id: null };
    expect(addAllFromCatalog([legacy], CATALOG).added).not.toContain("Soveværelse");
  });

  it("picks the new area's sensors, clears bathroom limits and keeps one unit sensor room", () => {
    const room = blankRoom([]);
    const moved = withArea(room, CATALOG[0]);
    expect(moved.sensors.humidity).toBe("sensor.bad_rh");
    const bath = { ...withType(moved, "bathroom"), rh_setpoint: 70 };
    expect(withType(bath, "normal").rh_setpoint).toBeUndefined();
    expect(withType({ ...bath, unit_sensor: true }, "excluded")).toMatchObject({ control: false, unit_sensor: false });
    const rooms = [{ ...room, id: "a", unit_sensor: true }, { ...room, id: "b" }];
    expect(withUnitSensor(rooms, "b", true).map(r => r.unit_sensor)).toEqual([false, true]);
  });

  it("offers the room's own area first", () => {
    const groups = sensorGroups(CATALOG, "temperature", "spisestue");
    expect(groups[0].label).toBe("Spisestue");
    expect(groups.map(group => group.label)).not.toContain("Udenfor");
  });

  it("finds problems the Pi would refuse", () => {
    const a = blankRoom([], "Stue");
    expect(roomProblems([a, { ...a, id: "x", name: " stue " }])).toEqual(['Navnet "Stue" bruges af flere rum.']);
    expect(roomProblems([{ ...a, unit_sensor: true }, { ...a, id: "b", name: "B", unit_sensor: true }])).toContain("Anlæggets egen føler kan kun sidde i ét rum.");
    expect(roomProblems([{ ...a, name: "" }])).toContain("Alle rum skal have et navn.");
    expect(toPayload([{ ...a, name: " Stue ", rh_setpoint: undefined }])[0]).not.toHaveProperty("rh_setpoint");
  });

  it("reads the live values the Pi uses", () => {
    const state = { smart_rooms: { Spisestue: { temperature: 21.4, humidity: 44, unit_sensor: true } } };
    expect(liveValues(state, "spisestue")).toEqual({ temperature: 21.4, humidity: 44, unit_sensor: true });
    expect(liveValues(state, "Garage")).toBeNull();
  });
});

describe("weather sensors (DMI)", () => {
  const withWeather = [...CATALOG, { area_id: WEATHER_AREA_ID, area_name: "Vejr (DMI)", sensors: [{ entity_id: "sensor.dmi_vejr_temperatur", name: "DMI temperatur", kind: "temperature" as const }] }];
  it("lead the list for an outdoor room and come last for an indoor one", () => {
    expect(sensorGroups(withWeather, "temperature", "udenfor", true)[0].label).toBe("Vejr (DMI)");
    const indoor = sensorGroups(withWeather, "temperature", "spisestue");
    expect(indoor[indoor.length - 1].label).toBe("Vejr (DMI)");
  });
  it("never become a room of their own", () => {
    expect(addAllFromCatalog([], withWeather).added).not.toContain("Vejr (DMI)");
  });
});

describe("presence per room", () => {
  const kontor = area("kontor", "Kontor", ["sensor.kontor_t", "temperature"], ["binary_sensor.hps_kontor_presence", "presence"]);
  it("is picked with the area and 'Ingen' stops the automatic choice", () => {
    const room = roomFromArea(kontor, []);
    expect(room.sensors.presence).toBe("binary_sensor.hps_kontor_presence");
    const none = withSensor(room, "presence", null);
    expect(none.presence_auto).toBe(false);
    expect(withSensor(none, "presence", "binary_sensor.x").presence_auto).toBeUndefined();
    expect(withArea(none, kontor).presence_auto).toBeUndefined();
  });
  it("live presence is read as on/off", () => {
    expect(liveValues({ smart_rooms: { Kontor: { temperature: 21, presence: true } } }, "kontor")).toEqual({ temperature: 21, presence: true });
  });
  it("the extract type says it does not take part in the control", () => {
    expect(TYPE_LABELS.extract).toBe("Kun kanalmåling (udsugning)");
    expect(TYPE_HELP.extract).toContain("IKKE med i styringen");
  });
});
