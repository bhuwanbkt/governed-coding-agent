import pytest

from calculator import mean


def test_mean() -> None:
    assert mean([2, 4]) == 3


def test_mean_empty() -> None:
    with pytest.raises(ValueError):
        mean([])
