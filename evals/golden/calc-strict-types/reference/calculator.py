"""A tiny calculator used as the agent's practice repository."""


def _check(*values: object) -> None:
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"expected a number, got {type(value).__name__}")


def add(a: float, b: float) -> float:
    _check(a, b)
    return a + b


def subtract(a: float, b: float) -> float:
    _check(a, b)
    return a - b


def multiply(a: float, b: float) -> float:
    _check(a, b)
    return a * b
