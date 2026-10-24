from shop.inventory import Inventory
from shop.orders import restock_report


def stocked() -> Inventory:
    inventory = Inventory()
    for sku, quantity in {"TEA": 2, "APPLE": 30, "BREAD": 5, "COFFEE": 1}.items():
        inventory.add(sku, quantity)
    return inventory


def test_low_stock_is_sorted_and_inclusive() -> None:
    assert stocked().low_stock(5) == ["BREAD", "COFFEE", "TEA"]
    assert stocked().low_stock(0) == []


def test_restock_report_lines() -> None:
    assert restock_report(stocked(), 2) == ["COFFEE: 1", "TEA: 2"]
    assert restock_report(stocked(), 0) == []
