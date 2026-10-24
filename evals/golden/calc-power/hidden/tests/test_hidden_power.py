import pytest

from calculator import power


def test_powers() -> None:
    assert power(2, 10) == 1024
    assert power(5, 0) == 1
    assert power(1.5, 2) == 2.25


def test_negative_exponent_raises_value_error() -> None:
    with pytest.raises(ValueError):
        power(2, -1)


def test_non_integer_exponent_raises_type_error() -> None:
    with pytest.raises(TypeError):
        power(2, 0.5)
