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
