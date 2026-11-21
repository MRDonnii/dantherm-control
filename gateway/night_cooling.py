"""Night cooling from the weather forecast, and the outdoor history it needs.

In the cooling season (5-day running mean of the outdoor temperature above
``night_cool_season_c``) a hot day ahead (DMI forecast high for the next 24 h
at or above ``night_cool_forecast_c``) or behind (today's high at or above
``night_cool_today_c``) lets the controller open the bypass and raise the
fan level during the night window, while the outdoor air is at least
``night_cool_delta_k`` cooler than the rooms and the rooms are warmer than
``night_cool_min_indoor_c``. Fireplace, standby, bonfire, vacation, manual
control and the frost lock win (the caller passes them as ``blocked``).

Kept free of the controller so the rules are easy to read and test.
"""
from __future__ import annotations

import math
from datetime import datetime

# Daily outdoor means kept for the running mean (days).
SEASON_DAYS = 5
# A day counts in the running mean with at least this many hours of samples.
MIN_DAY_HOURS = 6.0
# The night cooling stops once the outdoor air is less than this much cooler
# than the rooms (start needs the full delta).
STOP_MARGIN_K = 1.0
# Stopped because the rooms reached the minimum: restart only this much above it.
RESTART_ABOVE_MIN_K = 0.5
# Kept on at least this long unless something wins or the window ends.
MIN_ON_SECONDS = 15 * 60


def _minutes(text: str) -> int:
    hours, minutes = str(text).split(":")
    return int(hours) * 60 + int(minutes)


def in_window(now: datetime, start: str, end: str) -> bool:
    """True inside start..end (local time); the window may run past midnight."""
    current = now.hour * 60 + now.minute
    begin, finish = _minutes(start), _minutes(end)
    if begin == finish:
        return False
    if begin < finish:
        return begin <= current < finish
    return current >= begin or current < finish


class OutdoorHistory:
    """Daily outdoor means (for the season) and today's local high.

    ``days`` is [[date, mean, hours], ...] for completed days; it is saved by
    the caller (in the controller config) when a day completes, so a restart
    keeps the season. Samples are weighted by the time since the previous one
    (capped), so an irregular tick does not skew the mean.
    """

    MAX_GAP_SECONDS = 600.0

    def __init__(self, days: object = None) -> None:
        self.days: list[list[object]] = self.clean(days)
        self.today: str | None = None
        self.weighted_sum = 0.0
        self.seconds = 0.0
        self.high: float | None = None
        self.last_at: float | None = None

    @staticmethod
    def clean(value: object) -> list[list[object]]:
        result: list[list[object]] = []
        if isinstance(value, list):
            for item in value[-SEASON_DAYS:]:
                try:
                    day, mean, hours = str(item[0])[:10], float(item[1]), float(item[2])
                except (TypeError, ValueError, IndexError):
                    continue
                if -50 <= mean <= 60 and 0 <= hours <= 25:
                    result.append([day, round(mean, 2), round(hours, 2)])
        return result

    def add(self, value: float | None, now: float) -> bool:
        """One outdoor sample. Returns True when a day was completed (save the days)."""
        date = datetime.fromtimestamp(now).astimezone().date().isoformat()
        completed = False
        if self.today is not None and date != self.today:
            hours = self.seconds / 3600
            if hours >= MIN_DAY_HOURS:
                self.days = [day for day in self.days if day[0] != self.today]
                self.days.append([self.today, round(self.weighted_sum / self.seconds, 2), round(hours, 2)])
                self.days = self.days[-SEASON_DAYS:]
                completed = True
            self.weighted_sum, self.seconds, self.high, self.last_at = 0.0, 0.0, None, None
        self.today = date
        if value is None or not math.isfinite(value):
            self.last_at = None
            return completed
        if self.last_at is not None:
            weight = min(self.MAX_GAP_SECONDS, max(0.0, now - self.last_at))
            self.weighted_sum += value * weight
            self.seconds += weight
        self.last_at = now
        self.high = value if self.high is None else max(self.high, value)
        return completed

    def running_mean(self) -> float | None:
        """Mean of the last completed days plus today so far (if long enough)."""
        means = [float(day[1]) for day in self.days[-SEASON_DAYS:]]
        if self.seconds / 3600 >= MIN_DAY_HOURS:
            means = means[-(SEASON_DAYS - 1):] + [self.weighted_sum / self.seconds]
        elif not means and self.seconds >= 3600:
            means = [self.weighted_sum / self.seconds]  # first hours after an install
        return round(sum(means) / len(means), 2) if means else None


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


