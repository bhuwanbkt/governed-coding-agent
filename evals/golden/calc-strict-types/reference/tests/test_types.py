import pytest

from calculator import add


def test_add_rejects_strings() -> None:
    with pytest.raises(TypeError):
        add("1", 2)  # type: ignore[arg-type]
