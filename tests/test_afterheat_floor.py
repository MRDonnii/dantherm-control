"""The soft floor and the ping-pong guard of the Pi afterheat control (1.13.0-beta.9)."""
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import afterheat_pilot as pilot_module
import afterheat_sim as sim
from afterheat_pilot import AfterheatPilot, PilotInputs, TICK_PERIOD_S, hac1_action

START = 1_000_000.0


def per_tick_runs(levels):
    """Longest run of consecutive HAC1 ticks that each changed the level."""
    longest = run = 0
    for a, b in zip(levels, levels[1:]):
        run = run + 1 if a != b else 0
        longest = max(longest, run)
    return longest


class SoftFloorTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_rlv2_turns_back_up_before_the_air_dips(self):
        # RLV at 2 turns, 36 % fan: level 1 settles ~1.7 K cold and level 2 ~4.5 K
        # warm, so the Pi takes turns. Live 2026-11-25 (beta.8) the air dipped to
        # 19-20 C (target 22) every round and HAC1 and the Pi took turns every
        # tick for up to 27 min. Now the turn up comes before the falling air
        # passes target - 0.5 K, and the level changes stay calm.
        plant = sim.Hac1Plant(start=START, **sim.RLV2_VALVES, water_amp=3.0, seed=3, level=1, setpoint=22.0)
        ticks = []
        tick = plant._tick

        def logged_tick():
            tick()
            ticks.append(plant.level)
        plant._tick = logged_tick

        def coil_warm(inp, p, _gateway):
            inp.before_heater, inp.frost_sensor = p.air_in(), 20.0
        rows = sim.run(AfterheatPilot(), plant, target=22.0, minutes=180, frost_lock=True, frost_floor=18.0,
                       inputs_hook=coil_warm)
        late = [r for r in rows if r["t"] >= 30 * 60]
        below = sum(1 for r in late if r["air"] < 22.0 - pilot_module.SOFT_FLOOR_K) / len(late)
        mean = sum(r["air"] - 22.0 for r in late) / len(late)
        hours = (late[-1]["t"] - late[0]["t"]) / 3600
        changes = sum(1 for a, b in zip(late, late[1:]) if a["level"] != b["level"]) / hours
        self.assertLessEqual(below, 0.05)
        self.assertGreaterEqual(min(r["air"] for r in late), 22.0 - 1.0)
        self.assertTrue(0.0 <= mean <= 2.0, mean)
        self.assertLessEqual(changes, 20.0)
        self.assertLess(per_tick_runs(ticks[30:]), 4)


class PingPongGuardTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_guard_keeps_the_level_on_the_targets_side(self):
        # HAC1 flips 1 <-> 2 on four ticks in a row with the air 4 K warm (live
        # 2026-11-25 13:15-13:45). The Pi stops taking HAC1's steps back, keeps
        # the lower level a few ticks and never asks for the upper one meanwhile.
        pilot = AfterheatPilot()
        level, changed_at, air = 1, None, 26.0
        actions, guarded = [], 0.0
        for i in range(int(10 * 60 / 2)):
            now = START + 2 * i
            tick = int((now - START - 5.0) // TICK_PERIOD_S)
            at = START + 5.0 + tick * TICK_PERIOD_S
            if 0 < now - at <= 2.0:  # HAC1's tick
                if 1 <= tick <= 4:
                    new = 2 if level == 1 else 1  # its own flips
                elif pilot.setpoint is not None:
                    action = hac1_action(pilot.setpoint, air)
                    new = level + {"up": 1, "down": -1}.get(action, 0)
                else:
                    new = level
                if new != level:
                    level, changed_at = max(0, new), at + 1.0
            result = pilot.update(PilotInputs(
                now=now, mode="pi", afterheat_on=True, target=22.0, level=level, level_age=1.0,
                level_changed_at=changed_at, t2ah=air, t2ah_age=1.0, t2ah_raw=air, fan_supply=36.0,
                outdoor=5.0, extract=21.0, master=True))
            if result["afterheat_pingpong_guard"]:
                guarded = guarded or now
                actions.append(result["afterheat_next_action"])
                if now - guarded > TICK_PERIOD_S + 5:
                    self.assertLessEqual(level, 1)  # down to the lower level at the next tick, and kept
        self.assertTrue(guarded)
        self.assertNotIn("up", actions)
        self.assertEqual(pilot.revert_level, None)


if __name__ == "__main__":
    unittest.main()
