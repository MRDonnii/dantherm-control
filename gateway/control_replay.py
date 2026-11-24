"""Offline Local Auto comparison; never calls apply(), hardware or live config.save()."""
from __future__ import annotations
import copy
import math
import tempfile
from pathlib import Path
from controller_core import ControllerEngine, ControllerError, ControllerState, HardwareAdapter

REPLAY_SETTINGS = frozenset({"co2_setpoint", "rh_setpoint", "co2_hysteresis", "rh_hysteresis",
    "auto_step_co2", "auto_step_rh", "local_normal_level", "air_quality_max_level",
    "downshift_delay_seconds", "boost_hold_seconds"})

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
        for sample in sorted(samples, key=lambda row: row.get("ts") or 0):
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
        for sample in sorted(samples, key=lambda row: row.get('ts') or 0):
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
