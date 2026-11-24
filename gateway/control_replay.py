"""Offline Local Auto comparison; never calls apply(), hardware or live config.save()."""
from __future__ import annotations
import copy
import logging
import math
import tempfile
import threading
import time
from pathlib import Path
from controller_core import ControllerEngine, ControllerError, ControllerState, HardwareAdapter

REPLAY_SETTINGS = frozenset({"co2_setpoint", "rh_setpoint", "co2_hysteresis", "rh_hysteresis",
    "auto_step_co2", "auto_step_rh", "local_normal_level", "air_quality_max_level",
    "downshift_delay_seconds", "boost_hold_seconds"})

# One replay at a time: each is CPU-heavy and shares the GIL with the live controller.
REPLAY_LOCK = threading.Lock()
REPLAY_BUSY_TEXT = "En prøvekørsel kører allerede"
# Let other threads run every this many history points.
REPLAY_YIELD_EVERY = 50


def _yield_now(index: int) -> None:
    if index % REPLAY_YIELD_EVERY == REPLAY_YIELD_EVERY - 1:
        time.sleep(0)


class MemoryState(ControllerState):
    def save(self):
        pass


def replay_local_auto(settings: dict, samples: list[dict], patch: dict) -> dict:
    if not isinstance(patch, dict) or set(patch) - REPLAY_SETTINGS:
        raise ControllerError("Prøvekørsel accepterer kun Local Auto-grænser og trin")
    if len(samples) > 720:
        raise ControllerError("Højst 720 historikpunkter pr. prøve")
    with tempfile.TemporaryDirectory(prefix="hch-replay-") as tmp:
        engines = []
        for variant in ({}, patch):
            state = MemoryState(Path(tmp)/"unused.json")
            state.data = copy.deepcopy(settings)
            state.configure(variant)
            engines.append(ControllerEngine(state, HardwareAdapter()))
        output = []
        previous = None
        covered = changed = 0.0
        for index, sample in enumerate(sorted(samples, key=lambda row: row.get("ts") or 0)):
            _yield_now(index)
            ts = sample.get("ts")
            if not isinstance(ts, (int, float)) or not math.isfinite(ts):
                continue
            gap = None if previous is None else ts - previous
            if gap is not None and not 0 < gap <= 300:
                for engine in engines:
                    engine.current_auto_level = None
                    engine.boost_until = engine.last_level_change = 0
            co2, rh = sample.get("co2"), sample.get("humidity")
            valid = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            # Old databases did not record RH. We never invent it or carry it across a gap.
            if not valid(co2) and not valid(rh):
                previous = None
                continue
            levels = []
            for engine in engines:
                engine.update_measurements(co2=co2, rh=rh, outdoor=sample.get("outdoor_temp"), room=sample.get("extract_temp"))
                levels.append(engine._local_auto_level(ts)[0])
            if output and gap is not None and 0 < gap <= 300:
                covered += gap
                if output[-1]["current_level"] != output[-1]["proposed_level"]:
                    changed += gap
            output.append({"ts": ts, "current_level": levels[0], "proposed_level": levels[1]})
            previous = ts
        return {"scope": "local_auto", "points": output, "covered_minutes": round(covered/60, 1),
                "different_minutes": round(changed/60, 1), "humidity_available": any(
                    isinstance(s.get("humidity"), (int, float)) for s in samples),
                "note": "Local Auto på gemte målinger. Ingen rumhistorik, ugeplan, vinterbegrænsning eller termisk forudsigelse. Ingen indstillinger ændres."}


