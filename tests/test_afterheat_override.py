"""Supervised afterheat experiments: a short-lived, optionally fractional setpoint."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))

import controller_core
from controller_core import ControllerEngine, ControllerState, HardwareAdapter


class AfterheatOverrideTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.override = self.dir / "afterheat-override.json"
        patcher = mock.patch.object(controller_core, "AFTERHEAT_OVERRIDE_PATH", self.override)
        patcher.start()
        self.addCleanup(patcher.stop)
        state = ControllerState(self.dir / "controller.json")
        state.configure({"afterheat_coil": "water", "afterheat_setpoint": 21})
        self.writes = []
        hardware = HardwareAdapter(set_afterheat_setpoint=self.writes.append,
                                   set_fireplace=lambda *_: None, set_standby=lambda *_: None,
                                   write_fan_pair=lambda *_: None)
        self.engine = ControllerEngine(state, hardware)
        self.engine.retry_base_seconds = 0.0
        self.engine.frost_engage_seconds = 0
        self.engine.set_external({"outdoor_temperature": 10.0, "attic_temperature": 12.0,
                                  "afterheat_frost_temperature": 20.0})

    def write(self, setpoint, seconds=60):
        self.override.write_text(json.dumps({"setpoint": setpoint, "expires_at": time.time() + seconds}))
        os.utime(self.override, ns=(time.time_ns(), time.time_ns()))

    def test_no_file_means_normal_setpoint(self):
        result = self.engine.resolve()
        self.assertEqual(result["afterheat_command"], 21)
        self.assertIsNone(result["afterheat_override_setpoint"])

    def test_fractional_override_is_commanded_in_1_256_steps(self):
        self.write(21.5)
        result = self.engine.resolve()
        self.assertEqual(result["afterheat_command"], 21.5)
        self.assertEqual(result["afterheat_override_setpoint"], 21.5)
        self.engine.apply()
        self.assertEqual(self.writes[-1], 21.5)

    def test_expired_too_long_or_out_of_range_is_ignored(self):
        for setpoint, seconds in ((22.0, -5), (22.0, 3600), (9.0, 60), (36.0, 60)):
            with self.subTest(setpoint=setpoint, seconds=seconds):
                self.write(setpoint, seconds)
                self.assertEqual(self.engine.resolve()["afterheat_command"], 21)

    def test_frost_floor_still_applies(self):
        self.engine.set_external({"outdoor_temperature": -2.0, "attic_temperature": -1.0,
                                  "afterheat_frost_temperature": 20.0})
        self.write(15.0)
        result = self.engine.resolve()
        self.assertTrue(result["afterheat_frost_lock"])
        self.assertEqual(result["afterheat_command"], 18.0)

    def test_ignored_while_afterheat_is_off(self):
        self.engine.config.configure({"afterheat_enabled": False})
        self.write(25.0)
        self.assertIsNone(self.engine.resolve()["afterheat_command"])


class FractionalWriteTests(unittest.TestCase):
    def test_register_186_gets_setpoint_times_256(self):
        import importlib.machinery, types
        if "paho.mqtt.client" not in sys.modules:
            paho = types.ModuleType("paho"); mp = types.ModuleType("paho.mqtt"); mc = types.ModuleType("paho.mqtt.client")
            mc.__spec__ = importlib.machinery.ModuleSpec("paho.mqtt.client", loader=None)
            paho.mqtt = mp; mp.client = mc
            sys.modules.update({"paho": paho, "paho.mqtt": mp, "paho.mqtt.client": mc})
        try:
            import dantherm_gateway
        except ModuleNotFoundError as error:
            self.skipTest(str(error))
        gateway = dantherm_gateway.Gateway.__new__(dantherm_gateway.Gateway)
        gateway.state, gateway.mqtt_enabled = {}, False
        gateway.client = types.SimpleNamespace(publish=lambda *a, **k: None)
        gateway.prefix, gateway.retain = "x", True
        blocks = []
        gateway._write_afterheat_block = lambda ser, start, values, reason: blocks.append((start, values))
        gateway.write_afterheat_setpoint(None, 21.5)
        gateway.write_afterheat_setpoint(None, 21)
        gateway.write_afterheat_setpoint(None, None)
        self.assertEqual([values[1] for _start, values in blocks], [5504, 5376, 0])


if __name__ == "__main__":
    unittest.main()
