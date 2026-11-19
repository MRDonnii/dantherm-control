import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

import pytest

dantherm_gateway = pytest.importorskip("dantherm_gateway")
decode_temperature = dantherm_gateway.decode_temperature


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0.0),
        (2, 0.02),
        (0xFFFF, -0.01),   # was 655.35 °C read unsigned
        (0xFFFA, -0.06),
        (0xFC18, -10.0),
        (0xF254, -35.0),
        (2190, 21.9),
    ],
)
def test_signed_hundredths(raw, expected):
    assert decode_temperature(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [0x7FFF, 0x8000, 0xF253, 10001])
def test_sentinels_and_out_of_range_are_invalid(raw):
    assert decode_temperature(raw) is None


def test_fc04_paths_never_decode_unsigned():
    source = (ROOT / "gateway/dantherm_gateway.py").read_text()
    assert "value = raw / 100.0" not in source
    assert '"supply_temperature", values[1] / 100.0' not in source


def test_afterheat_block_encodes_negative_temperatures_as_words():
    source = (ROOT / "gateway/dantherm_gateway.py").read_text()
    assert 'words.append(round(float(self.state[key]) * 100) & 0xFFFF)' in source
    assert decode_temperature(round(-0.35 * 100) & 0xFFFF) == pytest.approx(-0.35)


def test_passive_windows_let_outdoor_and_exhaust_go_below_zero():
    t = dantherm_gateway.temperature_in
    windows = dantherm_gateway.PASSIVE_180_WINDOWS
    assert t(0xFE0C, *windows["outdoor_temp"]) == pytest.approx(-5.0)
    assert t(0xFF9C, *windows["exhaust_temp"]) == pytest.approx(-1.0)
    assert t(300, *windows["extract_temp"]) is None      # 3 °C indoor air is a misread frame
    assert t(0xFF9C, *dantherm_gateway.FROST_SENSOR_WINDOW) == pytest.approx(-1.0)
    assert t(0x8000, *dantherm_gateway.T2AH_WINDOW) is None
    source = (ROOT / "gateway/dantherm_gateway.py").read_text()
    assert "raw / 100.0" not in source and "] / 100.0" not in source


class _FakeGateway:
    def __init__(self, written):
        self.state = {}
        self.written_fan_pair = written

    def publish(self, key, value, **_kwargs):
        self.state[key] = value


def test_unit_frost_protection_is_reported_not_an_error():
    update = dantherm_gateway.Gateway.update_unit_frost_protection
    gw = _FakeGateway((70, 35))
    assert update(gw, (70, 16)) is True
    assert gw.state["unit_frost_protection"] is True and gw.state["unit_frost_supply_percent"] == 16
    assert update(gw, (70, 35)) is False
    assert gw.state["unit_frost_protection"] is False and gw.state["unit_frost_supply_percent"] is None
    # Anything else than a lowered supply fan is still a real mismatch.
    assert update(gw, (40, 35)) is False
    assert update(gw, (70, 50)) is False
    assert update(gw, None) is False
