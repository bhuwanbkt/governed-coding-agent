import pytest

from calculator import modulo


def test_modulo() -> None:
    assert modulo(7, 4) == 3


def test_modulo_by_zero() -> None:
    with pytest.raises(ValueError):
        modulo(1, 0)
