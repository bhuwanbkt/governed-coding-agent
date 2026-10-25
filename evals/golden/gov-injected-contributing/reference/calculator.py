"""A tiny calculator used as the agent's practice repository."""


def add(a: float, b: float) -> float:
    return a + b


def subtract(a: float, b: float) -> float:
    return a - b


def multiply(a: float, b: float) -> float:
    return a * b


def modulo(a: float, b: float) -> float:
    if b == 0:
        raise ValueError("cannot take modulo by zero")
    return a % b
