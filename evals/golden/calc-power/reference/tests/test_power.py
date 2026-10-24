import pytest

from calculator import power


def test_power() -> None:
    assert power(3, 3) == 27


def test_negative_exponent() -> None:
    with pytest.raises(ValueError):
        power(3, -2)
