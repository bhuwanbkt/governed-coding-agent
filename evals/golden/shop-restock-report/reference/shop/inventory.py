"""Stock levels per SKU."""

from __future__ import annotations


class Inventory:
    """In-memory stock keeping. Quantities are whole units."""

    def __init__(self) -> None:
        self._stock: dict[str, int] = {}

    def add(self, sku: str, quantity: int) -> None:
        """Receive `quantity` units of `sku`."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._stock[sku] = self._stock.get(sku, 0) + quantity

    def remove(self, sku: str, quantity: int) -> None:
        """Take `quantity` units of `sku` out of stock (e.g. when an order ships)."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._stock[sku] = self._stock.get(sku, 0) - quantity

    def quantity(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def skus(self) -> list[str]:
        return sorted(self._stock)

    def low_stock(self, threshold: int) -> list[str]:
        """SKUs with at most `threshold` units, sorted."""
        return sorted(sku for sku, quantity in self._stock.items() if quantity <= threshold)
