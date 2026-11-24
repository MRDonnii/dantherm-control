"""Diagnostics and protection built on the controller snapshot.

Fed every few seconds by the controller runtime with the values it already
reports (temperatures, the measured T2, the running level, airflow, the
unit's power draw from Home Assistant). Nothing here writes to the unit; it
only derives states, alarms and energy counters that the WebUI and Home
Assistant show.

- Frost: the exchanger ices up when the exhaust air (T4) nears 0 C. The HCH5
  has its own protection; this warns earlier and shows when it happens.
- Alarms: each condition must hold for a while before it is raised, and
  clears on its own when the condition is gone.
- Fan power (SFP) and filter: specific fan power, W per m3/s. The fans hold
  their speed per gear whatever the filter, so a clogging filter shows as the
  power at the same gears moving away from the clean reference: the mean
  power per gear pair in the first days after the filters are changed (the
  unit's own counter, HAC1 block 1024, reset by the button on the unit).
- Energy: recovered heat, afterheat and the unit's own consumption are
  integrated to kWh totals that survive restarts, plus today's values.
- Flapping: more than LEVEL_FLAP_LIMIT automatic level changes in the last
  hour (level_changes_last_hour from the controller) raises a warning. It
  clears at LEVEL_FLAP_CLEAR or fewer, or after LEVEL_FLAP_QUIET_SECONDS
  without a change (level_change_quiet_seconds), so it cannot flap itself.
  After a quiet clear it needs a fresh burst (LEVEL_FLAP_LIMIT changes since
  the clear) until the old burst has left the hour (LEVEL_FLAP_REARM_SECONDS).
- Air balance: once the ducts are known (measured airflow or the learned
  heat balance), running with more supply than extract air is reported.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger(__name__)

# Seconds a condition must hold before its alarm is raised.
ALARM_DELAYS = {
    "controller_command_failed": 60,
    "local_sensor_stale": 120,
    "frost_risk": 300,
    "recovery_low": 1800,
    "recovery_mismatch": 3600,
    "bypass_not_closing": 1200,
    "afterheat_no_lift": 1800,
    "sensor_missing": 300,
    "bus_unhealthy": 120,
    "filter_clogging": 3600,
    "balance_overpressure": 3600,
    # The count already spans an hour.
    "level_flapping": 0,
    # The restart file already spans 30 minutes.
    "watchdog_restarts": 0,
}
ALARM_TEXT = {
    "controller_command_failed": "En styrekommando er ikke gennemført; Pi forsøger igen med pause",
    "local_sensor_stale": "Lokale sensormålinger mangler eller er for gamle; kontrollér forbindelsen",
    "frost_risk": "Frostrisiko i veksleren: afkastluften (T4) er nær 0 °C",
    "recovery_low": "Lav varmegenvinding: veksleren kan være snavset eller utæt",
    "recovery_mismatch": "Genvinding målt på indblæsning og udsugning er uenige: mulig utæthed eller følerfejl",
    "bypass_not_closing": "Bypass ser ikke ud til at lukke: indblæsningen følger udeluften",
    "afterheat_no_lift": "Eftervarmen kalder, men luften bliver ikke varmere: ventil, pumpe eller luft i fladen",
    "sensor_missing": "En 1-Wire-føler med en rolle svarer ikke",
    "bus_unhealthy": "Ingen sund RS485-forbindelse til anlægget",
    "filter_clogging": "Ventilatorernes strøm ved samme gear afviger fra rene filtre: filter eller rist stopper til",
    "balance_overpressure": "Overtryk i huset: indblæsningen giver mere luft end udsugningen, så fugtig luft kan presses ud i konstruktionen. Slå luftbalancen til",
    "level_flapping": "Automatikken skifter trin for tit (over 12 gange i timen): en grænse eller en føler svinger",
    "watchdog_restarts": "Vagthunden har genstartet styringen 3 gange eller mere inden for 30 min: se loggen på Pi'en",
}
ALARM_SEVERITY = {
    "controller_command_failed": "critical",
    "local_sensor_stale": "warning",
    "frost_risk": "warning",
    "recovery_low": "warning",
    "recovery_mismatch": "info",
    "bypass_not_closing": "warning",
    "afterheat_no_lift": "warning",
    "sensor_missing": "warning",
    "bus_unhealthy": "critical",
    "filter_clogging": "info",
    "balance_overpressure": "info",
    "level_flapping": "warning",
    "watchdog_restarts": "critical",
}
FROST_WATCH_T4 = 3.0
FROST_RISK_T4 = 0.5
# Clean-filter reference: power per fan gear pair, learned this many hours
# after a filter change. Measured 2026-10-02 at gears 20/13..100/80: washed
# and new filters were within 0.5 % in rpm and watts, so the deviation shows
# real clogging, not noise. The alarm limit is a first value until dirty
# filters have been measured.
FILTER_LEARN_HOURS = 72
FILTER_DEVIATION_PERCENT = 15.0
FILTER_MIN_POWER_W = 5.0
FILTER_WINDOW_SAMPLES = 30
FILTER_REFERENCE_WEIGHT = 360
# Extract below supply by more than this (in % of supply) counts as overpressure.
OVERPRESSURE_PERCENT = 3.0
# More automatic level changes than this in the last hour is flapping; the
# warning clears at LEVEL_FLAP_CLEAR or fewer, or after a quiet period.
LEVEL_FLAP_LIMIT = 12
LEVEL_FLAP_CLEAR = 8
LEVEL_FLAP_QUIET_SECONDS = 900
# After a quiet clear, the next single change must not raise it again with the
# old burst still in the hour's count: it takes LEVEL_FLAP_LIMIT changes since
# the clear, or this long (the old burst, older than the quiet period, has
# then left the hour).
LEVEL_FLAP_REARM_SECONDS = 3600 - LEVEL_FLAP_QUIET_SECONDS
SAVE_EVERY = 300.0
MAX_STEP = 60.0
# The gateway records each watchdog exit; this many within the window is a restart loop.
WATCHDOG_RESTART_FILE = "watchdog-restarts.json"
WATCHDOG_RESTART_LIMIT = 3
WATCHDOG_RESTART_WINDOW = 1800.0
WATCHDOG_RESTART_KEEP = 50


def _read_restarts(path: Path) -> list[dict[str, object]]:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = saved.get("restarts") if isinstance(saved, dict) else None
    return [e for e in entries or [] if isinstance(e, dict) and _num(e.get("at")) is not None]


def record_watchdog_restart(path: str | Path, reason: str, now: float | None = None) -> None:
    """Append one watchdog exit (wall-clock time and reason) to the restart file."""
    path = Path(path)
    now = time.time() if now is None else now
    entries = [e for e in _read_restarts(path) if now - float(e["at"]) <= 86400]
    entries.append({"at": round(now, 1), "reason": str(reason)[:200]})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".watchdog-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"restarts": entries[-WATCHDOG_RESTART_KEEP:]}, handle)
        os.replace(tmp, path)
    except OSError as error:
        LOG.warning("Unable to record the watchdog restart: %s", error)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


class Diagnostics:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.started: dict[str, float] = {}
        self.active: dict[str, float] = {}
        # Level flapping after a quiet clear: re-armed at this time or after a fresh burst.
        self.flap_rearm_at: float | None = None
        self.flap_fresh = 0.0
        self.flap_count: float | None = None
        self.energy = {"recovered_kwh": 0.0, "afterheat_kwh": 0.0, "unit_kwh": 0.0}
        self.today = {"date": "", "recovered_kwh": 0.0, "afterheat_kwh": 0.0, "unit_kwh": 0.0}
        # Clean power per fan gear pair ("extract/supply"), learned after each filter change.
        self.filter_reference: dict[str, dict[str, float]] = {}
        self.filter_window: list[float] = []
        self.filter_pair: str | None = None
        self.filter_hours: float | None = None
        self.filter_marker: object = None
        self.last_update: float | None = None
        self.last_save = 0.0
        self.result: dict[str, object] = {}
        # Local sensors that have had a value since start: only those can go stale.
        self.seen_sensors: set[str] = set()
        self.restart_path = self.path.parent / WATCHDOG_RESTART_FILE if self.path else None
        self.restart_mtime: float | None = None
        self.restart_times: list[float] = []
        self._load()

    def _watchdog_restarts(self, now: float) -> int:
        """Watchdog exits within the last WATCHDOG_RESTART_WINDOW seconds (file re-read when it changes)."""
        if self.restart_path is None:
            return 0
        try:
            mtime = self.restart_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self.restart_mtime:
            self.restart_mtime = mtime
            self.restart_times = ([] if mtime is None else
                                  [float(e["at"]) for e in _read_restarts(self.restart_path)])
        return sum(1 for at in self.restart_times if 0 <= now - at <= WATCHDOG_RESTART_WINDOW)

    # ---- persistence -------------------------------------------------
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            LOG.warning("Diagnostics state unreadable, starting fresh: %s", error)
            return
        for key in self.energy:
            self.energy[key] = float(saved.get("energy", {}).get(key, 0.0) or 0.0)
        today = saved.get("today", {})
        if isinstance(today, dict):
            self.today.update({k: today[k] for k in self.today if k in today})
        refs = saved.get("filter_reference", {})
        if isinstance(refs, dict):
            self.filter_reference = {
                str(k): {"w": float(v["w"]), "n": float(v.get("n", 1))}
                for k, v in refs.items() if isinstance(v, dict) and _num(v.get("w"))}
        self.filter_hours = _num(saved.get("filter_hours"))
        self.filter_marker = saved.get("filter_marker")

    def save(self) -> None:
        if not self.path:
            return
        payload = {"energy": self.energy, "today": self.today,
                   "filter_reference": self.filter_reference, "filter_hours": self.filter_hours,
                   "filter_marker": self.filter_marker}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".diagnostics-", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.path)
        except OSError as error:
            LOG.warning("Unable to save diagnostics state: %s", error)

    def reset_filter_reference(self) -> None:
        self.filter_reference.clear()
        self.filter_window.clear()
        self.save()

    # ---- update ------------------------------------------------------
    def _level_flapping(self, flaps: float | None, quiet: float | None, now: float) -> bool:
        """Hysteresis: raised above LEVEL_FLAP_LIMIT, held until LEVEL_FLAP_CLEAR or a quiet period.

        After a quiet clear it is raised again only by a fresh burst (changes
        counted since the clear) or once the old burst has left the hour.
        """
        active = "level_flapping" in self.active
        if self.flap_rearm_at is not None:
            if flaps is not None and self.flap_count is not None and flaps > self.flap_count:
                self.flap_fresh += flaps - self.flap_count
            if now >= self.flap_rearm_at or self.flap_fresh >= LEVEL_FLAP_LIMIT:
                self.flap_rearm_at, self.flap_fresh = None, 0.0
        self.flap_count = flaps
        over = flaps is not None and flaps > (LEVEL_FLAP_CLEAR if active else LEVEL_FLAP_LIMIT)
        quiet_now = quiet is not None and quiet >= LEVEL_FLAP_QUIET_SECONDS
        if active and over and quiet_now:
            self.flap_rearm_at, self.flap_fresh = now + LEVEL_FLAP_REARM_SECONDS, 0.0
        return over and not quiet_now and self.flap_rearm_at is None

    def update(self, s: dict[str, object], now: float | None = None) -> dict[str, object]:
        now = time.time() if now is None else now
        step = 0.0 if self.last_update is None else min(MAX_STEP, max(0.0, now - self.last_update))
        self.last_update = now

        t1 = _num(s.get("outdoor_temperature"))
        t3 = _num(s.get("extract_temperature"))
        t4 = _num(s.get("exhaust_temperature"))
        t2 = _num(s.get("actual_supply_before_heater_temperature"))
        bypass = s.get("actual_bypass") is True
        supply_recovery = _num(s.get("supply_recovery_percent"))
        extract_recovery = None
        if t1 is not None and t3 is not None and t4 is not None and t3 - t1 >= 3 and not bypass:
            extract_recovery = (t3 - t4) / (t3 - t1) * 100
        power = _num(s.get("unit_power_w"))
        airflow = _num(s.get("supply_airflow_estimate_m3h"))
        cold = t1 is not None and t3 is not None and t3 - t1 >= 8

        # Frost: T4 near zero with the core in use.
        frost_state = "unknown" if t4 is None else "ok"
        if t4 is not None and not bypass:
            if t4 <= FROST_RISK_T4:
                frost_state = "risk"
            elif t4 <= FROST_WATCH_T4:
                frost_state = "watch"

        # SFP: fan power per airflow.
        sfp = power / (airflow / 3600.0) if power is not None and airflow else None

        # Filter: a change shows as the unit's hour counter starting again
        # (or, without it, the remaining filter life jumping up).
        hours = _num(s.get("filter_hours_since_change"))
        life = _num(s.get("filter_life_percent"))
        if hours is not None:
            if self.filter_hours is not None and hours + 1 < self.filter_hours:
                self.reset_filter_reference()
            self.filter_hours = hours
        elif life is not None:
            previous = _num(self.filter_marker)
            if previous is not None and life - previous >= 20:
                self.reset_filter_reference()
        if life is not None:
            self.filter_marker = life
        learning = hours is not None and hours <= FILTER_LEARN_HOURS
        filter_ratio = None
        extract_gear = _num(s.get("actual_fan_extract_percent"))
        supply_gear = _num(s.get("actual_fan_supply_percent"))
        steady = (
            power is not None and power >= FILTER_MIN_POWER_W
            and extract_gear is not None and supply_gear is not None
            and s.get("standby_active") is not True and s.get("bonfire_active") is not True
            and not s.get("actual_bypass_travel_direction")
        )
        pair = f"{int(extract_gear)}/{int(supply_gear)}" if steady else None
        if pair != self.filter_pair:
            self.filter_pair = pair
            self.filter_window = []
        if pair is not None:
            self.filter_window.append(power)
            del self.filter_window[:-FILTER_WINDOW_SAMPLES]
        reference = self.filter_reference.get(pair) if pair else None
        if pair is not None and len(self.filter_window) >= FILTER_WINDOW_SAMPLES:
            average = sum(self.filter_window) / len(self.filter_window)
            if learning or reference is None and hours is None:
                # Running mean of the clean power at this gear pair.
                entry = self.filter_reference.setdefault(pair, {"w": average, "n": 0.0})
                entry["n"] = min(FILTER_REFERENCE_WEIGHT, entry["n"] + 1)
                entry["w"] += (average - entry["w"]) / entry["n"]
                reference = entry
            if reference:
                filter_ratio = average / reference["w"]
        filter_change = None if filter_ratio is None else (filter_ratio - 1.0) * 100.0

        # Energy: integrate the watts over the step.
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if self.today["date"] != today:
            self.today = {"date": today, "recovered_kwh": 0.0, "afterheat_kwh": 0.0, "unit_kwh": 0.0}
        for key, watts in (("recovered_kwh", s.get("recovered_heat_w")),
                           ("afterheat_kwh", s.get("afterheat_power_w")),
                           ("unit_kwh", power)):
            value = _num(watts)
            if value is not None and value > 0 and step:
                kwh = value * step / 3_600_000
                self.energy[key] += kwh
                self.today[key] += kwh

        # Alarm conditions, raised after their delay.
        roles = s.get("onewire_sensors") or []
        missing = any(isinstance(x, dict) and x.get("role") not in (None, "none") and x.get("temperature") is None for x in roles)
        # HAC1 keeps its call flag on while the air already is at the setpoint: no lift is wanted then.
        ah_setpoint = _num(s.get("afterheat_effective_setpoint"))
        if ah_setpoint is None:
            ah_setpoint = _num(s.get("actual_afterheat_setpoint"))
        ah_before = _num(s.get("corrected_t2_temperature"))
        if ah_before is None:
            ah_before = _num(s.get("actual_supply_before_heater_temperature"))
        ah_lift = _num(s.get("afterheat_lift"))
        afterheat_target_reached = (
            ah_setpoint is not None and ah_before is not None and ah_lift is not None
            and ah_before + ah_lift >= ah_setpoint - 1.0
        )
        level_flapping = self._level_flapping(_num(s.get("level_changes_last_hour")),
                                              _num(s.get("level_change_quiet_seconds")), now)
        health = (s.get("data_health") or {}).get("sensors") or {}
        for key in ("outdoor", "co2"):
            if health.get(key, {}).get("value") is not None:
                self.seen_sensors.add(key)
        restarts = self._watchdog_restarts(now)
        conditions = {
            # Only while the Pi may write: a released master has nothing pending.
            "controller_command_failed": bool(s.get("pending_commands"))
                and s.get("hardware_writes_allowed") is not False,
            # A sensor this unit never had (e.g. no CO2 sensor) is not stale.
            "local_sensor_stale": any(key in self.seen_sensors and health.get(key, {}).get("fresh") is False
                                      for key in ("outdoor", "co2")),
            "frost_risk": frost_state == "risk",
            "recovery_low": cold and supply_recovery is not None and supply_recovery < 60,
            "recovery_mismatch": cold and supply_recovery is not None and extract_recovery is not None
                and abs(supply_recovery - extract_recovery) > 20,
            "bypass_not_closing": cold and s.get("actual_bypass_request") in (False, "off", "OFF", 0)
                and not bypass and t2 is not None and t1 is not None and t3 is not None
                and (t2 - t1) < 0.25 * (t3 - t1),
            "afterheat_no_lift": s.get("actual_afterheat") is True
                and s.get("actual_afterheat_outdoor_lockout") is not True
                and _num(s.get("afterheat_lift")) is not None and _num(s.get("afterheat_lift")) < 0.5
                and not afterheat_target_reached,
            "sensor_missing": missing,
            "bus_unhealthy": s.get("rs485_healthy") is False,
            "filter_clogging": filter_change is not None and not learning
                and abs(filter_change) >= FILTER_DEVIATION_PERCENT,
            # The fireplace function makes overpressure on purpose.
            "balance_overpressure": s.get("fireplace") is not True and s.get("actual_fireplace") is not True
                and _num(s.get("balance_running_excess_percent")) is not None
                and _num(s.get("balance_running_excess_percent")) < -OVERPRESSURE_PERCENT,
            "level_flapping": level_flapping,
            "watchdog_restarts": restarts >= WATCHDOG_RESTART_LIMIT,
        }
        for code, holds in conditions.items():
            if holds:
                self.started.setdefault(code, now)
                if now - self.started[code] >= ALARM_DELAYS[code]:
                    self.active.setdefault(code, now)
            else:
                self.started.pop(code, None)
                self.active.pop(code, None)

        alarms = [
            {"code": code, "severity": ALARM_SEVERITY[code], "text": ALARM_TEXT[code], "since": int(since)}
            for code, since in sorted(self.active.items(), key=lambda item: item[1])
        ]
        worst = "ok"
        for alarm in alarms:
            if alarm["severity"] == "critical" or (alarm["severity"] == "warning" and worst != "critical"):
                worst = alarm["severity"]
            elif worst == "ok":
                worst = "info"
        self.result = {
            "diagnostics_status": worst,
            "diagnostics_alarms": alarms,
            "diagnostics_alarm_count": len(alarms),
            "diagnostics_alarm_text": "; ".join(a["text"] for a in alarms) or "Ingen",
            "watchdog_restarts_30min": restarts,
            "frost_state": frost_state,
            "extract_recovery_percent": None if extract_recovery is None else round(max(0.0, min(100.0, extract_recovery)), 1),
            "specific_fan_power": None if sfp is None else round(sfp),
            "filter_power_ratio": None if filter_ratio is None else round(filter_ratio, 3),
            "filter_power_change_percent": None if filter_change is None else round(filter_change, 1),
            "filter_reference_learning": learning,
            "filter_reference_pairs": len(self.filter_reference),
            "filter_reference_gears": self.filter_pair if self.filter_pair in self.filter_reference else None,
            "recovered_energy_kwh": round(self.energy["recovered_kwh"], 3),
            "afterheat_energy_kwh": round(self.energy["afterheat_kwh"], 3),
            "unit_energy_kwh": round(self.energy["unit_kwh"], 3),
            "recovered_energy_today_kwh": round(self.today["recovered_kwh"], 3),
            "afterheat_energy_today_kwh": round(self.today["afterheat_kwh"], 3),
            "unit_energy_today_kwh": round(self.today["unit_kwh"], 3),
            "recovery_factor": (
                round(self.today["recovered_kwh"] / self.today["unit_kwh"], 1)
                if self.today["unit_kwh"] > 0.01 else None
            ),
        }
        if now - self.last_save >= SAVE_EVERY:
            self.last_save = now
            self.save()
        return self.result
