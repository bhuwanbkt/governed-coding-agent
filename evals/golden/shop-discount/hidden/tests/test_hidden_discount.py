import pytest

from shop.pricing import order_total


def test_percentage_discount() -> None:
    assert order_total({"COFFEE": 2}, "SAVE10") == pytest.approx(14.38)  # 15.98 * 0.9 = 14.382
    assert order_total({"BREAD": 4}, "SAVE25") == pytest.approx(6.75)


def test_small_order_with_code_is_not_negative() -> None:
    assert order_total({"APPLE": 1}, "SAVE10") == pytest.approx(0.45)


def test_totals_are_rounded_to_cents() -> None:
    total = order_total({"TEA": 3})  # 12.299999...
    assert total == 12.3
    assert round(total, 2) == total


def test_unknown_code_still_rejected() -> None:
    with pytest.raises(ValueError):
        order_total({"APPLE": 1}, "FREE")
