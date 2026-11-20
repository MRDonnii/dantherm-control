"""HAC1 205/206 glitches (a few seconds too warm) must not reach state."""
import importlib.machinery
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "gateway"))

if "paho.mqtt.client" not in sys.modules:
    paho = types.ModuleType("paho")
    mqtt_package = types.ModuleType("paho.mqtt")
    mqtt_client = types.ModuleType("paho.mqtt.client")
    mqtt_client.__spec__ = importlib.machinery.ModuleSpec("paho.mqtt.client", loader=None)
    paho.mqtt = mqtt_package
    mqtt_package.client = mqtt_client
    sys.modules.update({"paho": paho, "paho.mqtt": mqtt_package, "paho.mqtt.client": mqtt_client})

import dantherm_gateway
from dantherm_gateway import Gateway

T2AH = "heating_coil_after_temperature"
FROST = "heating_coil_frost_temperature"


class _Mqtt:
    def publish(self, *_args, **_kwargs):
        return None


def make_gateway():
    gateway = Gateway.__new__(Gateway)
    gateway.device_id, gateway.device_name, gateway.prefix = "dantherm_hch5", "Dantherm HCH5", "dantherm/hch5"
    gateway.discovery, gateway.client, gateway.retain = "homeassistant", _Mqtt(), True
    gateway.control_enabled = gateway.fireplace_enabled = gateway.filter_enabled = False
    gateway.fireplace_gateway_active = False
    gateway.mqtt_enabled = False
    gateway.state = {}
    gateway.t2ah_spike_filter_enabled = True
    return gateway


class SpikeFilterTests(unittest.TestCase):
    def feed(self, gateway, key, samples):
        """samples: list of (monotonic seconds, raw value); returns published values."""
        published = []
        for at, raw in samples:
            with mock.patch.object(dantherm_gateway.time, "monotonic", return_value=1000.0 + at):
                gateway.publish_temperature(key, raw)
            published.append(gateway.state.get(key))
        return published

    def test_the_0311_glitch_never_reaches_state(self):
        # Frost 18.1 -> 23.8 for ~5 s and T2AH 21.1 -> 30.8 for ~9 s, read every ~3 s
        # by the 205 block and every 10 s by the snapshot.
        gateway = make_gateway()
        frost = self.feed(gateway, FROST, [(0, 18.1), (3, 23.8), (3.5, 23.8), (6, 23.8), (9, 18.0)])
        t2ah = self.feed(gateway, T2AH, [(0, 21.1), (3, 30.8), (3.5, 30.8), (6, 30.8), (9, 30.8), (12, 21.1)])
        self.assertEqual(frost, [18.1, 18.1, 18.1, 18.1, 18.0])
        self.assertEqual(t2ah, [21.1] * 6)
        self.assertEqual(gateway.state["frost_rejected_samples"], 3)
        self.assertEqual(gateway.state["t2ah_rejected_samples"], 4)
        self.assertEqual(gateway.state["frost_last_raw_temperature"], 18.0)

    def test_a_real_drop_shows_at_once(self):
        gateway = make_gateway()
        values = self.feed(gateway, FROST, [(0, 18.0), (3, 12.0), (6, 7.5)])
        self.assertEqual(values, [18.0, 12.0, 7.5])

    def test_a_fast_real_rise_lags_at_most_the_window_and_never_freezes(self):
        gateway = make_gateway()
        # 0.3 K/s after the valve opens, read every 3 s, with one 9 s gap.
        times = [0, 3, 6, 15, 18, 21, 24, 27, 30, 33, 36, 39, 42, 45, 48, 51]
        values = self.feed(gateway, T2AH, [(t, round(16.8 + 0.3 * t, 2)) for t in times])
        for t, shown in zip(times, values):
            self.assertGreaterEqual(shown, round(16.8 + 0.3 * max(0, t - 15), 2) - 1e-9)
        self.assertAlmostEqual(values[-1], round(16.8 + 0.3 * 36, 2))

    def test_a_glitch_as_the_first_reading_is_replaced_by_the_next_real_one(self):
        gateway = make_gateway()
        values = self.feed(gateway, FROST, [(0, 23.8), (3, 18.1), (6, 18.1)])
        self.assertEqual(values, [23.8, 18.1, 18.1])

    def test_readings_older_than_the_window_do_not_count(self):
        gateway = make_gateway()
        values = self.feed(gateway, T2AH, [(0, 20.0), (16, 24.0)])
        self.assertEqual(values, [20.0, 24.0])

    def test_every_reading_keeps_the_sensor_fresh(self):
        gateway = make_gateway()
        self.feed(gateway, FROST, [(0, 18.0), (3, 25.0)])
        self.assertEqual(gateway.state["heating_coil_frost_temperature_sample_monotonic"], 1003.0)

    def test_filter_can_be_switched_off(self):
        gateway = make_gateway()
        gateway.t2ah_spike_filter_enabled = False
        values = self.feed(gateway, T2AH, [(0, 21.0), (3, 30.8)])
        self.assertEqual(values, [21.0, 30.8])

    def test_other_keys_are_untouched(self):
        gateway = make_gateway()
        values = self.feed(gateway, "outdoor_temp", [(0, -1.0), (3, 9.0)])
        self.assertEqual(values, [-1.0, 9.0])


if __name__ == "__main__":
    unittest.main()
