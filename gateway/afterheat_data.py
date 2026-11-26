"""Read-only data about the afterheat model for Home Assistant (the block ``afterheat_ha``).

Plain numbers only; every value is None when unknown. Nothing here steers the coil.

* ``Telemetry``: one sample a minute of air/target/level for the performance over 1 h and 24 h,
  and the coil's heat in kWh today (integrated from the heat in kW, reset at local midnight).
  The kWh is kept in the algorithm store, so a restart does not lose the day.
* ``build_block``: the compact block the token (read scope) can fetch in /api/controller/state.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque

from afterheat_control import LEVEL_MAX, SOFT_FLOOR_K, CoilModel, OutputSpec, num

SAMPLE_EVERY_S = 55.0
KEEP_S = 24 * 3600
GAP_S = 180.0  # a longer hole in the samples or the heat is not bridged
WINDOWS = {"1h": 3600.0, "24h": 86400.0}
STATS_CACHE_S = 30.0
PREDICT_MINUTES = 10
LOG = logging.getLogger("afterheat-control")
# HAC1 alone (afterheat_control_mode native, setpoint 22), live 2026-11-26 07:10-09:30 at 36 % fan:
# the air's spread (standard deviation) and its share under target - 0.5 K. The MPC's own
# 2 h are compared with these (MpcWatch); worse is only logged, nothing is switched.
HAC1_REF_SD_K = 1.27
HAC1_REF_UNDER_PERCENT = 24.0
WATCH_SPAN_S = 2 * 3600.0
WATCH_EVERY_S = 30 * 60.0


class Telemetry:
    def __init__(self) -> None:
        self.samples: deque[tuple[float, float, float, float | None]] = deque()  # ts, air, target, level
        self.energy_date: str | None = None
        self.energy_kwh = 0.0
        self._power_at: float | None = None
        self._power_kw: float | None = None
        self._stats: tuple[float, dict] | None = None

    # ------------------------------------------------------------ samples
    def sample(self, now: float, air: float | None, target: float | None, level: float | None) -> None:
        if num(air) is None or num(target) is None:
            return
        if self.samples and now - self.samples[-1][0] < SAMPLE_EVERY_S:
            return
        self.samples.append((now, float(air), float(target), num(level)))
        while self.samples and self.samples[0][0] < now - KEEP_S:
            self.samples.popleft()

    def performance(self, now: float) -> dict[str, dict[str, float | None]]:
        if self._stats is not None and 0 <= now - self._stats[0] < STATS_CACHE_S:
            return self._stats[1]
        result: dict[str, dict[str, float | None]] = {}
        for name, span in WINDOWS.items():
            rows = [s for s in self.samples if s[0] >= now - span]
            item: dict[str, float | None] = {"mean_error_k": None, "air_min_c": None, "under_percent": None,
                                             "changes_per_hour": None, "minutes": len(rows)}
            if rows:
                item["mean_error_k"] = round(sum(r[1] - r[2] for r in rows) / len(rows), 2)
                item["air_min_c"] = round(min(r[1] for r in rows), 1)
                item["under_percent"] = round(100.0 * sum(1 for r in rows if r[1] < r[2] - SOFT_FLOOR_K) / len(rows), 1)
                changes, covered = 0, 0.0
                for a, b in zip(rows, rows[1:]):
                    if 0 < b[0] - a[0] <= GAP_S:
                        covered += b[0] - a[0]
                        if a[3] is not None and b[3] is not None and round(a[3]) != round(b[3]):
                            changes += 1
                if covered >= 900:
                    item["changes_per_hour"] = round(changes / (covered / 3600.0), 1)
            result[name] = item
        self._stats = (now, result)
        return result

    # ------------------------------------------------------------- energy
    def add_power(self, now: float, power_w: float | None) -> None:
        """The coil's heat now (W, None unknown): integrate the one before over the time since."""
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if today != self.energy_date:
            self.energy_date, self.energy_kwh = today, 0.0
            self._power_at = self._power_kw = None
        if self._power_at is not None and now - self._power_at < 1.0:
            return
        if self._power_at is not None and self._power_kw is not None and 0 < now - self._power_at <= GAP_S:
            self.energy_kwh += self._power_kw * (now - self._power_at) / 3600.0
        self._power_at = now
        watts = num(power_w)
        self._power_kw = None if watts is None else max(0.0, watts) / 1000.0

    def energy_today(self, now: float) -> float | None:
        if self.energy_date != time.strftime("%Y-%m-%d", time.localtime(now)):
            return None if self.energy_date is None else 0.0
        return round(self.energy_kwh, 3)

    def energy_dict(self) -> dict[str, object]:
        return {"date": self.energy_date, "kwh": round(self.energy_kwh, 4)}

    def load_energy(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        kwh = num(data.get("kwh"))
        if isinstance(data.get("date"), str) and kwh is not None and 0 <= kwh < 1000:
            self.energy_date, self.energy_kwh = data["date"], kwh


class MpcWatch:
    """The MPC's own last 2 h of steering against HAC1 alone (HAC1_REF_*): a warning in the log when worse.

    Only samples while the MPC steers count; a pause or another algorithm starts it over.
    Read-only: it never changes the mode or the algorithm.
    """

    def __init__(self) -> None:
        self.samples: deque[tuple[float, float, float]] = deque()  # ts, air, target
        self.checked_at: float | None = None
        self.last: dict[str, float] | None = None

    def reset(self) -> None:
        self.samples.clear()
        self.checked_at = None

    def sample(self, now: float, air: float | None, target: float | None) -> None:
        if num(air) is None or num(target) is None:
            return
        if self.samples and now - self.samples[-1][0] > GAP_S:
            self.samples.clear()  # a hole: the 2 h must be the MPC's own, unbroken
        if not self.samples or now - self.samples[-1][0] >= SAMPLE_EVERY_S:
            self.samples.append((now, float(air), float(target)))
        while self.samples and self.samples[0][0] < now - WATCH_SPAN_S:
            self.samples.popleft()
        self.check(now)

    def stats(self) -> dict[str, float] | None:
        rows = list(self.samples)
        if len(rows) < 2 or rows[-1][0] - rows[0][0] < 0.95 * WATCH_SPAN_S:
            return None
        errors = [air - target for _t, air, target in rows]
        mean = sum(errors) / len(errors)
        sd = math.sqrt(sum((e - mean) ** 2 for e in errors) / len(errors))
        under = 100.0 * sum(1 for e in errors if e < -SOFT_FLOOR_K) / len(errors)
        return {"mean_k": round(mean, 2), "sd_k": round(sd, 2), "under_percent": round(under, 1),
                "air_max_c": round(max(r[1] for r in rows), 1)}

    def check(self, now: float) -> None:
        if self.checked_at is not None and now - self.checked_at < WATCH_EVERY_S:
            return
        stats = self.stats()
        if stats is None:
            return
        self.checked_at, self.last = now, stats
        worse = [f"spredning {stats['sd_k']:.2f} K > {HAC1_REF_SD_K:.2f} K"] if stats["sd_k"] > HAC1_REF_SD_K else []
        if stats["under_percent"] > HAC1_REF_UNDER_PERCENT:
            worse.append(f"under mål − 0,5 °C {stats['under_percent']:.1f} % > {HAC1_REF_UNDER_PERCENT:.1f} %")
        if worse:
            LOG.warning("Afterheat MPC-vagt: de sidste 2 timer er MPC dårligere end HAC1 alene (%s); "
                        "middel %+.2f K, højeste %.1f °C. Intet skiftes automatisk.",
                        ", ".join(worse), stats["mean_k"], stats["air_max_c"])
        else:
            LOG.info("Afterheat MPC-vagt: de sidste 2 timer spredning %.2f K, under %.1f %%, middel %+.2f K "
                     "(HAC1 alene: %.2f K, %.1f %%)", stats["sd_k"], stats["under_percent"], stats["mean_k"],
                     HAC1_REF_SD_K, HAC1_REF_UNDER_PERCENT)


# ------------------------------------------------------------- predictions
def predict(model: CoilModel, now: float) -> dict[str, object]:
    """The air 1 and 10 min ahead with the output kept (the model's own view; cheap: ~30 steps)."""
    if model.x is None or model.updated is None or now - model.updated > 300 or not model.outputs:
        return {}
    spec = OutputSpec(period=60.0, apply_delay_s=0.0)
    path = model.simulate(now, [model.outputs[-1][1]] * PREDICT_MINUTES, spec, model.last_scale, model.base(None),
                          step_s=20.0)
    per_minute = [path[i] for i in range(2, len(path), 3)]
    return {"air_1min": per_minute[0], "air_10min": per_minute[-1], "air_min": min(per_minute)}


def mean_abs_error(model: CoilModel) -> float | None:
    """Mean absolute error (K) of the minute-ahead predictions over the last hour."""
    errors = [abs(e) for _t, e in model.check_errors]
    return sum(errors) / len(errors) if len(errors) >= 5 else None


def _round(value: object, digits: int) -> float | None:
    number = num(value)
    return None if number is None else round(number, digits)


def build_block(control, telemetry: Telemetry, state: dict, forecast: list | None, prediction: dict,
                pid_parts: tuple | None, now: float) -> dict[str, object]:
    """The compact block. ``state`` is the controller snapshot (afterheat_* keys); ``forecast`` the
    algorithm's own per-minute forecast (MPC), else ``prediction`` from the model alone."""
    model = control.model
    status = control.status_summary()
    level = num(state.get("afterheat_level"))
    scale = model.last_scale
    step_now = step_next = None
    if level is not None:
        lvl = int(round(level))
        if 1 <= lvl <= LEVEL_MAX:
            step_now = scale * (model.lift[lvl] - model.lift[lvl - 1])
        if 0 <= lvl < LEVEL_MAX:
            step_next = scale * (model.lift[lvl + 1] - model.lift[lvl])
    air_1 = air_10 = air_low = None
    if forecast:
        air_1, air_low = forecast[0], min(forecast)
        air_10 = forecast[9] if len(forecast) >= 10 else None
    if air_1 is None:
        air_1, air_10, air_low = prediction.get("air_1min"), prediction.get("air_10min"), prediction.get("air_min")
    last = model.reid_last
    refit = None if not last else {"at": _round(last.get("at"), 0), "adopted": last.get("accepted") == 1.0,
                                   "before_k": _round(last.get("prior_rmse"), 3), "after_k": _round(last.get("fitted_rmse"), 3)}
    power_w = num(state.get("afterheat_power_w"))
    pid = None
    if control.name == "pid" and pid_parts is not None and state.get("afterheat_algorithm_active") == "pid":
        air, target = num(state.get("heating_coil_after_temperature")), num(state.get("afterheat_target"))
        pid = {"output": _round(sum(pid_parts), 2), "error_k": _round(None if air is None or target is None else target - air, 2)}
    perf = telemetry.performance(now)
    return {
        "algorithm": state.get("afterheat_algorithm_active"),
        "label": state.get("afterheat_algorithm_label") if state.get("afterheat_algorithm_active") else None,
        "why": state.get("afterheat_algorithm_why"),
        "model_state": status["state"], "samples": model.samples,
        "step_k": _round(step_now if step_now is not None else step_next, 2), "step_next_k": _round(step_next, 2),
        "tau_up_s": round(model.tau_up), "tau_down_s": round(model.tau_down), "dead_s": round(model.dead),
        "offset_k": round(model.offset, 2),
        "refit": refit,
        "air_1min_c": _round(air_1, 2), "air_10min_c": _round(air_10, 2), "air_lowest_c": _round(air_low, 2),
        "prediction_error_k": _round(mean_abs_error(model), 2),
        "heat_kw": None if power_w is None else round(max(0.0, power_w) / 1000.0, 3),
        "energy_today_kwh": telemetry.energy_today(now),
        "pid": pid,
        "performance": perf,
    }
