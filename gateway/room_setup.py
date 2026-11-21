"""Room setup: which Home Assistant sensors belong to which room, and how a room counts.

The mapping is made on the WebUI page "Rum" and stored in the controller
config (``room_setup``). Home Assistant sends a catalogue of its areas and
their sensors (``ha-catalog``), reads the mapping back and sends the room
values in the rooms named here. The Pi is the only place rooms are set up.

Kept free of the controller so the rules are easy to read and test.
"""
from __future__ import annotations

import re

# Room types. "Indoor" types take part in the air quality control and the
# indoor averages; the measuring types are only used as sources (stove,
# outdoor air, extract rooms); excluded rooms (e.g. a garage without
# ventilation) are ignored everywhere.
ROOM_TYPES = ("normal", "bedroom", "bathroom", "kitchen", "source", "outdoor", "extract", "excluded")
INDOOR_TYPES = frozenset({"normal", "bedroom", "bathroom", "kitchen"})
MEASURING_TYPES = frozenset({"source", "outdoor", "extract"})
ROOM_TYPE_LABELS = {
    "normal": "Normalt rum", "bedroom": "Soverum", "bathroom": "Badeværelse", "kitchen": "Køkken",
    "source": "Kilde (brændeovn)", "outdoor": "Ude", "extract": "Kun kanalmåling (udsugning)",
    "excluded": "Ikke ventileret",
}
# Room types whose temperature counts in the house average used by the afterheat
# target and the night cooling (bathrooms are skewed by showers).
COMFORT_TYPES = frozenset({"normal", "bedroom", "kitchen"})
PRIORITIES = ("auto", "low", "normal", "high", "critical")
# Stored rooms or catalogues with other kinds (e.g. "presence" from 1.12.0) drop them silently.
SENSOR_KINDS = ("temperature", "humidity", "co2", "pm25")
MAX_ROOMS = 32
MAX_AREAS = 64
MAX_SENSORS_PER_AREA = 40
ENTITY_RE = re.compile(r"^sensor\.[a-z0-9_]{1,120}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# The unit's own room sensor (HRC2/T5 and RH) sits here unless chosen otherwise.
DEFAULT_UNIT_SENSOR_ROOM = "spisestue"


class RoomSetupError(ValueError):
    """A room setup or catalogue the Pi refuses; the message is for the user."""


def slug(text: object) -> str:
    """A short id from a room name: lower case a-z, 0-9 and _."""
    value = str(text or "").strip().lower()
    for old, new in (("æ", "ae"), ("ø", "oe"), ("å", "aa"), ("ü", "u"), ("ö", "o"), ("ä", "a"), ("é", "e")):
        value = value.replace(old, new)
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return (value or "rum")[:40]


def _text(value: object, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _number(raw: dict, key: str, low: float, high: float, kind, name: str, label: str):
    if raw.get(key) in (None, ""):
        return None
    try:
        number = kind(raw[key])
    except (TypeError, ValueError) as error:
        raise RoomSetupError(f"{name}: {label} skal være et tal") from error
    if not low <= number <= high:
        raise RoomSetupError(f"{name}: {label} skal være {low:g}..{high:g}")
    return number


def clean_room(raw: object, *, max_level: int = 6) -> dict[str, object]:
    """One room, validated. Raises RoomSetupError with a Danish message."""
    if not isinstance(raw, dict):
        raise RoomSetupError("Et rum skal være et objekt")
    name = _text(raw.get("name"), 64)
    if not name:
        raise RoomSetupError("Rummet skal have et navn")
    room_id = _text(raw.get("id"), 40).lower() or slug(name)
    if not ID_RE.match(room_id):
        raise RoomSetupError(f"{name}: ugyldigt id")
    room_type = raw.get("type", "normal")
    if room_type not in ROOM_TYPES:
        raise RoomSetupError(f"{name}: ukendt rumtype")
    priority = raw.get("priority", "auto")
    if priority not in PRIORITIES:
        raise RoomSetupError(f"{name}: ukendt prioritet")
    for key in ("control", "enabled", "unit_sensor"):
        if key in raw and not isinstance(raw[key], bool):
            raise RoomSetupError(f"{name}: {key} skal være sand/falsk")
    area_id = _text(raw.get("area_id"), 64) or None
    sensors_in = raw.get("sensors") or {}
    if not isinstance(sensors_in, dict):
        raise RoomSetupError(f"{name}: følerne skal være et objekt")
    sensors: dict[str, str | None] = {}
    for kind in SENSOR_KINDS:
        entity_id = _text(sensors_in.get(kind), 130) or None
        if entity_id is not None and not ENTITY_RE.match(entity_id):
            raise RoomSetupError(f"{name}: {entity_id} er ikke en sensor fra Home Assistant")
        sensors[kind] = entity_id
    room: dict[str, object] = {
        "id": room_id,
        "name": name,
        "area_id": area_id,
        "area_name": _text(raw.get("area_name"), 64) or None,
        "type": room_type,
        "priority": priority,
        "control": raw.get("control", True) is True and room_type != "excluded",
        "enabled": raw.get("enabled", True) is not False,
        "unit_sensor": raw.get("unit_sensor") is True and room_type != "excluded",
        "sensors": sensors,
    }
    for key, low, high, label, kind in (
        ("rh_setpoint", 35.0, 90.0, "fugtgrænse", float),
        ("rh_hysteresis", 1.0, 20.0, "hysterese", float),
        ("max_level", 1, max_level, "højeste trin", int),
    ):
        value = _number(raw, key, low, high, kind, name, label)
        if value is not None:
            room[key] = value
    return room


def clean_rooms(value: object, *, max_level: int = 6) -> list[dict[str, object]]:
    """The whole room list: at most 32 rooms, unique ids and names, one unit sensor room."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise RoomSetupError("Rumlisten skal være en liste")
    if len(value) > MAX_ROOMS:
        raise RoomSetupError(f"Højst {MAX_ROOMS} rum understøttes")
    rooms = [clean_room(raw, max_level=max_level) for raw in value]
    ids: set[str] = set()
    names: set[str] = set()
    for room in rooms:
        if room["id"] in ids:
            raise RoomSetupError(f"{room['name']}: id'et bruges af et andet rum")
        if str(room["name"]).casefold() in names:
            raise RoomSetupError(f"{room['name']}: navnet bruges af et andet rum")
        ids.add(str(room["id"]))
        names.add(str(room["name"]).casefold())
    if sum(1 for room in rooms if room["unit_sensor"]) > 1:
        raise RoomSetupError("Anlæggets egen føler kan kun sidde i ét rum")
    return rooms


def clean_catalog(value: object) -> list[dict[str, object]]:
    """Home Assistant's areas and their sensors, as sent to /api/controller/ha-catalog."""
    if not isinstance(value, list):
        raise RoomSetupError("areas skal være en liste")
    if len(value) > MAX_AREAS:
        raise RoomSetupError(f"Højst {MAX_AREAS} områder understøttes")
    areas: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise RoomSetupError("Et område skal være et objekt")
        area_id = _text(raw.get("area_id"), 64)
        if not area_id or area_id in seen:
            continue
        seen.add(area_id)
        sensors: list[dict[str, str]] = []
        raw_sensors = raw.get("sensors") or []
        if not isinstance(raw_sensors, list):
            raise RoomSetupError(f"{area_id}: sensors skal være en liste")
        # Read a little past the limit: an older integration also sends presence sensors, which are skipped.
        for sensor in raw_sensors[:2 * MAX_SENSORS_PER_AREA]:
            if len(sensors) >= MAX_SENSORS_PER_AREA:
                break
            if not isinstance(sensor, dict):
                continue
            entity_id = _text(sensor.get("entity_id"), 130)
            kind = sensor.get("kind")
            if not ENTITY_RE.match(entity_id) or kind not in SENSOR_KINDS:
                continue
            entry = {"entity_id": entity_id, "name": _text(sensor.get("name"), 80) or entity_id, "kind": kind}
            unit = _text(sensor.get("unit"), 16)
            if unit:
                entry["unit"] = unit
            sensors.append(entry)
        areas.append({"area_id": area_id, "area_name": _text(raw.get("area_name"), 64) or area_id,
                      "sensors": sensors})
    return areas


def area_for_entities(catalog: list[dict[str, object]], entity_ids: list[str]) -> tuple[str | None, str | None]:
    """The one area all the entities belong to, or (None, None)."""
    found: set[tuple[str, str]] = set()
    for entity_id in entity_ids:
        for area in catalog:
            if any(sensor["entity_id"] == entity_id for sensor in area["sensors"]):
                found.add((str(area["area_id"]), str(area["area_name"])))
    return next(iter(found)) if len(found) == 1 else (None, None)


def guess_type(name: str) -> str:
    """A sensible room type from a room or area name (Danish and English)."""
    folded = name.casefold()
    words = re.split(r"[^a-z0-9æøå]+", folded)
    if "garage" in folded or "carport" in folded:
        return "excluded"
    if any(marker in folded for marker in ("brændeovn", "pejs", "stove", "fireplace")):
        return "source"
    if "udsug" in folded or "extract" in folded:
        return "extract"
    if any(word in ("ude", "udenfor", "udeluft", "outdoor", "outside", "have", "terrasse", "terrassen")
           for word in words) or folded.startswith("udeluft") or any(
               marker in folded for marker in ("indkørsel", "indkorsel", "driveway", "altan", "balkon", "balcony")):
        return "outdoor"
    if any(marker in folded for marker in ("bad", "bath", "brus", "shower", "toilet")) or "wc" in words:
        return "bathroom"
    if "køkken" in folded or "kokken" in folded or "kitchen" in folded:
        return "kitchen"
    if "sove" in folded or "bedroom" in folded:
        return "bedroom"
    return "normal"


def migrate_legacy(legacy: object, catalog: list[dict[str, object]], config: dict,
                   *, max_level: int = 6) -> list[dict[str, object]]:
    """The rooms from the old Home Assistant options flow as a first room setup.

    ``legacy`` is the room payload HA used to send ({name: {enabled, control,
    priority, room_type, entities: {kind: entity_id}}}). Rooms used as stove,
    outdoor or extract sources in the settings keep that role. WebUI bathroom
    limits (``bathroom_rooms``) move into the rooms they belong to.
    """
    if not isinstance(legacy, dict):
        return []
    sources = {}
    for key, room_type in (("fireplace_auto_source", "source"), ("outdoor_humidity_source", "outdoor"),
                           ("duct_extract_source", "extract")):
        text = str(config.get(key) or "")
        if text.startswith("room:"):
            sources[text[5:].casefold()] = room_type
    bathroom = config.get("bathroom_rooms") or {}
    rooms: list[dict[str, object]] = []
    used_ids: set[str] = set()
    for raw_name, raw in list(legacy.items())[:MAX_ROOMS]:
        name = _text(raw_name, 64)
        if not name or not isinstance(raw, dict):
            continue
        entities = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
        sensors = {kind: (str(entities.get(kind)) if entities.get(kind) else None) for kind in SENSOR_KINDS}
        room_type = sources.get(name.casefold()) or guess_type(name)
        legacy_type = str(raw.get("room_type") or "auto")
        override = bathroom.get(name) if isinstance(bathroom.get(name), dict) else {}
        override_type = override.get("type", "auto")
        if room_type in INDOOR_TYPES:
            wanted = override_type if override_type != "auto" else legacy_type
            if wanted == "bathroom":
                room_type = "bathroom"
            elif wanted == "normal" and room_type == "bathroom":
                room_type = "normal"
        area_id, area_name = (area_for_entities(catalog, [e for e in sensors.values() if e])
                              if room_type in INDOOR_TYPES else (None, None))
        room_id = slug(name)
        while room_id in used_ids:
            room_id = (room_id[:36] + "_" + str(len(used_ids)))[:40]
        used_ids.add(room_id)
        candidate = {
            "id": room_id, "name": name, "area_id": area_id, "area_name": area_name,
            "type": room_type, "priority": raw.get("priority", "auto") if raw.get("priority") in PRIORITIES else "auto",
            "control": raw.get("control", True) is not False, "enabled": raw.get("enabled", True) is not False,
            "unit_sensor": False, "sensors": {k: v for k, v in sensors.items() if v and ENTITY_RE.match(v)},
        }
        for key in ("rh_setpoint", "rh_hysteresis", "max_level"):
            if override.get(key) is not None:
                candidate[key] = override[key]
        try:
            rooms.append(clean_room(candidate, max_level=max_level))
        except RoomSetupError:
            continue
    return rooms


def by_name(rooms: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    """Rooms keyed by the folded name HA uses when it sends values."""
    return {str(room["name"]).casefold(): room for room in rooms}


def room_kind_flags(room: dict[str, object]) -> dict[str, object]:
    """The metadata the control uses for a mapped room (see ControllerRuntime.room_inputs)."""
    return {
        "enabled": room.get("enabled", True) is not False,
        "control": room.get("control", True) is not False and room.get("type") in INDOOR_TYPES,
        "priority": room.get("priority", "auto"),
        "room_type": "bathroom" if room.get("type") == "bathroom" else "normal",
        "room_kind": room.get("type", "normal"),
        "room_id": room.get("id"),
        "area_id": room.get("area_id"),
    }
