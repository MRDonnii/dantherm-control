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
