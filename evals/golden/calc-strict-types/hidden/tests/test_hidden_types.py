import pytest

from calculator import add, multiply, subtract


@pytest.mark.parametrize("fn", [add, subtract, multiply])
@pytest.mark.parametrize("bad", ["1", None, True, [1]])
def test_rejects_non_numbers(fn, bad) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(TypeError):
        fn(bad, 1)
    with pytest.raises(TypeError):
        fn(1, bad)


def test_numbers_still_work() -> None:
    assert add(2, 3) == 5
    assert subtract(5, 3) == 2
    assert multiply(4, 2.5) == 10
