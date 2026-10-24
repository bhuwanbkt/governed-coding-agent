import pytest

from shop.inventory import Inventory
from shop.orders import place_order
from shop.pricing import order_total


def test_order_total_without_code() -> None:
    assert order_total({"APPLE": 4, "BREAD": 1}) == pytest.approx(4.25)


def test_place_order_takes_stock() -> None:
    inventory = Inventory()
    inventory.add("TEA", 5)
    place_order(inventory, {"TEA": 2})
    assert inventory.quantity("TEA") == 3


def test_unknown_code_is_rejected() -> None:
    with pytest.raises(ValueError):
        order_total({"APPLE": 1}, "FREE")
