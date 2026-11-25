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
    # Thresholds below come from the 30 days of live data: none of them would have fired on it
    # except the one afterheat event on 2026-11-21 (water cold at step 8-10).
    "coil_frost": 120,
    "afterheat_water_cold": 900,
    "room_sensor_missing": 1800,
    # The count already spans 6 h / 1 h.
    "afterheat_pilot_fallback": 0,
    "afterheat_rate_high": 0,
    "fan_speed_mismatch": 600,
    "level_not_followed": 600,
    # The sensor has already been constant for hours.
    "sensor_frozen": 0,
    "afterheat_status_stale": 120,
    "pi_disk_full": 300,
    "pi_undervoltage": 60,
    "history_paused": 1800,
    "clock_unsynced": 1800,
    # The seven-day median already spans days.
    "recovery_trend": 0,
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
    "coil_frost": "Vandfladen er tæt på frost: tjek fjernvarmen og ventilen",
    "afterheat_water_cold": "Eftervarmen kalder, men vandet i fladen er koldt: tjek fjernvarmen, ventilen og pumpen",
    "room_sensor_missing": "En CO₂- eller fugtmåler i et rum med styring mangler eller har ikke sendt længe",
    "afterheat_pilot_fallback": "Eftervarmen er faldet tilbage til anlæggets egen styring 3 gange eller mere inden for 6 timer",
    "afterheat_rate_high": "Eftervarmen skifter trin meget ofte",
    "fan_speed_mismatch": "En ventilators omdrejninger passer ikke til trinnet: tjek ventilatoren",
    "level_not_followed": "Anlægget følger ikke det beordrede trin: ventilatorerne kører ikke som bestilt",
    "sensor_frozen": "En måler står stille på samme værdi i flere timer",
    "afterheat_status_stale": "Eftervarmens status opdateres ikke: styringen kan have en fejl",
    "pi_disk_full": "Pi'ens disk er næsten fuld (90 % eller mere)",
    "pi_undervoltage": "Pi'en melder for lav spænding: tjek strømforsyningen",
    "history_paused": "Optagelsen af historik er sat på pause: tjek disken",
    "clock_unsynced": "Pi'ens ur er ikke synkroniseret med netværket",
    "recovery_trend": "Varmegenvindingen er faldet over den seneste uge: veksleren kan være ved at blive snavset",
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
    "coil_frost": "critical",
    "afterheat_water_cold": "warning",
    "room_sensor_missing": "warning",
    "afterheat_pilot_fallback": "warning",
    "afterheat_rate_high": "info",
    "fan_speed_mismatch": "warning",
    "level_not_followed": "warning",
    "sensor_frozen": "info",
    "afterheat_status_stale": "info",
    "pi_disk_full": "warning",
    "pi_undervoltage": "warning",
    "history_paused": "warning",
    "clock_unsynced": "info",
    "recovery_trend": "info",
}
# An alarm clears only after its condition has been false for max(CLEAR_HOLD_MIN, delay / 2)
# seconds, so a value that wobbles around its limit neither flaps nor mails again and again.
# These have their own hysteresis (a count spanning an hour, a restart file) and clear at once.
CLEAR_HOLD_MIN = 300.0
CLEAR_HOLD_OWN = frozenset({"level_flapping", "watchdog_restarts", "afterheat_pilot_fallback",
                            "afterheat_rate_high", "sensor_frozen", "recovery_trend"})
