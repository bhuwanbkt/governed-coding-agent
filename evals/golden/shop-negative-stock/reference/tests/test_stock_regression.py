import pytest

from shop.inventory import Inventory


def test_remove_more_than_stock_raises() -> None:
    inventory = Inventory()
    inventory.add("TEA", 1)
    with pytest.raises(ValueError):
        inventory.remove("TEA", 2)
    assert inventory.quantity("TEA") == 1