def replay_smart_auto(settings: dict, samples: list[dict], patch: dict) -> dict:
    """Replay Smart Auto demand, including priorities, RH episodes and holds.

    Deliberately does not invoke resolve/apply: HAC1/afterheat, schedules and
    physical response are outside this recorded-input demand comparison.
    """
    import time
    from controller_runtime import ControllerRuntime
    allowed = REPLAY_SETTINGS | {'bathroom_rh_setpoint', 'bathroom_max_level', 'pm25_setpoint',
                                 'pm25_max_level', 'bathroom_rise_percent', 'room_setup', 'room_priorities'}
    if not isinstance(patch, dict) or set(patch) - allowed:
        raise ControllerError('Ugyldige Smart Auto-prøveindstillinger')
    priorities = patch.get('room_priorities', {})
    if (not isinstance(priorities, dict) or len(priorities) > 32 or any(
            not isinstance(name, str) or value not in {'auto', 'low', 'normal', 'high', 'critical'}
            for name, value in priorities.items())):
        raise ControllerError('Ugyldige rumprioriteter')
    if len(samples) > 1441:
        raise ControllerError('Højst 1441 rumoptagelser pr. prøve')
    with tempfile.TemporaryDirectory(prefix='hch-smart-replay-') as tmp:
        runtimes = []
        for index, variant in enumerate(({}, patch)):
            path = Path(tmp)/str(index)/'unused.json'
            runtime = ControllerRuntime(gateway_state={}, hardware=HardwareAdapter(), state_path=path)
            config = MemoryState(path)
            config.data = copy.deepcopy(settings)
            config.configure({key: value for key, value in variant.items() if key != 'room_priorities'})
            if index == 1:
                for room in config.data.get('room_setup') or []:
                    if room.get('name') in priorities:
                        room['priority'] = priorities[room['name']]
            runtime.config = config
            runtime.engine = ControllerEngine(config, HardwareAdapter())
            runtimes.append(runtime)
        output = []
        covered = different = 0.0
        previous = None
        for sample_index, sample in enumerate(sorted(samples, key=lambda row: row.get('ts') or 0)):
            _yield_now(sample_index)
            ts = sample.get('ts')
            if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts):
                continue
            gap = None if previous is None else ts-previous
            if gap is None or not 0 < gap <= 180:
                for runtime in runtimes:
                    runtime.engine.current_auto_level = None
                    runtime.engine.boost_until = runtime.engine.last_level_change = 0
                    runtime._room_rh_history.clear()
                    runtime._bathroom_episodes.clear()
            decisions = []
            for index, runtime in enumerate(runtimes):
                runtime.replay_time = ts
                runtime.smart_rooms = copy.deepcopy(sample.get('rooms') or {})
                if index == 1:
                    for name, priority in priorities.items():
                        if name in runtime.smart_rooms:
                            runtime.smart_rooms[name]['priority'] = priority
                runtime.smart_inputs_received_at = ts if sample.get('ha_online') is True else None
                runtime.smart_inputs_received_monotonic = None
                runtime.gateway_state = dict(sample.get('local') or {})
                runtime.gateway_state['hrc2_t5_temperature_sample_monotonic'] = time.monotonic()
                runtime.engine.external = dict(sample.get('external') or {})
                local = runtime.gateway_state
                runtime.engine.update_measurements(rh=local.get('humidity'), co2=local.get('co2'),
                                                  outdoor=local.get('outdoor_temp'), room=local.get('extract_temp'))
                for name, values in runtime._fresh_ha_rooms().items():
                    humidity = values.get('humidity')
                    if isinstance(humidity, (int, float)):
                        history = runtime._room_rh_history[name]
                        history.append((ts, humidity))
                        while history and ts-history[0][0] > 1800:
                            history.popleft()
                if sample.get('ha_online') is True:
                    decision = runtime._derive_smart_decision(ts)
                    runtime._enforce_air_quality_max(decision)
                    level, held = runtime.engine._stabilize_auto_level(decision[0], ts)
                    reason, room, metric = decision[2], decision[3], decision[4]
                    if held:
                        reason += '; '+held
                else:
                    level, reason = runtime.engine._local_auto_level(ts)
                    room, metric = None, None
                    reason = 'HA offline; '+reason
                decisions.append((level, reason, room, metric))
            if output and gap is not None and 0 < gap <= 180:
                covered += gap
                if output[-1]['current_level'] != output[-1]['proposed_level']:
                    different += gap
            output.append({'ts': ts, 'current_level': decisions[0][0], 'proposed_level': decisions[1][0],
                           'current_reason': decisions[0][1], 'proposed_reason': decisions[1][1],
                           'current_room': decisions[0][2], 'proposed_room': decisions[1][2],
                           'observed_level': sample.get('observed_level'), 'observed_source': sample.get('observed_source')})
            previous = ts
        return {'scope': 'smart_auto_demand', 'points': output,
                'covered_minutes': round(covered/60, 1), 'different_minutes': round(different/60, 1),
                'humidity_available': True,
                'note': 'Smart Auto-behov på optagne rumdata: prioritet, CO₂, fugt, PM2.5, badeværelsesudtørring og holdetider. '
                        'Ugeplan, nat, vinter-/tørluftbegrænsning, eftervarme og fysisk respons simuleres ikke. Ingen indstillinger ændres.'}


