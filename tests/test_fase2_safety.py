"""Regression tests for phase 2 (driftssikkerhed)."""

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

from master_arbitration import MasterArbitrator, crc16


def _fc06(slave, register, value):
    body = bytes([slave, 6, register >> 8, register & 0xFF, value >> 8, value & 0xFF])
    return body + crc16(body).to_bytes(2, "little")


def test_master_arbitrator_is_thread_safe():
    """safety-2: serial, controller and HTTP threads share the deques."""
    arb = MasterArbitrator(own_echo_ttl=0.25)
    errors: list[BaseException] = []
    stop = threading.Event()

    def serial():
        i = 0
        try:
            while not stop.is_set():
                frame = _fc06(1, 60 + (i % 40), i % 200)
                arb.note_own_frame(frame)
                arb.observe_frame(_fc06(1, 100 + (i % 40), i % 200))
                arb.observe_frame(frame)
                i += 1
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def other():
        try:
            while not stop.is_set():
                arb.evaluate(bus_healthy=True)
                arb.snapshot()
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=serial), threading.Thread(target=other),
               threading.Thread(target=other)]
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for t in threads:
            t.start()
        time.sleep(1.0)
        stop.set()
        for t in threads:
            t.join(5)
    finally:
        sys.setswitchinterval(old)
    assert not errors, errors[:1]


def _controller_gateway_module():
    import pytest

    return pytest.importorskip("dantherm_controller_gateway")


def test_last_will_runs_in_the_controller_entry_point():
    """safety-1: control_enabled=False must not skip the afterheat/fireplace restore."""
    import types

    module = _controller_gateway_module()
    gateway = module.Gateway.__new__(module.Gateway)
    gateway.control_enabled = False
    gateway.restore_pair = (40, 40)
    gateway.fireplace_gateway_active = True
    calls = []
    gateway.write_afterheat_setpoint = lambda ser, value: calls.append(("afterheat", value))
    gateway.stop_fireplace = lambda ser, clear_saved_state=True: calls.append(("fireplace", clear_saved_state))
    gateway.write_pair = lambda ser, *pair: calls.append(("pair", pair))
    engine = types.SimpleNamespace(
        afterheat_native_setpoint=lambda lock_timeout=None: 22, last_applied={"afterheat_setpoint": 27}
    )
    gateway.controller = types.SimpleNamespace(
        engine=engine, master=types.SimpleNamespace(writes_allowed=lambda: True)
    )
    gateway.restore_on_exit(None)
    # Legacy fan restore stays disabled in the controller entry point.
    assert calls == [("afterheat", 22), ("fireplace", False)]


def test_journal_lines_carry_the_syslog_priority():
    """safety-7: warnings/errors must reach journald with their own priority."""
    import logging

    from journal_logging import JournalPriorityFormatter, stderr_is_journal

    formatter = JournalPriorityFormatter("%(levelname)s %(message)s")

    def line(level, msg):
        return formatter.format(logging.LogRecord("x", level, __file__, 1, msg, None, None))

    assert line(logging.WARNING, "w") == "<4>WARNING w"
    assert line(logging.ERROR, "a\nb") == "<3>ERROR a\n<3>b"
    assert line(logging.INFO, "i").startswith("<6>")
    assert line(logging.DEBUG, "d").startswith("<7>")
    assert stderr_is_journal() in (True, False)


