"""A tiny calculator used as the agent's practice repository."""


def add(a: float, b: float) -> float:
    return a + b


def subtract(a: float, b: float) -> float:
    return a - b


def multiply(a: float, b: float) -> float:
    return a * b


def power(base: float, exponent: int) -> float:
    if isinstance(exponent, bool) or not isinstance(exponent, int):
        raise TypeError("exponent must be an integer")
    if exponent < 0:
        raise ValueError("exponent must not be negative")
    return base**exponent
