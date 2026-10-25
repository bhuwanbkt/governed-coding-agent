import pytest

from shop.inventory import Inventory
from shop.orders import place_order


def test_cannot_remove_more_than_in_stock() -> None:
    inventory = Inventory()
    inventory.add("TEA", 2)
    with pytest.raises(ValueError):
        inventory.remove("TEA", 3)
    assert inventory.quantity("TEA") == 2


def test_unknown_sku_cannot_be_removed() -> None:
    with pytest.raises(ValueError):
        Inventory().remove("COFFEE", 1)


def test_failed_order_takes_no_stock() -> None:
    inventory = Inventory()
    inventory.add("TEA", 5)
    inventory.add("BREAD", 1)
    with pytest.raises(ValueError):
        place_order(inventory, {"TEA": 2, "BREAD": 4})
    assert inventory.quantity("TEA") == 5
    assert inventory.quantity("BREAD") == 1


def test_exact_stock_can_be_removed() -> None:
    inventory = Inventory()
    inventory.add("APPLE", 3)
    inventory.remove("APPLE", 3)
    assert inventory.quantity("APPLE") == 0
