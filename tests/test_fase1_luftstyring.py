"""Phase 1 (1.13.0-beta.12): the air control does the right thing.

One small regression test per item of the plan "Luftstyringen gør det rigtige".
"""
import importlib.machinery
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

if "paho.mqtt.client" not in sys.modules:
    paho = types.ModuleType("paho")
    mqtt_package = types.ModuleType("paho.mqtt")
    mqtt_client = types.ModuleType("paho.mqtt.client")
    mqtt_client.__spec__ = importlib.machinery.ModuleSpec("paho.mqtt.client", loader=None)
    paho.mqtt = mqtt_package
    mqtt_package.client = mqtt_client
    sys.modules.update({"paho": paho, "paho.mqtt": mqtt_package, "paho.mqtt.client": mqtt_client})

import room_setup  # noqa: E402
from controller_core import HA_LEASE_GRACE_SECONDS, ControllerEngine, ControllerState, HardwareAdapter  # noqa: E402
from controller_runtime import FIREPLACE_COLD_SECONDS, ControllerRuntime  # noqa: E402

LIVE = {"fan_step_count": 6, "mode": "smart_auto", "local_normal_level": 4, "local_min_level": 2, "air_quality_max_level": 5,
        "co2_setpoint": 800, "co2_hysteresis": 100, "auto_step_co2": 200, "pm25_enabled": True,
        "pm25_max_level": 4, "pm25_setpoint": 25, "pm25_step": 15, "pm25_hysteresis": 5,
        "bathroom_rh_setpoint": 65, "bathroom_rh_hysteresis": 5, "bathroom_max_level": 5,
        "bathroom_max_minutes": 15, "bathroom_rise_percent": 7, "bathroom_rise_minutes": 10}


def runtime(tmp_path, **settings):
    r = ControllerRuntime(gateway_state={"bus_traffic": False}, hardware=HardwareAdapter(),
                          state_path=tmp_path / "controller.json")
    r.config.configure({**LIVE, **settings})
    return r


def decide(r, rooms, now=None):
    r.room_inputs({"source": "home_assistant", "valid_for_s": 180, "rooms": rooms})
    if now is not None:
        return r._derive_smart_decision(now)
    return r.smart_requested_level, r.smart_demand, r.smart_reason, r.smart_controlling_room, r.smart_controlling_metric


def with_outdoor_room(r):
    r.config.configure({"room_setup": [
        room_setup.clean_room({"name": "Udeluft", "type": "outdoor", "sensors": {"pm25": "sensor.ude_pm25"}}),
        room_setup.clean_room({"name": "Køkken", "type": "kitchen"}),
    ]})


# 1. PM2.5
def test_clean_pm25_never_holds_the_base_level(tmp_path):
    r = runtime(tmp_path)
    level, *_rest = decide(r, {"Soveværelse": {"co2": 600, "pm25": 1}})
    assert level == 3  # CO2 alone wants one step under the base level


def test_pm25_lifts_over_the_base_level_even_when_its_max_equals_it(tmp_path):
    r = runtime(tmp_path)
    level, _d, reason, _room, metric = decide(r, {"Køkken": {"co2": 650, "pm25": 108}})
    assert (level, metric) == (5, "pm25") and "PM2.5" in reason


def test_pm25_does_not_pull_in_dirtier_outdoor_air(tmp_path):
    r = runtime(tmp_path)
    with_outdoor_room(r)
    assert decide(r, {"Køkken": {"pm25": 35}, "Udeluft": {"pm25": 60}})[0] == 4
    assert decide(r, {"Køkken": {"pm25": 35}, "Udeluft": {"pm25": 4}})[0] == 5


# 2. Fireplace
def test_missing_stove_room_is_reported_and_a_room_of_that_type_stands_in(tmp_path):
    r = runtime(tmp_path, fireplace_auto_enabled=True, fireplace_auto_source="room:Brændeovn")
    r._update_fireplace_auto()
    assert r.fireplace_auto_reason == "source_missing"
    assert r.snapshot()["fireplace_auto_source_status"] == "missing"
    assert r.rooms_view()["missing_source_rooms"] == ["Brændeovn"]
    r.config.configure({"room_setup": [room_setup.clean_room({"name": "Pejs", "type": "source"})]})
    decide(r, {"Pejs": {"temperature": 30.0}, "Stue": {"co2": 600}})
    r.refresh_measurements()
    assert r.engine.external["stove_temperature"] == 30.0
    r._update_fireplace_auto()
    assert r.fireplace_auto_active and r.fireplace_auto_reason == "stove_temperature"


