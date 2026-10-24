import pytest

from calculator import divide


def test_divide() -> None:
    assert divide(10, 4) == 2.5


def test_divide_by_zero() -> None:
    with pytest.raises(ValueError):
        divide(1, 0)