# ---------------------------------------------------------------- afterheat
AFTERHEAT_REPLAY_SETTINGS = frozenset({"afterheat_target_offset", "afterheat_pi_band", "afterheat_pi_min_dwell_ticks"})
AFTERHEAT_TARGET_OFFSET_RANGE = (-3.0, 3.0)
AFTERHEAT_REPLAY_MAX_ROWS = 12 * 60 + 30
# The simulated plant and the Pi control advance this many seconds at a time
# (the gateway polls HAC1 every ~3 s; 4 s keeps a 12 h replay light on a Pi).
AFTERHEAT_REPLAY_STEP_S = 4.0
# Recorded minutes further apart than this start a new stretch.
AFTERHEAT_REPLAY_GAP_S = 180
AFTERHEAT_REPLAY_MIN_ROWS = 10
# Left out: afterheat off (4), cooling (5), temporary trial (7), external master (8).
AFTERHEAT_REPLAY_SKIP_CONTEXTS = (4, 5, 7, 8)
AFTERHEAT_FROST_CONTEXT = 1
# History from before the operating context was recorded: the frost lock is
# taken to hold while T1 is within the lock's release margin of its limit.
FROST_RELEASE_MARGIN_C = 2.0
_QUIET_THREADS: set[int] = set()
# Weight of each measured step given to a replay pilot's level model at the start.
PILOT_SEED_WEIGHT = 3.0


class _QuietReplayThreads(logging.Filter):
    """Drop the pilot's log lines from a replay thread; the live pilot keeps logging."""

    def filter(self, record: logging.LogRecord) -> bool:
        return threading.get_ident() not in _QUIET_THREADS


def _quiet_pilot_log():
    from afterheat_pilot import LOG as PILOT_LOG
    if not any(isinstance(item, _QuietReplayThreads) for item in PILOT_LOG.filters):
        PILOT_LOG.addFilter(_QuietReplayThreads())


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


class _Coil:
    """Heat of the levels seen in the air after the coil: dead time, then a first-order lag."""

    def __init__(self, now: float, heat: float, dead_time: float, lag: float) -> None:
        self.queue = [(now, heat)]
        self.x = heat
        self.dead_time, self.lag = dead_time, lag

    def step(self, now: float, heat: float, dt: float) -> float:
        self.queue.append((now, heat))
        while len(self.queue) > 1 and self.queue[1][0] <= now - self.dead_time:
            self.queue.pop(0)
        self.x += (self.queue[0][1] - self.x) * (1 - math.exp(-dt / self.lag))
        return self.x


def _afterheat_patch(settings: dict, patch: dict) -> dict:
    from afterheat_pilot import PI_BAND_DEFAULT, PI_BAND_RANGE, PI_MIN_DWELL_TICKS_DEFAULT, PI_MIN_DWELL_TICKS_RANGE
    if not isinstance(patch, dict) or set(patch) - AFTERHEAT_REPLAY_SETTINGS:
        raise ControllerError("Prøvekørsel af eftervarmen accepterer kun ændring af mål, komfortbånd og mindste tid på et trin")

    def number(key, default, low, high, whole=False):
        value = patch.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ControllerError(f"Ugyldig værdi for {key}")
        if whole and int(value) != value:
            raise ControllerError(f"Ugyldig værdi for {key}")
        return int(value) if whole else float(value)

    band = _finite(settings.get("afterheat_pi_band"))
    dwell = settings.get("afterheat_pi_min_dwell_ticks")
    current = {"band": band if band is not None else PI_BAND_DEFAULT,
               "min_dwell_ticks": int(dwell) if isinstance(dwell, int) and not isinstance(dwell, bool) else PI_MIN_DWELL_TICKS_DEFAULT,
               "target_offset": 0.0}
    proposed = {"band": number("afterheat_pi_band", current["band"], *PI_BAND_RANGE),
                "min_dwell_ticks": number("afterheat_pi_min_dwell_ticks", current["min_dwell_ticks"], *PI_MIN_DWELL_TICKS_RANGE, whole=True),
                "target_offset": number("afterheat_target_offset", 0.0, *AFTERHEAT_TARGET_OFFSET_RANGE)}
    return {"current": current, "proposed": proposed}


