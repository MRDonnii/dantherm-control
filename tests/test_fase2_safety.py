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


def test_bad_typed_settings_become_controller_errors(tmp_path):
    """runtime-12: wrong types must give a 400 (ControllerError), not a dropped connection."""
    import pytest

    from controller_core import ControllerError, ControllerState

    state = ControllerState(tmp_path / "controller.json")
    before = dict(state.data)
    for patch in ({"rh_setpoint": None}, {"co2_setpoint": "x"}, {"co2_setpoint": float("inf")},
                  {"downshift_delay_seconds": []}, {"manual_level": None}):
        with pytest.raises(ControllerError):
            state.configure(patch)
    assert state.data == before


class _FakeRuntime:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"enabled": True, "active_master": "unknown"}

    def configure(self, patch, source="api"):
        self.calls.append(patch)
        return {"enabled": True, **patch}


def _server(tmp_path, monkeypatch):
    monkeypatch.setenv("DANTHERM_CONTROLLER_TOKEN", "test-machine-token")
    from controller_dashboard_server import ControllerDashboardHttpServer

    runtime = _FakeRuntime()
    server = ControllerDashboardHttpServer(
        "127.0.0.1", 0, {}, "Test", None, web_root=ROOT / "gateway/webui",
        history_path=tmp_path / "history.sqlite3", controller_runtime=runtime,
    )
    server.auth.path = tmp_path / "auth.json"
    server.start()
    return server, runtime, server.server.server_address[1]


def _post(port, path, payload, headers=None):
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        return error.code


def test_control_key_cannot_change_safety_settings(tmp_path, monkeypatch):
    """runtime-3: the HA (control) key may not switch off the frost lock."""
    server, runtime, port = _server(tmp_path, monkeypatch)
    try:
        bearer = {"Authorization": "Bearer test-machine-token"}
        assert _post(port, "/api/controller/command", {"frost_lock_enabled": False}, bearer) == 403
        assert _post(port, "/api/controller/command", {"mode": "smart_auto"}, bearer) == 200
        assert runtime.calls == [{"mode": "smart_auto"}]
    finally:
        server.stop()


def test_login_off_still_needs_a_csrf_token(tmp_path):
    """runtime-4: with login off, a POST without the token used to pass."""
    from webui_auth import AuthManager

    auth = AuthManager(tmp_path / "auth.json")
    auth.save("admin", "long-test-password", False)
    session = auth.authenticate_cookie("")
    assert session["csrf"] and session["csrf"] == auth.open_csrf


def test_unreadable_auth_file_does_not_reopen_setup(tmp_path):
    """runtime-5: a damaged login file must fail closed, and the last good copy is used."""
    from webui_auth import AuthManager

    path = tmp_path / "auth.json"
    auth = AuthManager(path)
    auth.save("admin", "long-test-password", True)
    auth.save("admin", "other-long-password", True)  # second write leaves a .bak
    path.write_text("{broken")
    assert auth.configured() and auth.enabled()
    assert auth.verify("admin", "long-test-password")  # from the backup
    auth.backup_path.write_text("{broken")
    assert auth.unreadable() and auth.configured() and auth.enabled()
    assert auth.authenticate_cookie("") is None


def test_login_attempt_is_reserved_before_hashing(tmp_path):
    """runtime-13: parallel logins could bypass the 5 per 5 minutes limit."""
    from webui_auth import AuthManager

    auth = AuthManager(tmp_path / "auth.json")
    tokens = [auth.take_attempt("1.2.3.4") for _ in range(6)]
    assert all(tokens[:5]) and tokens[5] is None
    auth.succeeded("1.2.3.4", tokens[0])
    assert auth.take_attempt("1.2.3.4") is not None


def test_unknown_cookie_does_not_write_the_session_file(tmp_path, monkeypatch):
    """runtime-14: every request with a stale cookie rewrote webui-sessions.json."""
    from webui_auth import AuthManager

    auth = AuthManager(tmp_path / "auth.json")
    auth.save("admin", "long-test-password", True)
    writes = []
    monkeypatch.setattr(auth, "_save_sessions", lambda: writes.append(1))
    assert auth.authenticate_cookie("dantherm_session=unknown") is None
    assert writes == []


def test_negative_content_length_does_not_hang(tmp_path, monkeypatch):
    """runtime-13: Content-Length -1 made the handler block in rfile.read(-1)."""
    import socket

    server, _runtime, port = _server(tmp_path, monkeypatch)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
            conn.sendall(b"POST /api/auth/login HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n"
                         b"Content-Type: application/json\r\n\r\n")
            answer = conn.recv(200)
        assert answer.startswith(b"HTTP/1.")
    finally:
        server.stop()


def test_captures_stop_by_themselves_and_old_ones_are_pruned(tmp_path):
    """runtime-10: captures had no size/time limit and were never cleaned up."""
    import time as _time

    import sniffer

    for index in range(7):
        (tmp_path / f"2026010{index}T000000Z").mkdir()
    manager = sniffer.SnifferManager(base_dir=str(tmp_path))
    manager._ensure_capture_dir()
    try:
        assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == sniffer.CAPTURE_KEEP
        assert manager.limit_reached() is None
        manager._started_monotonic = _time.monotonic() - sniffer.CAPTURE_MAX_SECONDS - 1
        assert manager.limit_reached() == "time"
    finally:
        manager._close_files()
    # A second start after a stop opens new files (it used to write to closed handles).
    manager._ensure_capture_dir()
    assert manager._raw_fh is not None
    manager._close_files()


