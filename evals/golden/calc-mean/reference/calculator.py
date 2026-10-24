"""A tiny calculator used as the agent's practice repository."""

from collections.abc import Sequence


def add(a: float, b: float) -> float:
    return a + b


def subtract(a: float, b: float) -> float:
    return a - b


def multiply(a: float, b: float) -> float:
    return a * b


def mean(numbers: Sequence[float]) -> float:
    if not numbers:
        raise ValueError("mean of an empty list")
    return sum(numbers) / len(numbers)