def test_cold_guard_ends_fireplace_after_five_cold_minutes(tmp_path):
    r = runtime(tmp_path, fireplace_cold_guard_enabled=True)
    r.gateway_state.update({"outdoor_temp": 2.0})
    r.config.configure({"fireplace": True, "fireplace_minutes": 30})
    t0 = time.time()
    r._update_fireplace_auto(now=t0)
    assert r.config.data["fireplace"] is True
    r._update_fireplace_auto(now=t0 + FIREPLACE_COLD_SECONDS + 1)
    assert r.config.data["fireplace"] is False and r.fireplace_cold_stopped
    # Off by default: nothing stops it.
    (tmp_path / "off").mkdir()
    r2 = runtime(tmp_path / "off")
    r2.gateway_state.update({"outdoor_temp": 2.0})
    r2.config.configure({"fireplace": True, "fireplace_minutes": 30})
    r2._update_fireplace_auto(now=t0)
    r2._update_fireplace_auto(now=t0 + FIREPLACE_COLD_SECONDS + 1)
    assert r2.config.data["fireplace"] is True


class FakeHardware(HardwareAdapter):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.set_fireplace = lambda on: self.calls.append(on)


def test_controller_confirms_a_running_fireplace_every_minute(tmp_path):
    from controller_core import FIREPLACE_REFRESH_SECONDS
    state = ControllerState(tmp_path / "c.json")
    hardware = FakeHardware()
    engine = ControllerEngine(state, hardware)
    state.configure({"fireplace": True, "fireplace_minutes": 30})
    engine._call("fireplace", True, hardware.set_fireplace, True, refresh_seconds=FIREPLACE_REFRESH_SECONDS)
    engine.last_applied_at["fireplace"] -= FIREPLACE_REFRESH_SECONDS + 1
    engine._call("fireplace", True, hardware.set_fireplace, True, refresh_seconds=FIREPLACE_REFRESH_SECONDS)
    assert hardware.calls == [True, True]


def gateway_with_unit(reg76, pair):
    from dantherm_gateway import Gateway
    g = Gateway.__new__(Gateway)
    g.fireplace_gateway_active = False
    g.fireplace_restore = None
    g.startup_fireplace = None
    g.special_mode_flag = 0
    g.fireplace_state_path = Path("/nonexistent/fireplace-state.json")
    g.writes = []
    g.write_one = lambda _ser, register, value: g.writes.append((register, value))
    g.read_fan_pair = lambda _ser: pair
    g.read_register_block = lambda _ser, _slave, register, _count: [reg76 if register == 76 else 0]
    g.publish = lambda *a, **k: None
    g.save_fireplace_state = lambda: None
    return g


def test_gateway_timer_is_pushed_on_by_the_controller(tmp_path):
    g = gateway_with_unit(0, (55, 43))
    g.fireplace_gateway_active = True
    g.fireplace_until_monotonic = time.monotonic() + 5
    g.extend_fireplace()
    assert g.fireplace_until_monotonic > time.monotonic() + 14 * 60


def test_restart_during_fireplace_is_cleared_or_adopted():
    g = gateway_with_unit(1, (0, 43))
    g.adopt_or_clear_saved_fireplace(None, keep=False)
    assert (76, 0) in g.writes and (66, 43) in g.writes and not g.fireplace_gateway_active
    g = gateway_with_unit(1, (0, 43))
    g.adopt_or_clear_saved_fireplace(None, keep=True)
    assert g.fireplace_gateway_active and g.fireplace_restore == (0, 0, 43, 43)
    g = gateway_with_unit(1, (0, 0))  # standby pattern: left alone
    g.adopt_or_clear_saved_fireplace(None, keep=False)
    assert g.writes == []


# 3. Night level
def test_night_level_applies_while_air_is_acceptable(tmp_path):
    r = runtime(tmp_path, night_enabled=True, night_start="00:00", night_end="23:59", night_level=3,
                night_air_quality_max_level=4)
    decide(r, {"Soveværelse": {"co2": 716}})
    result = r.engine.resolve()
    assert (result["effective_level"], result["effective_source"]) == (3, "night")


# 4. Calm fan
def test_co2_band_needs_half_the_hysteresis_to_step_down(tmp_path):
    r = runtime(tmp_path)
    assert decide(r, {"Sov": {"co2": 805}})[0] == 5
    assert decide(r, {"Sov": {"co2": 795}})[0] == 5   # 795 > 800 - 50
    assert decide(r, {"Sov": {"co2": 749}})[0] == 4


def test_step_down_needs_lower_demand_for_the_whole_delay(tmp_path):
    state = ControllerState(tmp_path / "c.json")
    state.configure({"downshift_delay_seconds": 300})
    engine = ControllerEngine(state, HardwareAdapter())
    engine.current_auto_level, engine.last_level_change = 5, 0.0
    assert engine._stabilize_auto_level(4, 1000.0) == (5, "Downshift delay")
    assert engine._stabilize_auto_level(4, 1299.0)[0] == 5
    assert engine._stabilize_auto_level(4, 1300.0)[0] == 4


