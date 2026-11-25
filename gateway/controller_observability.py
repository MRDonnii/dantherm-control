#!/usr/bin/env python3
"""Observability helpers for HCH5 Control.

This module is deliberately read-only with respect to the ventilation bus. It
adds controller decision history, data-health diagnostics and comfort status by
wrapping ControllerRuntime.snapshot(). No Modbus write path is introduced here.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from room_quality import quality_rooms
from sensor_freshness import UNIT_RH_MIN_PLAUSIBLE


class ControllerObservability:
    def __init__(self, runtime, *, path: str | Path | None = None, max_events: int = 200) -> None:
        self.runtime = runtime
        self.path = Path(path or os.getenv(
            "DANTHERM_DECISION_LOG",
            "/var/lib/dantherm-hch5-ha/decision-log.jsonl",
        ))
        self.events: deque[dict[str, Any]] = deque(maxlen=max(20, int(max_events)))
        self.lock = threading.RLock()
        self._last_signature: tuple[Any, ...] | None = None
        self._load_tail()

    def _load_tail(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-self.events.maxlen:]
        except OSError:
            return
        for line in lines:
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                self.events.append(item)

    def _append_disk(self, event: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Keep the persistent file bounded on long-running installations.
            if self.path.exists() and self.path.stat().st_size > 1_500_000:
                tail = list(self.events)[-100:]
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(
                    "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in tail),
                    encoding="utf-8",
                )
                os.replace(tmp, self.path)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            # Observability must never be able to interrupt ventilation control.
            pass

    @staticmethod
    def _number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ha_rooms_health(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Combined HA-room status, built from the per-measurement quality.

        The connection (``smart_inputs_online``: HA sent something recently)
        and the age of the measurements in it are reported separately: HA may
        resend a four-hour-old reading every minute. Status display only; the
        regulation already filters per measurement in room_quality.
        """
        rooms = snapshot.get("smart_ha_rooms") or {}
        rooms = rooms if isinstance(rooms, dict) else {}
        online = bool(snapshot.get("smart_inputs_online"))
        quality = snapshot.get("smart_room_quality")
        if not isinstance(quality, dict):
            quality = quality_rooms(rooms, 0.0, online=online)[1] if rooms else {}
        counted = [name for name in quality
                   if not (isinstance(rooms.get(name), dict) and rooms[name].get("enabled") is False)]
        counts = {"fresh": 0, "stale": 0, "unknown_age": 0, "connection_stale": 0}
        used = 0
        for name in counted:
            per_kind = quality.get(name)
            for item in (per_kind.values() if isinstance(per_kind, dict) else ()):
                if not isinstance(item, dict):
                    continue
                # "unavailable" = this room has no such sensor: optional, not a fault.
                if item.get("state") in counts:
                    counts[item["state"]] += 1
                used += item.get("used") is True
        # Selected in HA (or in the room setup) but without data in the latest message: the
        # optional per-room "missing_sensors" (integration 0.9.0-beta.15+) and set-up rooms the
        # message left out. Older integrations send neither, which reads as none missing. Only
        # counted while the connection is up; offline already says it all.
        reported = snapshot.get("smart_missing_sensors")
        reported = reported if isinstance(reported, dict) and online else {}
        missing_rooms = snapshot.get("smart_missing_rooms")
        missing_rooms = [name for name in missing_rooms if name in reported] if isinstance(missing_rooms, list) else []
        missing_by_room = {str(name): kinds for name, kinds in reported.items()
                           if isinstance(kinds, list) and kinds
                           and not (isinstance(rooms.get(name), dict) and rooms[name].get("enabled") is False)}
        missing = sum(len(kinds) for kinds in missing_by_room.values())
        measured = counts["fresh"] + counts["stale"] + counts["unknown_age"]
        # A selected-but-missing sensor is never fresh: alone it is "missing", together with
        # fresh, unknown-age or too old measurements it is "mixed".
        if not online:
            state, fresh = "offline", False
            measured += counts["connection_stale"]
        elif not counted and not missing:
            state, fresh = "no_rooms", None
        elif not measured and not missing:
            state, fresh = "no_measurements", None
        elif counts["stale"] or missing:
            if counts["fresh"] or counts["unknown_age"] or (counts["stale"] and missing):
                state = "mixed"
            else:
                state = "missing" if missing else "stale"
            fresh = False
        elif counts["unknown_age"]:
            state, fresh = "unknown", None
        else:
            state, fresh = "fresh", True
        return {
            "available": bool(rooms) if not online else measured > 0,
            "fresh": fresh,
            "state": state,
            "connection_online": online,
            "value": len(rooms),
            "rooms": len(set(counted) | set(missing_by_room)),
            "measurements": measured,
            "fresh_count": counts["fresh"],
            "stale_count": counts["stale"],
            "unknown_age_count": counts["unknown_age"],
            "missing_count": missing,
            "missing_rooms_count": len(missing_rooms),
            "missing": missing_by_room,
            "used_count": used,
            "source": "home_assistant",
        }

    def _data_health(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        gateway = self.runtime.gateway_state
        age = self._number(gateway.get("bus_last_frame_age"))
        if age is None:
            master_age = self.runtime.master.bus_age()
            age = float(master_age) if master_age is not None else None
        bus_fresh = (self.runtime._bus_healthy() if hasattr(self.runtime, "_bus_healthy") else
                     bool(gateway.get("bus_traffic") is True and age is not None and age <= 10.0))

        gateway = self.runtime._local_state() if hasattr(self.runtime, "_local_state") else gateway
        local = {
            "rh": self.runtime._first(gateway, "humidity", "relative_humidity"),
            "co2": self.runtime._first(gateway, "co2"),
            "outdoor": self.runtime._first(gateway, "outdoor_temp", "outdoor_temperature"),
            "room": (snapshot.get("measurements") or {}).get("room"),
            "supply": snapshot.get("actual_supply_air_temperature"),
        }
        # The unit's RH sensor reads far too low (9-14 % against 30 % in the rooms): below
        # UNIT_RH_MIN_PLAUSIBLE it is not counted as a working measurement.
        rh_value = self._number(local.get("rh"))
        implausible_rh = rh_value is not None and rh_value < UNIT_RH_MIN_PLAUSIBLE
        sensors = {
            key: {
                "available": value is not None and not (key == "rh" and implausible_rh),
                **({"implausible": True} if key == "rh" and implausible_rh else {}),
                "fresh": bool(value is not None and bus_fresh),
                "value": value,
                "source": (snapshot.get("afterheat_room_source_used") or "hch5") if key == "room" else "hch5",
            }
            for key, value in local.items()
        }
        sensors["ha_rooms"] = self._ha_rooms_health(snapshot)

        # A unit RH that reads implausibly low is a known fault of the sensor, not a data problem.
        required = [sensors[k] for k in ("rh", "outdoor", "room") if not (k == "rh" and implausible_rh)]
        if not bus_fresh:
            overall = "stale"
        elif all(item["available"] for item in required):
            overall = "healthy"
        else:
            overall = "degraded"
        return {
            "state": overall,
            "bus_fresh": bus_fresh,
            "bus_age_seconds": round(age, 1) if age is not None else None,
            "sensors": sensors,
        }

    def _comfort(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        supply = self._number(snapshot.get("actual_supply_air_temperature"))
        room = self._number((snapshot.get("measurements") or {}).get("room"))
        outdoor = self._number((snapshot.get("measurements") or {}).get("outdoor"))
        if supply is None:
            state = "unknown"
            reason = "Ingen gyldig indblæsningstemperatur"
        elif supply < 15.0:
            state = "cold"
            reason = "Indblæsningen er kølig"
        elif supply > 26.0:
            state = "warm"
            reason = "Indblæsningen er varm"
        else:
            state = "comfortable"
            reason = "Indblæsning inden for komfortområde"
        return {
            "state": state,
            "reason": reason,
            "supply_temperature": supply,
            "room_temperature": room,
            "outdoor_temperature": outdoor,
            "diagnostic_only": True,
        }

    @staticmethod
    def _signature(snapshot: dict[str, Any]) -> tuple[Any, ...]:
        return (
            snapshot.get("active_master"),
            snapshot.get("effective_source"),
            snapshot.get("effective_level"),
            snapshot.get("effective_bypass"),
            snapshot.get("cooling_state"),
            bool(snapshot.get("quick_boost_active")),
            bool(snapshot.get("vacation_active")),
            bool(snapshot.get("night_active")),
            bool(snapshot.get("schedule_active")),
            bool(snapshot.get("fireplace")),
            snapshot.get("smart_controlling_room"),
            snapshot.get("smart_controlling_metric"),
        )

    def _record_transition(self, snapshot: dict[str, Any]) -> None:
        signature = self._signature(snapshot)
        if signature == self._last_signature:
            return
        self._last_signature = signature
        event = {
            "timestamp": time.time(),
            "master": snapshot.get("active_master"),
            "source": snapshot.get("effective_source"),
            "level": snapshot.get("effective_level"),
            "reason": snapshot.get("effective_reason"),
            "bypass": snapshot.get("effective_bypass"),
            "cooling_state": snapshot.get("cooling_state"),
            "quick_boost": bool(snapshot.get("quick_boost_active")),
            "vacation": bool(snapshot.get("vacation_active")),
            "night": bool(snapshot.get("night_active")),
            "schedule": bool(snapshot.get("schedule_active")),
            "fireplace": bool(snapshot.get("fireplace")),
            "controlling_room": snapshot.get("smart_controlling_room"),
            "controlling_metric": snapshot.get("smart_controlling_metric"),
            # Everything that wanted more air at the time, with the winner first.
            "candidates": snapshot.get("smart_candidates") or [],
        }
        with self.lock:
            self.events.append(event)
            self._append_disk(event)

    def augment(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self._record_transition(snapshot)
        with self.lock:
            log = list(self.events)
        snapshot["data_health"] = self._data_health(snapshot)
        snapshot["comfort_guard"] = self._comfort(snapshot)
        snapshot["decision_log"] = log
        snapshot["decision_log_size"] = len(log)
        return snapshot


def install_controller_observability(runtime) -> ControllerObservability:
    """Wrap a ControllerRuntime instance without changing control semantics."""
    existing = getattr(runtime, "_hch_observability", None)
    if existing is not None:
        return existing

    observer = ControllerObservability(runtime)
    original_snapshot = runtime.snapshot

    def snapshot_with_observability():
        return observer.augment(original_snapshot())

    runtime.snapshot = snapshot_with_observability
    runtime._hch_observability = observer
    return observer


__all__ = ["ControllerObservability", "install_controller_observability"]
