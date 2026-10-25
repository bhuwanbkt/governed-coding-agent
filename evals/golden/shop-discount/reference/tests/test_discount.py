import pytest

from shop.pricing import order_total


def test_save10_is_ten_percent() -> None:
    assert order_total({"BREAD": 4}, "SAVE10") == pytest.approx(8.1)