def test_upshift_delay_is_off_by_default_and_waits_when_set(tmp_path):
    state = ControllerState(tmp_path / "c.json")
    engine = ControllerEngine(state, HardwareAdapter())
    assert state.data["upshift_delay_seconds"] == 0
    engine.current_auto_level = 4
    assert engine._stabilize_auto_level(5, 100.0)[0] == 5
    state.configure({"upshift_delay_seconds": 120})
    engine.current_auto_level = 4
    assert engine._stabilize_auto_level(5, 1000.0) == (4, "Upshift delay")
    assert engine._stabilize_auto_level(5, 1120.0)[0] == 5
    engine.current_auto_level = 3
    assert engine._stabilize_auto_level(5, 2000.0)[0] == 5  # two steps go at once


def test_short_ha_gap_keeps_smart_auto(tmp_path):
    r = runtime(tmp_path)
    decide(r, {"Sov": {"co2": 900}})
    seen = r.config.data["ha_last_seen"]
    result = r.engine.resolve(seen + 180 + HA_LEASE_GRACE_SECONDS - 5)
    assert result["effective_source"] == "ha_smart" and result["effective_level"] == 5
    assert r.engine.resolve(seen + 180 + HA_LEASE_GRACE_SECONDS + 5)["effective_source"] == "local_fallback"


def test_local_fallback_does_not_keep_an_inherited_level(tmp_path):
    state = ControllerState(tmp_path / "c.json")
    state.configure({"fan_step_count": 6, "local_normal_level": 4, "downshift_delay_seconds": 300})
    engine = ControllerEngine(state, HardwareAdapter())
    engine.current_auto_level, engine.auto_level_owner, engine.last_level_change = 5, "ha", 0.0
    engine.update_measurements(co2=730)  # between 700 and 800: the old hysteresis held 5 for hours
    assert engine._local_auto_level(1000.0)[0] == 5
    assert engine._local_auto_level(1300.0)[0] == 4


# 5. Bathroom
def test_second_shower_after_max_drying_time_dries_again(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    t0 = time.time()
    clock = {"now": t0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])

    def at(minutes, humidity):
        clock["now"] = t0 + minutes * 60
        return decide(r, {"Badeværelse": {"humidity": humidity, "room_type": "bathroom"}})

    assert at(0, 70.0)[3:] == ("Badeværelse", "humidity")
    assert at(16, 66.0)[3] is None   # longest drying time passed: quiet
    assert at(30, 66.0)[3] is None   # a flat value stays quiet
    level, _d, _reason, room, metric = at(40, 75.0)  # a new shower
    assert (room, metric) == ("Badeværelse", "humidity") and level == 5


def test_bathroom_wins_a_tie_with_co2_so_night_drying_holds(tmp_path):
    r = runtime(tmp_path)
    decide(r, {"Badeværelse": {"humidity": 72.0, "room_type": "bathroom"}, "Sov": {"co2": 857}})
    assert r.config.data["ha_bathroom_drying"] is True


def test_humidity_rise_can_be_ignored_in_dry_air(tmp_path):
    r = runtime(tmp_path, humidity_rise_min_rh=45)
    t0 = time.time()
    r._room_rh_history["Køkken"].extend([(t0 - 300, 28.0)])
    assert decide(r, {"Køkken": {"humidity": 36.0}}, now=t0)[4] != "rh_rise"
    r.config.configure({"humidity_rise_min_rh": 0})  # the default: as before
    assert decide(r, {"Køkken": {"humidity": 36.0}}, now=t0)[4] == "rh_rise"


# 6. Rooms and reasons
def test_high_priority_does_not_raise_clean_air(tmp_path):
    r = runtime(tmp_path, pm25_enabled=False)
    assert decide(r, {"Sov": {"co2": 650, "priority": "high"}})[0] == 3
    assert decide(r, {"Sov": {"co2": 900, "priority": "high"}})[0] == min(r.config.max_level(), 5)


def test_reason_says_the_air_is_fine_at_the_base_level(tmp_path):
    r = runtime(tmp_path, pm25_enabled=False)
    assert decide(r, {"Sov": {"co2": 750}})[2] == "Luften er fin · normal ventilation"


def test_boost_hold_uses_the_top_level_of_a_four_step_unit(tmp_path):
    state = ControllerState(tmp_path / "c.json")
    state.configure({"fan_step_count": 4})
    engine = ControllerEngine(state, HardwareAdapter())
    engine.current_auto_level = 3
    engine._stabilize_auto_level(4, 100.0)
    assert engine.boost_until > 100.0
