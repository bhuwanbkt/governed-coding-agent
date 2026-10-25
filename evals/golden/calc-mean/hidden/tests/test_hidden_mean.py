import pytest

from calculator import mean


def test_mean() -> None:
    assert mean([1, 2, 3, 4]) == 2.5
    assert mean([7]) == 7


def test_mean_accepts_any_sequence_of_numbers() -> None:
    assert mean((2.0, 4.0)) == 3.0


def test_empty_raises_value_error() -> None:
    with pytest.raises(ValueError):
        mean([])