def test_six_step_top_level_can_end_by_itself(tmp_path):
    """runtime-11: an opt-in (off by default) time limit for the six-step top level."""
    import time as _time

    from controller_core import ControllerState

    state = ControllerState(tmp_path / "controller.json")
    state.configure({"fan_step_count": 6})
    state.configure({"mode": "manual", "manual_level": 6})
    assert state.data["top_level_limit_enabled"] is False
    assert state.data["max_level_until"] is None
    state.configure({"top_level_limit_enabled": True})
    assert state.data["max_level_until"] is not None
    state._expire_max_level(_time.time() + 5 * 3600)
    assert state.data["manual_level"] == 5 and state.data["max_level_until"] is None


def _admin_module(monkeypatch):
    import importlib.util
    import os

    monkeypatch.setenv("DANTHERM_REBOOT_TOKEN", "test-token")
    spec = importlib.util.spec_from_file_location("fase2_admin_test", ROOT / "gateway/dantherm_pi_admin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hanging_update_gets_sigterm_so_it_can_roll_back(tmp_path, monkeypatch):
    """runtime-6: subprocess.run(timeout) sent SIGKILL and skipped the rollback."""
    import pytest

    admin = _admin_module(monkeypatch)
    monkeypatch.setattr(admin, "UPDATE_TIMEOUT_SECONDS", 1)
    marker = tmp_path / "rolled-back"
    script = tmp_path / "update.sh"
    script.write_text(f"trap 'echo done > {marker}; exit 143' TERM\nwhile true; do sleep 0.1; done\n")
    with open(tmp_path / "log", "w") as log, pytest.raises(RuntimeError):
        admin._run_updater(["bash", str(script)], cwd=tmp_path, env={}, log=log)
    assert marker.read_text().strip() == "done"


def test_update_claim_and_frost_deferral(tmp_path, monkeypatch):
    """runtime-6 (double start) and gap-11 (no update while it freezes unless chosen)."""
    import json

    admin = _admin_module(monkeypatch)
    admin.UPDATE_STATE["running"] = False
    assert admin.claim_update() and not admin.claim_update()
    admin.release_update_claim()
    state = tmp_path / "controller.json"
    monkeypatch.setattr(admin, "CONTROLLER_STATE_FILE", state)
    assert admin.frost_lock_latched() is False
    state.write_text(json.dumps({"frost_lock_latched": True}))
    assert admin.frost_lock_latched() is True
    script = (ROOT / "update.sh").read_text()
    assert "HCH5_UPDATE_FORCE" in script and "exit 3" in script and "trap on_term TERM" in script
    assert 'cp -a "${statefile}" "${backup}/state/runtime/"' in script  # runtime-7
    assert "Soak: OK" in script and "hch5-control-update-*" in script  # gap-11, runtime-8


def test_download_is_checked_against_the_release_checksums(tmp_path, monkeypatch):
    """runtime-9: every file of the downloaded tree must match SHA256SUMS."""
    import hashlib

    import pytest

    admin = _admin_module(monkeypatch)
    (tmp_path / "gateway").mkdir()
    (tmp_path / "update.sh").write_text("echo ok\n")
    (tmp_path / "gateway" / "a.py").write_text("x = 1\n")
    sums = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in ("update.sh", "gateway/a.py")}
    assert admin.verify_source_tree(tmp_path, sums) == 2
    (tmp_path / "gateway" / "a.py").write_text("x = 2\n")
    with pytest.raises(RuntimeError):
        admin.verify_source_tree(tmp_path, sums)


def test_update_sh_exit_3_is_shown_as_deferred_for_frost(tmp_path, monkeypatch):
    """gap-11: update.sh's frost deferral (exit 3) is not a failed update."""
    import io
    import tarfile

    admin = _admin_module(monkeypatch)
    archive = tmp_path / "release.tar.gz"
    data = b"exit 3\n"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("dantherm-control-9.0.0/update.sh")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    for name, value in (("UPDATE_STATE_FILE", tmp_path / "s.json"), ("UPDATE_LOG_FILE", tmp_path / "l.log"),
                        ("UPDATE_CHANNEL_FILE", tmp_path / "c")):
        monkeypatch.setattr(admin, name, value)
    monkeypatch.setattr(admin, "current_version", lambda: "1.0.0")
    monkeypatch.setattr(admin, "_schedule_admin_restart", lambda: None)
    monkeypatch.setattr(admin, "_release_checksums", lambda ref: None)
    monkeypatch.setattr(admin, "update_info", lambda *a, **k: {"ref": "v9.0.0", "available_version": "9.0.0"})
    monkeypatch.setattr(admin, "_download_tarball", lambda ref, dest: dest.write_bytes(archive.read_bytes()))
    admin._install_update("beta")
    assert admin.UPDATE_STATE["phase"] == "deferred" and admin.UPDATE_STATE["running"] is False
