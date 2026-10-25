"""Placing orders against the inventory."""

from __future__ import annotations

from shop.inventory import Inventory
from shop.pricing import order_total


def place_order(inventory: Inventory, lines: dict[str, int], code: str | None = None) -> float:
    """Take the ordered items out of stock and return the amount to charge.
    Nothing is taken unless every line can be fulfilled."""
    for sku, quantity in lines.items():
        if inventory.quantity(sku) < quantity:
            raise ValueError(f"not enough {sku} in stock")
    total = order_total(lines, code)
    for sku, quantity in lines.items():
        inventory.remove(sku, quantity)
    return total