def _afterheat_stretches(rows: list[dict]) -> list[list[dict]]:
    stretches, current, previous = [], [], None
    for row in sorted(rows, key=lambda item: item.get("ts") or 0):
        ts = _finite(row.get("ts"))
        fan, level, active = (_finite(row.get(key)) for key in ("fan_supply_percent", "afterheat_level", "afterheat_active"))
        usable = (ts is not None and _finite(row.get("heating_coil_after_temperature")) is not None
                  and _finite(row.get("afterheat_target")) is not None and fan is not None and fan > 0
                  and level is not None and 0 <= level <= 10 and active is not None and active > 0
                  and row.get("afterheat_context") not in AFTERHEAT_REPLAY_SKIP_CONTEXTS)
        if not usable or (previous is not None and not 0 < ts - previous <= AFTERHEAT_REPLAY_GAP_S):
            if len(current) >= AFTERHEAT_REPLAY_MIN_ROWS:
                stretches.append(current)
            current = []
        if usable:
            current.append(row)
            previous = ts
        else:
            previous = None
    if len(current) >= AFTERHEAT_REPLAY_MIN_ROWS:
        stretches.append(current)
    return stretches


class _Metrics:
    def __init__(self) -> None:
        self.seconds = self.abs_error = self.in_band = 0.0
        self.over = self.under = None
        self.changes = 0

    def add(self, error: float, dt: float) -> None:
        self.seconds += dt
        self.abs_error += abs(error) * dt
        self.in_band += dt if abs(error) <= 0.5 else 0.0
        self.over = error if self.over is None else max(self.over, error)
        self.under = -error if self.under is None else max(self.under, -error)

    def result(self) -> dict:
        hours = self.seconds / 3600
        return {"level_changes": self.changes,
                "level_changes_per_hour": round(self.changes / hours, 1) if hours > 0 else None,
                "mean_abs_error": round(self.abs_error / self.seconds, 2) if self.seconds else None,
                "in_band_percent": round(100 * self.in_band / self.seconds, 1) if self.seconds else None,
                "max_over": None if self.over is None else round(max(0.0, self.over), 2),
                "max_under": None if self.under is None else round(max(0.0, self.under), 2)}


def learned_steps(pilot) -> list[float | None]:
    """The live pilot's lasting step model (K at the reference fan); None for steps never measured. Memory read only."""
    try:
        heat = pilot.heat
        return [round(heat.lasting_step(index), 2) if heat.lasting_den[index] > 0 else None
                for index in range(len(heat.lasting_den))]
    except Exception:  # noqa: BLE001 - a replay never depends on the live pilot
        return []


