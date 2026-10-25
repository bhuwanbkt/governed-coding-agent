from shop.inventory import Inventory
from shop.orders import restock_report


def test_report() -> None:
    inventory = Inventory()
    inventory.add("TEA", 1)
    inventory.add("APPLE", 9)
    assert restock_report(inventory, 1) == ["TEA: 1"]
