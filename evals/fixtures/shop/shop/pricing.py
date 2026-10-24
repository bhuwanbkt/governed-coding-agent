"""Prices and discount codes."""

from __future__ import annotations

PRICES: dict[str, float] = {
    "APPLE": 0.5,
    "BREAD": 2.25,
    "COFFEE": 7.99,
    "TEA": 4.1,
}

# Discount codes take a percentage off the order total.
DISCOUNT_CODES: dict[str, int] = {
    "SAVE10": 10,
    "SAVE25": 25,
}


def unit_price(sku: str) -> float:
    try:
        return PRICES[sku]
    except KeyError:
        raise KeyError(f"unknown SKU: {sku}") from None


def order_total(lines: dict[str, int], code: str | None = None) -> float:
    """Total price of an order: {sku: quantity}, with an optional discount code."""
    total = sum(unit_price(sku) * quantity for sku, quantity in lines.items())
    if code is not None:
        if code not in DISCOUNT_CODES:
            raise ValueError(f"unknown discount code: {code}")
        total -= DISCOUNT_CODES[code]
    return total