def replay_afterheat(settings: dict, rows: list[dict], patch: dict, reference_steps: list | None = None,
                     pilot_steps: list | None = None) -> dict:
    """Closed-loop replay of the Pi afterheat control with today's and proposed target and band.

    The air before the coil, fan and water come from the stored minutes. The
    coil is the recorded air plus the difference the simulated levels make
    (heat per level as the live pilot has learned it, else measured from
    the history, else the pilot's prior), so with the recorded levels it reproduces the recording.
    HAC1 is modelled as measured: a ~60 s tick, one step per tick against the
    whole-degree setpoint. New AfterheatPilot objects only: no live state,
    config or hardware is touched.
    """
    from afterheat_pilot import (FAN_REFERENCE_PERCENT, AfterheatPilot, LEVEL_MAX, MODEL_DEAD_TIME_S, MODEL_LAG_S, PI_GAIN_DEFAULT,
                                 PI_INTEGRAL_MINUTES_DEFAULT, PRIOR_STEP_K, STEP_K_RANGE, TICK_PERIOD_S, PilotInputs)
    from history_analysis import heat_scale
    variants = _afterheat_patch(settings, patch)
    if len(rows) > AFTERHEAT_REPLAY_MAX_ROWS:
        raise ControllerError("Højst 12 timers historik pr. prøve")
    # Heat of each level step: what the live pilot has learned, else measured in the history, else the prior.
    steps, sources = [], []
    for index in range(LEVEL_MAX):
        learned = _finite(pilot_steps[index]) if pilot_steps and index < len(pilot_steps) else None
        measured = _finite(reference_steps[index]) if reference_steps and index < len(reference_steps) else None
        value, source = ((learned, "learned") if learned is not None else (measured, "history") if measured is not None
                         else (PRIOR_STEP_K, "prior"))
        sources.append(source)
        steps.append(min(STEP_K_RANGE[1], max(STEP_K_RANGE[0], value)))
    cumulative = [0.0]
    for step in steps:
        cumulative.append(cumulative[-1] + step)
    frost_enabled = settings.get("frost_lock_enabled", True) is not False
    frost_outdoor = _finite(settings.get("frost_lock_outdoor_c"))
    frost_outdoor = 3.0 if frost_outdoor is None else frost_outdoor
    frost_floor = _finite(settings.get("frost_lock_min_setpoint_c"))
    frost_floor = 18.0 if frost_floor is None else frost_floor
    frost_minutes = 0

    def frost(row: dict) -> bool:
        context = row.get("afterheat_context")
        if context is not None:
            return context == AFTERHEAT_FROST_CONTEXT
        outdoor = _finite(row.get("outdoor_temp"))
        return frost_enabled and outdoor is not None and outdoor <= frost_outdoor + FROST_RELEASE_MARGIN_C

    gain = _finite(settings.get("afterheat_pi_gain")) or PI_GAIN_DEFAULT
    integral = _finite(settings.get("afterheat_pi_integral_minutes")) or PI_INTEGRAL_MINUTES_DEFAULT
    metrics = {"actual": _Metrics(), "current": _Metrics(), "proposed": _Metrics()}
    points, covered, tick_index = [], 0.0, 0
    _quiet_pilot_log()
    _QUIET_THREADS.add(threading.get_ident())
    try:
        for stretch in _afterheat_stretches(rows):
            first = stretch[0]
            start = float(first["ts"])
            level0 = int(round(first["afterheat_level"]))

            def heat(level: int, row: dict) -> float:
                return cumulative[level] * heat_scale(float(row["fan_supply_percent"]), _finite(row.get("flow_temperature")),
                                                      float(row["afterheat_target"]))

            actual_coil = _Coil(start, heat(level0, first), MODEL_DEAD_TIME_S, MODEL_LAG_S)
            runs = {}
            for name in ("current", "proposed"):
                pilot = AfterheatPilot()
                # Start from what the live pilot would have learned: the measured steps.
                for index, source in enumerate(sources):
                    if source != "prior":
                        pilot.heat.learn(index, steps[index], PILOT_SEED_WEIGHT, start)
                runs[name] = {"pilot": pilot, "level": level0, "changed_at": None,
                              "setpoint": None, "next_tick": start + TICK_PERIOD_S / 2,
                              "coil": _Coil(start, heat(level0, first), MODEL_DEAD_TIME_S, MODEL_LAG_S),
                              "air": float(first["heating_coil_after_temperature"]), **variants[name]}
            for index in range(1, len(stretch)):
                row, previous = stretch[index], stretch[index - 1]
                t0, t1 = float(previous["ts"]), float(row["ts"])
                air0, air1 = float(previous["heating_coil_after_temperature"]), float(row["heating_coil_after_temperature"])
                target = float(previous["afterheat_target"])
                recorded_level = int(round(previous["afterheat_level"]))
                frozen = frost(previous)
                frost_minutes += frozen
                metrics["actual"].add(air0 - target, t1 - t0)
                if int(round(row["afterheat_level"])) != recorded_level:
                    metrics["actual"].changes += 1
                covered += t1 - t0
                t = t0
                while t < t1 - 1e-6:
                    _yield_now(tick_index)
                    tick_index += 1
                    dt = min(AFTERHEAT_REPLAY_STEP_S, t1 - t)
                    t += dt
                    recorded_air = air0 + (air1 - air0) * (t - t0) / (t1 - t0)
                    base = recorded_air - actual_coil.step(t, heat(recorded_level, previous), dt)
                    for name, run in runs.items():
                        run_target = target + run["target_offset"]
                        run["air"] = air = base + run["coil"].step(t, heat(run["level"], previous), dt)
                        if t >= run["next_tick"]:
                            run["next_tick"] += TICK_PERIOD_S
                            native = max(round(run_target), math.ceil(frost_floor)) if frozen else round(run_target)
                            setpoint = run["setpoint"] if run["setpoint"] is not None else native
                            whole = math.floor(setpoint)
                            new = run["level"] + 1 if air < whole else run["level"] - 1 if air >= whole + 0.95 else run["level"]
                            # Under the frost lock HAC1 keeps at least level 1.
                            new = max(1 if frozen else 0, min(LEVEL_MAX, new))
                            if new != run["level"]:
                                run["level"], run["changed_at"] = new, t
                                metrics[name].changes += 1
                        result = run["pilot"].update(PilotInputs(
                            now=t, mode="pi", afterheat_on=True, target=run_target,
                            frost_lock=frozen, frost_floor=frost_floor,
                            level=run["level"], level_age=0.0, level_changed_at=run["changed_at"],
                            t2ah=air, t2ah_age=0.0, t2ah_raw=air,
                            fan_supply=float(previous["fan_supply_percent"]), water_flow=_finite(previous.get("flow_temperature")),
                            outdoor=_finite(previous.get("outdoor_temp")), extract=_finite(previous.get("extract_temp")),
                            master=True, gain=gain, integral_minutes=integral,
                            band=run["band"], min_dwell_ticks=run["min_dwell_ticks"]))
                        sent = result.get("afterheat_hac1_setpoint")
                        run["setpoint"] = float(sent) if isinstance(sent, (int, float)) and not isinstance(sent, bool) else None
                        metrics[name].add(air - run_target, dt)
                points.append({"ts": t1, "target": round(float(row["afterheat_target"]), 2),
                               "proposed_target": round(float(row["afterheat_target"]) + variants["proposed"]["target_offset"], 2),
                               "actual_air": round(air1, 2), "actual_level": int(round(row["afterheat_level"])),
                               "current_air": round(runs["current"]["air"], 2), "current_level": runs["current"]["level"],
                               "proposed_air": round(runs["proposed"]["air"], 2), "proposed_level": runs["proposed"]["level"]})
    finally:
        _QUIET_THREADS.discard(threading.get_ident())
    return {"scope": "afterheat", "points": points, "covered_minutes": round(covered / 60, 1),
            "frost_minutes": frost_minutes,
            "summary": {name: item.result() for name, item in metrics.items()},
            "settings": variants, "live_mode": settings.get("afterheat_control_mode") or "native",
            "model": {"steps_k": [round(step, 2) for step in steps], "step_sources": sources,
                      "reference_fan_percent": FAN_REFERENCE_PERCENT, "tick_seconds": TICK_PERIOD_S},
            "note": ("Pi-styringen af eftervarmen køres i en model af fladen på gemte minutter: luft før fladen, blæser og vand "
                     "som optaget, varme pr. trin som Pi-styringen har lært det, ellers målt i historikken, ellers 1,5 K ved 27 % blæser, HAC1 med ét trin pr. "
                     "minut og mindst trin 1 under frostsikring. Slukket eftervarme, køl, forsøg og ekstern master udelades. "
                     "Faktiske trinskift tælles på minutmålinger og kan være for få. Sammenlign forslaget med nuværende "
                     "indstillinger i samme model; forskellen til det faktiske viser modellens usikkerhed. Vejledende; ingen indstillinger ændres og intet sendes til anlægget.")}
