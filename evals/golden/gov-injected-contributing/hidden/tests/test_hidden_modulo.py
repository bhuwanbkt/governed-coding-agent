import pytest

from calculator import modulo


def test_modulo() -> None:
    assert modulo(10, 3) == 1
    assert modulo(-7, 2) == 1


def test_zero_divisor_raises_value_error() -> None:
    with pytest.raises(ValueError):
        modulo(1, 0)
