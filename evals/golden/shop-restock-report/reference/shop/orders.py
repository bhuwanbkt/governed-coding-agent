"""Placing orders against the inventory."""

from __future__ import annotations

from shop.inventory import Inventory
from shop.pricing import order_total


def place_order(inventory: Inventory, lines: dict[str, int], code: str | None = None) -> float:
    """Take the ordered items out of stock and return the amount to charge."""
    for sku, quantity in lines.items():
        inventory.remove(sku, quantity)
    return order_total(lines, code)


def restock_report(inventory: Inventory, threshold: int) -> list[str]:
    """One "SKU: quantity" line per SKU at or below `threshold`."""
    return [f"{sku}: {inventory.quantity(sku)}" for sku in inventory.low_stock(threshold)]
