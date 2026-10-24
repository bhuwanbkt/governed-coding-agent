import pytest

from shop.inventory import Inventory


def test_add_and_remove() -> None:
    inventory = Inventory()
    inventory.add("APPLE", 10)
    inventory.remove("APPLE", 3)
    assert inventory.quantity("APPLE") == 7


def test_quantities_must_be_positive() -> None:
    inventory = Inventory()
    with pytest.raises(ValueError):
        inventory.add("APPLE", 0)
    with pytest.raises(ValueError):
        inventory.remove("APPLE", -1)


def test_skus_are_sorted() -> None:
    inventory = Inventory()
    inventory.add("TEA", 1)
    inventory.add("BREAD", 1)
    assert inventory.skus() == ["BREAD", "TEA"]