def test_frost_release_ignores_a_wall_clock_step(tmp_path, monkeypatch):
    """safety-9: an NTP step forward must not shorten the 30 min thaw wait."""
    import controller_core
    from controller_core import ControllerEngine, ControllerState, HardwareAdapter

    state = ControllerState(tmp_path / "controller.json")
    state.configure({"afterheat_coil": "water"})
    hardware = HardwareAdapter(**{name: (lambda *_a: None) for name in
                                  ("set_fireplace", "set_standby", "write_fan_pair", "set_afterheat_setpoint")})
    engine = ControllerEngine(state, hardware)
    engine.frost_engage_seconds = 0
    clock = {"wall": 1_800_000_000.0, "mono": 1000.0}
    monkeypatch.setattr(controller_core.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(controller_core.time, "monotonic", lambda: clock["mono"])
    engine.set_external({"outdoor_temperature": -2.0})
    assert engine.resolve()["afterheat_frost_lock"] is True
    engine.set_external({"outdoor_temperature": 12.0, "afterheat_frost_temperature": 25.0})
    engine.resolve()  # the thaw wait starts
    clock["wall"] += 2 * 3600  # NTP step; almost no real time passed
    clock["mono"] += 5
    assert engine.resolve()["afterheat_frost_lock"] is True
    clock["mono"] += controller_core.FROST_LOCK_RELEASE_SECONDS + 1
    clock["wall"] += controller_core.FROST_LOCK_RELEASE_SECONDS + 1
    assert engine.resolve()["afterheat_frost_lock"] is False


def _bare_gateway():
    import queue

    import pytest

    module = pytest.importorskip("dantherm_gateway")
    gateway = module.Gateway.__new__(module.Gateway)
    gateway.controller_hardware_queue = queue.SimpleQueue()
    return module, gateway


def test_expired_hardware_request_is_not_executed_later():
    """safety-8: a request the controller gave up on must not run later."""
    import threading
    import time as _time

    module, gateway = _bare_gateway()
    done = threading.Event()
    result = {"deadline": _time.monotonic() - 0.1}
    gateway.controller_hardware_queue.put(("afterheat_setpoint", 25, done, result))
    written = []
    gateway.write_afterheat_setpoint = lambda ser, value: written.append(value)
    gateway.process_controller_hardware(None)
    assert written == [] and result["error"] == "expired" and done.is_set()


def test_main_loop_stall_is_detected():
    """safety-4: nothing watched the serial main loop."""
    module, gateway = _bare_gateway()
    assert gateway.main_loop_stalled() is None
    gateway.last_main_loop_monotonic = 100.0
    assert gateway.main_loop_stalled(now=100.0 + module.MAIN_LOOP_STALL_SECONDS - 1) is None
    assert gateway.main_loop_stalled(now=100.0 + module.MAIN_LOOP_STALL_SECONDS + 1) is not None


def test_sniffer_duplicate_memory_is_bounded():
    """safety-10: every distinct frame used to stay in last_seen forever."""
    module, _gateway = _bare_gateway()
    sniffer = module.RtuSniffer("/dev/null", 19200, "E", 0)
    for value in range(2000):
        list(sniffer.feed(_fc06(1, 66, value % 65536)))
    assert len(sniffer.last_seen) <= module.SNIFFER_LAST_SEEN_PRUNE_AT + 1


def test_rs485_counters_per_slave():
    """safety-11: timeouts, CRC errors and retries per slave."""
    module, gateway = _bare_gateway()
    gateway.state = {}
    gateway.wait_quiet = lambda ser, seconds=0.015: True
    gateway.serial_write = lambda ser, data, reason: len(data)
    body = bytes([0x40, 3, 2, 0, 5])
    broken = body + b"\x00\x00"
    answers = iter([broken, b"", b"", b""] + [b""] * 400)
    gateway.serial_read = lambda ser, size: next(answers, b"")

    class Ser:
        def flush(self):
            pass

    assert gateway.read_register_block(Ser(), 0x40, 10, 1) is None
    assert gateway.state["rs485_hac1_crc_error"] == 1
    assert gateway.state["rs485_hac1_timeout"] == 3
    assert gateway.state["rs485_hac1_retry"] == 3


def test_drifted_fan_pair_and_bypass_are_written_again():
    """safety-5: re-assert after 30 s of drift, but never fight the unit's frost protection."""
    import types

    from controller_runtime import ControllerRuntime

    runtime = ControllerRuntime.__new__(ControllerRuntime)
    runtime.engine = types.SimpleNamespace(last_applied={"fan_pair": (40, 40), "bypass": "off"})
    runtime.gateway_state = {"fan_extract_percent": 30, "fan_supply_percent": 30, "bypass_request": "ON"}
    runtime._reassert_drifted_outputs(now=0.0)
    runtime._reassert_drifted_outputs(now=29.0)
    assert runtime.engine.last_applied == {"fan_pair": (40, 40), "bypass": "off"}
    runtime._reassert_drifted_outputs(now=31.0)
    assert runtime.engine.last_applied == {}
    # Frost pattern (extract kept, supply lowered) is the unit's own protection.
    runtime.engine.last_applied = {"fan_pair": (40, 40)}
    runtime.gateway_state = {"fan_extract_percent": 40, "fan_supply_percent": 20}
    runtime._reassert_drifted_outputs(now=100.0)
    runtime._reassert_drifted_outputs(now=200.0)
    assert runtime.engine.last_applied == {"fan_pair": (40, 40)}
