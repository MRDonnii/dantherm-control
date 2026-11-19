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