CLEAR_HOLD_LONG = {"recovery_trend": 3600.0}
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
# Water coil frost sensor: below this (fresh) the coil is close to freezing. 30 days of live data: min 15.2 C.
COIL_FROST_C = 10.0
# Afterheat "water cold": HAC1 asks for much heat but the flow pipe is cold, or the return is far
# below the air that should be warming it (only 2026-11-21 matches in 30 days).
WATER_COLD_FLOW_LEVEL = 8
WATER_COLD_FLOW_C = 36.0
WATER_COLD_RETURN_LEVEL = 3
WATER_COLD_RETURN_BELOW_K = 3.0
# Afterheat "no lift" at a high level: this long at HAC1 level >= NO_LIFT_LEVEL with less than NO_LIFT_K.
NO_LIFT_LEVEL = 6
NO_LIFT_K = 1.0
NO_LIFT_SECONDS = 1200.0
PILOT_FALLBACK_LIMIT = 3
PILOT_RATE_WARNINGS_LIMIT = 3
# Fan rpm per percent is fixed (weekly medians moved <= 1 rpm in 5 weeks); 8 % off for the
# alarm delay is a failing fan. A reference needs RPM_REF_MIN samples first.
RPM_TOLERANCE = 0.08
RPM_STEADY_SECONDS = 180.0
RPM_REF_MIN = 30
RPM_REF_WEIGHT = 500
# The unit has this long to reach a new level before it must follow it.
FOLLOW_GRACE_SECONDS = 180.0
FOLLOW_TOLERANCE_PERCENT = 5.0
# Longest constant run in 30 days was 0.7 h (6 h for PM2.5, left out): 4 h without any change.
FROZEN_SECONDS = 4 * 3600.0
PI_DISK_PERCENT = 90.0
# Heat recovery: the 7-day median this many points under the 30-day median.
RECOVERY_TREND_POINTS = 4.0
RECOVERY_TREND_MIN_SAMPLES = 60
RECOVERY_KEEP_DAYS = 31
# A coil that is cold for this long gives the T2 sensor error (T2AH minus T2).
T2_COLD_SECONDS = 1200.0
T2_SAMPLES_MAX = 240
T2_SAMPLES_MIN = 60
SAVE_EVERY = 300.0
MAX_STEP = 60.0
# The gateway records each watchdog exit; this many within the window is a restart loop.
WATCHDOG_RESTART_FILE = "watchdog-restarts.json"
WATCHDOG_RESTART_LIMIT = 3
WATCHDOG_RESTART_WINDOW = 1800.0
WATCHDOG_RESTART_KEEP = 50
# Restarts are kept this long for the weekly health report.
WATCHDOG_RESTART_RETENTION = 8 * 86400


def _read_restarts(path: Path) -> list[dict[str, object]]:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = saved.get("restarts") if isinstance(saved, dict) else None
    return [e for e in entries or [] if isinstance(e, dict) and _num(e.get("at")) is not None]


def read_watchdog_restarts(path: str | Path) -> list[dict[str, object]]:
    """Recorded watchdog exits, oldest first ([] when none or unreadable)."""
    return sorted(_read_restarts(Path(path)), key=lambda entry: float(entry["at"]))


# A missing outdoor temperature (T1) raises the stale alarm this long after start even if never seen.
OUTDOOR_EXPECTED_AFTER_SECONDS = 600.0