class NightCooling:
    """The night cooling decision with its latch and minimum on time."""

    def __init__(self) -> None:
        self.active = False
        self.on_at: float | None = None
        self.hot_latched = False
        self.stopped_cool = False  # stopped at the indoor minimum: wait for RESTART_ABOVE_MIN_K
        self.state = "disabled"
        self.reason = ""

    def _off(self, state: str, reason: str) -> dict[str, object]:
        self.active, self.on_at = False, None
        self.state, self.reason = state, reason
        return self.result()

    def result(self) -> dict[str, object]:
        return {"night_cool_active": self.active, "night_cool_state": self.state, "night_cool_reason": self.reason}

    def update(self, *, now: float, local: datetime, settings: dict, indoor: float | None, outdoor: float | None,
               season_mean: float | None, forecast_high: float | None, today_high: float | None,
               blocked: str | None) -> dict[str, object]:
        d = settings
        if not d.get("night_cool_enabled"):
            self.hot_latched = self.stopped_cool = False
            return self._off("disabled", "Natkøling er slået fra")
        window = in_window(local, str(d["night_cool_start"]), str(d["night_cool_end"]))
        if not window:
            self.hot_latched = self.stopped_cool = False
            return self._off("outside_window", f"Uden for natvinduet {d['night_cool_start']}–{d['night_cool_end']}")
        if blocked:
            return self._off("blocked", f"Natkøling venter: {blocked}")
        season_c = float(d["night_cool_season_c"])
        if season_mean is None:
            return self._off("no_season", "Natkøling venter: udetemperaturens 5-døgns middel kendes ikke endnu")
        if season_mean <= season_c:
            return self._off("not_season", f"Ikke kølesæson (5-døgns middel ude {season_mean:.1f} °C ≤ {season_c:.0f} °C)".replace(".", ","))
        forecast = _num(forecast_high)
        today = _num(today_high)
        hot_tomorrow = forecast is not None and forecast >= float(d["night_cool_forecast_c"])
        hot_today = today is not None and today >= float(d["night_cool_today_c"])
        if hot_tomorrow or hot_today:
            # Latched for the rest of the window: after midnight "today's high" starts over.
            self.hot_latched = True
        if hot_tomorrow:
            why = f"varm dag i morgen ({forecast:.0f} °C)"
        elif hot_today:
            why = f"varm dag i dag ({today:.0f} °C)"
        elif self.hot_latched:
            why = "varm dag"
        else:
            if forecast is None and today is None:
                return self._off("no_forecast", "Natkøling venter: ingen vejrudsigt fra Home Assistant")
            return self._off("no_hot_day", "Ingen varm dag i vejrudsigten")
        if indoor is None or outdoor is None:
            return self._off("sensor_missing", "Natkøling venter: mangler inde- eller udetemperatur")
        delta = float(d["night_cool_delta_k"])
        minimum = float(d["night_cool_min_indoor_c"])
        status = f"{why}, ude {outdoor:.0f} °C, inde {indoor:.1f} °C".replace(".", ",")
        held = self.active and self.on_at is not None and now - self.on_at < MIN_ON_SECONDS
        if self.active:
            if indoor <= minimum:
                self.stopped_cool = True
                return self._off("indoor_satisfied", f"Natkøling stoppet: inde {indoor:.1f} °C ≤ {minimum:.1f} °C".replace(".", ","))
            if outdoor > indoor - max(0.2, delta - STOP_MARGIN_K) and not held:
                return self._off("not_cooler_outside", f"Natkøling stoppet: ude {outdoor:.1f} °C er ikke køligere nok".replace(".", ","))
            self.state, self.reason = "active", f"Natkøling: {status}"
            return self.result()
        restart_min = minimum + (RESTART_ABOVE_MIN_K if self.stopped_cool else 0.0)
        if indoor <= restart_min:
            return self._off("indoor_satisfied", f"Natkøling ikke nødvendig: inde {indoor:.1f} °C".replace(".", ","))
        if outdoor > indoor - delta:
            return self._off("not_cooler_outside", f"Natkøling venter: ude {outdoor:.1f} °C er ikke {delta:.0f} K køligere end inde".replace(".", ","))
        self.active, self.on_at, self.stopped_cool = True, now, False
        self.state, self.reason = "active", f"Natkøling: {status}"
        return self.result()


__all__ = ["NightCooling", "OutdoorHistory", "in_window", "SEASON_DAYS"]
