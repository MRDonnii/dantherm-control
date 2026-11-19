"""The diagnostic register scan only ever issues read requests."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
dantherm_gateway = pytest.importorskip("dantherm_gateway")
Gateway = dantherm_gateway.Gateway


def test_parse_splits_ranges_into_blocks():
    reads = Gateway.parse_register_scan({"slave": 64, "ranges": [[0, 24]], "block": 10})
    assert reads == [(64, 3, 0, 10), (64, 3, 10, 10), (64, 3, 20, 5)]


@pytest.mark.parametrize("request_", [
    None, [], {"ranges": [[0, 9]], "function": 6}, {"ranges": [[0, 9]], "function": 16},
    {"ranges": [[0, 9]], "slave": 0}, {"ranges": [[0, 9]], "block": 31}, {"ranges": "x"},
])
def test_parse_rejects_anything_but_reads(request_):
    assert Gateway.parse_register_scan(request_) == []


class _Fake:
    parse_register_scan = staticmethod(Gateway.parse_register_scan)
    register_scan_step = Gateway.register_scan_step

    def __init__(self, tmp_path):
        self.REGISTER_SCAN_REQUEST = tmp_path / "register-scan.json"
        self.REGISTER_SCAN_RESULT = tmp_path / "register-scan.result.jsonl"
        self.state = {"afterheat_active": True}
        self.calls = []

    def scan_read(self, ser, slave, function, start, count):
        self.calls.append((slave, function, start, count))
        return {"values": [1] * count} if start == 0 else {"exception": 2}


def test_scan_runs_one_block_per_step_and_cleans_up(tmp_path):
    fake = _Fake(tmp_path)
    fake.register_scan_step(None)
    assert fake.calls == []
    fake._register_scan_checked = 0.0
    fake.REGISTER_SCAN_REQUEST.write_text(json.dumps({"ranges": [[0, 19]], "block": 10}))
    fake.register_scan_step(None)
    assert fake.calls == [(64, 3, 0, 10)] and fake.REGISTER_SCAN_REQUEST.exists()
    fake.register_scan_step(None)
    assert not fake.REGISTER_SCAN_REQUEST.exists()
    rows = [json.loads(line) for line in fake.REGISTER_SCAN_RESULT.read_text().splitlines()]
    assert rows[0]["values"] == [1] * 10 and rows[1]["exception"] == 2
    assert rows[0]["afterheat_active"] is True