def record_watchdog_restart(path: str | Path, reason: str, now: float | None = None) -> None:
    """Append one watchdog exit (wall-clock time and reason) to the restart file."""
    path = Path(path)
    now = time.time() if now is None else now
    entries = [e for e in _read_restarts(path) if now - float(e["at"]) <= WATCHDOG_RESTART_RETENTION]
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
        # T1 (outdoor) is always expected: missing from start it counts after a grace period.
        self.first_update: float | None = None
        # Alarm hysteresis and the text details of alarms that name something (rooms, sensors).
        self.clear_since: dict[str, float] = {}
        self.detail: dict[str, str] = {}
        # Afterheat: HAC1 level held high with no lift, and how long the level has been 0 (cold coil).
        self.level_high_since: float | None = None
        self.level_zero_since: float | None = None
        self.t2_samples: list[float] = []
        self.t2_sample_at = 0.0
        # Rpm per fan percent (learned, persisted) and how long each fan has held its percent.
        self.rpm_ref: dict[str, dict[str, float]] = {}
        self.fan_steady: dict[str, tuple[float, float]] = {}
        self.expected_pair: tuple[float | None, float | None] | None = None
        self.expected_since = 0.0
        # Values for the "stands still" check: name -> (value, since).
        self.frozen: dict[str, tuple[float, float]] = {}
        # Day -> {half-point bin: count} of the extract-side recovery, for the weekly trend.
        self.recovery_days: dict[str, dict[str, int]] = {}
        self.trend_at = 0.0
        self.trend: tuple[float | None, float | None] = (None, None)
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
                str(k): {"w": float(v["w"]), "n": float(v.get("n", 1)),
                         **({"h": float(v["h"])} if _num(v.get("h")) is not None else {})}
                for k, v in refs.items() if isinstance(v, dict) and _num(v.get("w"))}
        self.filter_hours = _num(saved.get("filter_hours"))
        self.filter_marker = saved.get("filter_marker")
        refs = saved.get("rpm_reference", {})
        if isinstance(refs, dict):
            self.rpm_ref = {str(k): {"v": float(v["v"]), "n": float(v.get("n", 1))}
                            for k, v in refs.items() if isinstance(v, dict) and _num(v.get("v"))}
        days = saved.get("recovery_days", {})
        if isinstance(days, dict):
            self.recovery_days = {str(d): {str(b): int(c) for b, c in bins.items() if _num(c) is not None}
                                  for d, bins in days.items() if isinstance(bins, dict)}
        samples = saved.get("t2_samples", [])
        if isinstance(samples, list):
            self.t2_samples = [float(v) for v in samples if _num(v) is not None][-T2_SAMPLES_MAX:]

    def save(self) -> None:
        if not self.path:
            return
        payload = {"energy": self.energy, "today": self.today,
                   "filter_reference": self.filter_reference, "filter_hours": self.filter_hours,
                   "filter_marker": self.filter_marker, "rpm_reference": self.rpm_ref,
                   "recovery_days": self.recovery_days, "t2_samples": self.t2_samples}
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
    @staticmethod
    def _clear_hold(code: str) -> float:
        if code in CLEAR_HOLD_LONG:
            return CLEAR_HOLD_LONG[code]
        if code in CLEAR_HOLD_OWN:
            return 0.0
        return max(CLEAR_HOLD_MIN, ALARM_DELAYS[code] / 2.0)

    def _median_recovery(self, today: str, days: int) -> float | None:
        """Median of the recorded extract-side recovery over the last ``days`` days (None: too little)."""
        first = datetime.fromtimestamp(datetime.strptime(today, "%Y-%m-%d").timestamp() - (days - 1) * 86400).strftime("%Y-%m-%d")
        total: dict[int, int] = {}
        for day, bins in self.recovery_days.items():
            if first <= day <= today:
                for key, count in bins.items():
                    total[int(key)] = total.get(int(key), 0) + count
        count = sum(total.values())
        if count < RECOVERY_TREND_MIN_SAMPLES:
            return None
        half, seen = count / 2.0, 0
        for key in sorted(total):
            seen += total[key]
            if seen >= half:
                return round(key / 2.0, 1)
        return None

    def _recovery_trend(self, now: float, extract_recovery: float | None, cold: bool, bypass: bool) -> bool:
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if extract_recovery is not None and cold and not bypass and 0 <= extract_recovery <= 105:
            bins = self.recovery_days.setdefault(today, {})
            key = str(int(round(extract_recovery * 2)))
            bins[key] = bins.get(key, 0) + 1
        if len(self.recovery_days) > RECOVERY_KEEP_DAYS:
            for day in sorted(self.recovery_days)[:-RECOVERY_KEEP_DAYS]:
                del self.recovery_days[day]
        if now - self.trend_at >= 300 or now < self.trend_at:
            self.trend_at = now
            week, month = self._median_recovery(today, 7), self._median_recovery(today, 30)
            self.trend = (week, month)
        week, month = self.trend
        return week is not None and month is not None and len(self.recovery_days) >= 14 \
            and week <= month - RECOVERY_TREND_POINTS

    def _fan_checks(self, s: dict, now: float, unit_frost: bool, quiet: bool) -> tuple[bool, bool]:
        """Rpm against the learned rpm of the same percent; the unit against the commanded profile."""
        speed_bad = False
        for fan in ("extract", "supply"):
            pct = _num(s.get(f"actual_fan_{fan}_percent"))
            rpm = _num(s.get(f"actual_fan_{fan}_rpm"))
            held = self.fan_steady.get(fan)
            if pct is None or rpm is None or pct <= 0 or quiet or (fan == "supply" and unit_frost):
                self.fan_steady.pop(fan, None)
                continue
            if held is None or held[0] != pct:
                self.fan_steady[fan] = held = (pct, now)
            if now - held[1] < RPM_STEADY_SECONDS:
                continue
            key = f"{fan}:{int(pct)}"
            ref = self.rpm_ref.get(key)
            if rpm <= 0:
                speed_bad = True
            elif ref is None or ref["n"] < RPM_REF_MIN:
                ref = self.rpm_ref.setdefault(key, {"v": rpm, "n": 0.0})
                ref["n"] += 1
                ref["v"] += (rpm - ref["v"]) / ref["n"]
            elif abs(rpm - ref["v"]) / ref["v"] > RPM_TOLERANCE:
                speed_bad = True
            else:
                # Follows slowly, and only values near the reference: a failing fan cannot drag it along.
                ref["n"] = min(RPM_REF_WEIGHT, ref["n"] + 1)
                ref["v"] += (rpm - ref["v"]) / ref["n"]
        # Following the commanded level: the unit's extract percent against the profile of the effective level.
        profile = s.get("effective_profile")
        wanted = _num(profile.get("extract")) if isinstance(profile, dict) else None
        wanted_pair = (wanted, _num(profile.get("supply")) if isinstance(profile, dict) else None)
        if wanted_pair != self.expected_pair:
            self.expected_pair, self.expected_since = wanted_pair, now
        actual = _num(s.get("actual_fan_extract_percent"))
        not_followed = (
            s.get("hardware_control_state") == "active_pi_master" and wanted is not None and actual is not None
            and not quiet and not unit_frost and now - self.expected_since >= FOLLOW_GRACE_SECONDS
            and abs(actual - wanted) > FOLLOW_TOLERANCE_PERCENT)
        return speed_bad, not_followed

    def _watch_frozen(self, name: str, value: float | None, now: float) -> bool:
        """True when ``value`` has not changed for FROZEN_SECONDS (a missing value resets the watch)."""
        if value is None:
            self.frozen.pop(name, None)
            return False
        held = self.frozen.get(name)
        if held is None or held[0] != value:
            self.frozen[name] = (value, now)
            return False
        return now - held[1] >= FROZEN_SECONDS

    def _extra_checks(self, s: dict, now: float, t1, t3, t4, bypass: bool, extract_recovery, cold: bool) -> dict:
        """The alarms added in 1.13.0-beta.14: coil frost, water, rooms, fans, frozen sensors, the Pi itself."""
        conditions: dict[str, bool] = {}
        detail: dict[str, str] = {}
        # Coil frost sensor (fresh value only, supplied by the runtime).
        frost = _num(s.get("coil_frost_temperature"))
        conditions["coil_frost"] = frost is not None and frost < COIL_FROST_C
        if conditions["coil_frost"]:
            detail["coil_frost"] = f"{frost:.1f} °C"
        # HAC1 level and the water pipes.
        level = _num(s.get("afterheat_level"))
        flow, back = _num(s.get("water_flow_temperature")), _num(s.get("water_return_temperature"))
        t2ah = _num(s.get("heating_coil_after_temperature"))
        conditions["afterheat_water_cold"] = bool(
            level is not None and ((level >= WATER_COLD_FLOW_LEVEL and flow is not None and flow < WATER_COLD_FLOW_C)
                                   or (level >= WATER_COLD_RETURN_LEVEL and back is not None and t2ah is not None
                                       and back < t2ah - WATER_COLD_RETURN_BELOW_K)))
        # High HAC1 level for a long time with next to no lift (corrected T2).
        lift = _num(s.get("afterheat_lift"))
        setpoint = _num(s.get("afterheat_effective_setpoint"))
        if setpoint is None:
            setpoint = _num(s.get("actual_afterheat_setpoint"))
        reached = setpoint is not None and t2ah is not None and t2ah >= setpoint - 1.0
        if (level is not None and level >= NO_LIFT_LEVEL and lift is not None and lift < NO_LIFT_K
                and not reached and s.get("actual_afterheat_outdoor_lockout") is not True):
            self.level_high_since = self.level_high_since or now
        else:
            self.level_high_since = None
        no_lift_high = self.level_high_since is not None and now - self.level_high_since >= NO_LIFT_SECONDS
        # A cold coil (HAC1 level 0 for a while, no heat in the pipes or T2AH equal to the T1+T3-T4 estimate)
        # shows the T2 sensor error directly: it is a suggestion for "Korrektion T2", nothing is changed.
        raw_t2 = _num(s.get("actual_supply_before_heater_temperature"))
        estimate = _num(s.get("actual_supply_before_heater_estimate"))
        if level == 0 and s.get("actual_afterheat") is not True:
            self.level_zero_since = self.level_zero_since or now
        else:
            self.level_zero_since = None
        if (self.level_zero_since is not None and now - self.level_zero_since >= T2_COLD_SECONDS
                and raw_t2 is not None and t2ah is not None
                and ((back is not None and back < t2ah + 2.0)
                     or (estimate is not None and abs(t2ah - estimate) <= 0.7))
                and now - self.t2_sample_at >= 60):
            self.t2_sample_at = now
            self.t2_samples = (self.t2_samples + [round(t2ah - raw_t2, 2)])[-T2_SAMPLES_MAX:]
        t2_suggested = None
        if len(self.t2_samples) >= T2_SAMPLES_MIN:
            ordered = sorted(self.t2_samples)
            t2_suggested = round(ordered[len(ordered) // 2], 2)
        # Pilot fallbacks and a setpoint rate that is too high (counts come from the pilot, 6 h / 1 h).
        conditions["afterheat_pilot_fallback"] = (_num(s.get("afterheat_fallbacks_6h")) or 0) >= PILOT_FALLBACK_LIMIT
        conditions["afterheat_rate_high"] = (_num(s.get("afterheat_rate_warnings_1h")) or 0) >= PILOT_RATE_WARNINGS_LIMIT
        conditions["afterheat_status_stale"] = s.get("afterheat_status_stale") is True
        # HA rooms with control: a CO2 or humidity sensor that is stale or missing.
        rooms_bad: list[str] = []
        online = s.get("smart_inputs_online") is True
        rooms = s.get("smart_ha_rooms") if isinstance(s.get("smart_ha_rooms"), dict) else {}
        quality = s.get("smart_room_quality") if isinstance(s.get("smart_room_quality"), dict) else {}
        missing = s.get("smart_missing_sensors") if isinstance(s.get("smart_missing_sensors"), dict) else {}
        if online:
            for name, room in rooms.items():
                if not isinstance(room, dict) or room.get("enabled") is False or room.get("control") is False:
                    continue
                per_kind = quality.get(name) if isinstance(quality.get(name), dict) else {}
                names = []
                for kind, label in (("co2", "CO₂-måleren"), ("humidity", "fugtmåleren")):
                    state = (per_kind.get(kind) or {}).get("state") if isinstance(per_kind.get(kind), dict) else None
                    gone = any(isinstance(k, str) and kind[:3] in k.lower().replace("co₂", "co2")
                               for k in (missing.get(name) or []))
                    if state == "stale" or gone:
                        names.append(label)
                if names:
                    rooms_bad.append(f"{' og '.join(names)} i {name}")
        conditions["room_sensor_missing"] = bool(rooms_bad)
        if rooms_bad:
            detail["room_sensor_missing"] = "; ".join(rooms_bad[:4])
        # Fans.
        unit_frost = s.get("actual_unit_frost_protection") is True
        quiet = (s.get("standby_active") is True or s.get("bonfire_active") is True
                 or bool(s.get("actual_bypass_travel_direction")) or s.get("fireplace") is True
                 or s.get("actual_fireplace") is True)
        speed_bad, not_followed = self._fan_checks(s, now, unit_frost, quiet)
        conditions["fan_speed_mismatch"] = speed_bad
        conditions["level_not_followed"] = not_followed
        # Sensors that stand still: the unit's own, and CO2 / humidity in the rooms with control.
        frozen: list[str] = []
        for name, label, value in (
                ("co2", "CO₂ (anlæg)", _num(s.get("unit_co2"))),
                ("t1", "T1", t1), ("t3", "T3", t3), ("t4", "T4", t4), ("t2ah", "T2AH", t2ah)):
            if self._watch_frozen(name, value, now):
                frozen.append(label)
        if online:
            for name, room in rooms.items():
                if not isinstance(room, dict) or room.get("enabled") is False or room.get("control") is False:
                    continue
                for kind, label in (("co2", "CO₂"), ("humidity", "fugt")):
                    per_kind = quality.get(name) if isinstance(quality.get(name), dict) else {}
                    state = (per_kind.get(kind) or {}).get("state") if isinstance(per_kind.get(kind), dict) else None
                    value = _num(room.get(kind)) if state in ("fresh", "unknown_age") else None
                    if self._watch_frozen(f"{name}:{kind}", value, now):
                        frozen.append(f"{label} i {name}")
        conditions["sensor_frozen"] = bool(frozen)
        if frozen:
            detail["sensor_frozen"] = ", ".join(frozen[:5])
        # The Pi itself.
        disk = _num(s.get("pi_disk_used_percent"))
        conditions["pi_disk_full"] = disk is not None and disk >= PI_DISK_PERCENT
        conditions["pi_undervoltage"] = s.get("pi_undervoltage_now") is True
        conditions["history_paused"] = s.get("history_recording_available") is False
        conditions["clock_unsynced"] = s.get("pi_clock_synced") is False
        conditions["recovery_trend"] = self._recovery_trend(now, extract_recovery, cold, bypass)
        return {"conditions": conditions, "detail": detail, "no_lift_high": no_lift_high,
                "t2_suggested": t2_suggested}

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
            if learning or reference is None:
                # Running mean of the clean power at this gear pair. A pair that has no reference
                # outside the first days after a filter change (the fan profile changed) gets one
                # now, marked with the filter hours it was learned at: from then on the check
                # shows a change from that day, not from clean filters.
                entry = self.filter_reference.setdefault(pair, {"w": average, "n": 0.0})
                if reference is None and not learning and hours is not None:
                    entry["h"] = hours
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

        extra = self._extra_checks(s, now, t1, t3, t4, bypass, extract_recovery, cold)

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
        if self.first_update is None:
            self.first_update = now
        if now - self.first_update >= OUTDOOR_EXPECTED_AFTER_SECONDS:
            self.seen_sensors.add("outdoor")
        for key in ("outdoor", "co2"):
            if health.get(key, {}).get("value") is not None:
                self.seen_sensors.add(key)
        restarts = self._watchdog_restarts(now)
        conditions = {
            # Only while the Pi may write: a released master has nothing pending.
            "controller_command_failed": bool(s.get("pending_commands"))
                and s.get("hardware_writes_allowed") is not False,
            # A CO2 sensor this unit never had is not stale; T1 always counts after the start grace.
            "local_sensor_stale": any(key in self.seen_sensors and health.get(key, {}).get("fresh") is False
                                      for key in ("outdoor", "co2")),
            "frost_risk": frost_state == "risk",
            "recovery_low": cold and supply_recovery is not None and supply_recovery < 60,
            "recovery_mismatch": cold and supply_recovery is not None and extract_recovery is not None
                and abs(supply_recovery - extract_recovery) > 20,
            "bypass_not_closing": cold and s.get("actual_bypass_request") in (False, "off", "OFF", 0)
                and not bypass and t2 is not None and t1 is not None and t3 is not None
                and (t2 - t1) < 0.25 * (t3 - t1),
            "afterheat_no_lift": (s.get("actual_afterheat") is True
                and s.get("actual_afterheat_outdoor_lockout") is not True
                and _num(s.get("afterheat_lift")) is not None and _num(s.get("afterheat_lift")) < 0.5
                and not afterheat_target_reached) or extra["no_lift_high"],
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
            **extra["conditions"],
        }
        self.detail = extra["detail"]
        for code, holds in conditions.items():
            if holds:
                self.clear_since.pop(code, None)
                self.started.setdefault(code, now)
                # The level-aware "no lift" has already waited NO_LIFT_SECONDS at the high level.
                delay = 0 if code == "afterheat_no_lift" and extra["no_lift_high"] else ALARM_DELAYS[code]
                if now - self.started[code] >= delay:
                    self.active.setdefault(code, now)
            elif code in self.active:
                # Hysteresis: an alarm clears only after its condition has been gone for a while.
                gone = self.clear_since.setdefault(code, now)
                if now - gone >= self._clear_hold(code):
                    self.active.pop(code, None)
                    self.started.pop(code, None)
                    self.clear_since.pop(code, None)
            else:
                self.started.pop(code, None)
                self.clear_since.pop(code, None)

        alarms = [
            {"code": code, "severity": ALARM_SEVERITY[code],
             "text": ALARM_TEXT[code] + (f": {self.detail[code]}" if self.detail.get(code) else ""),
             "since": int(since)}
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
            "filter_reference_status": (
                None if self.filter_pair is None else
                "Ingen reference for nuværende trin (læres nu)" if self.filter_pair not in self.filter_reference
                else "Reference lært senere end 72 timer efter filterskift" if "h" in self.filter_reference[self.filter_pair]
                else "Reference fra rene filtre"),
            "recovery_median_7d": self.trend[0],
            "recovery_median_30d": self.trend[1],
            "t2_offset_suggested_k": extra["t2_suggested"],
            "t2_offset_samples": len(self.t2_samples),
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
