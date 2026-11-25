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
