import pytest

from calculator import divide


def test_divides() -> None:
    assert divide(10, 4) == 2.5
    assert divide(-9, 3) == -3


def test_zero_divisor_raises_value_error() -> None:
    with pytest.raises(ValueError):
        divide(1, 0)
