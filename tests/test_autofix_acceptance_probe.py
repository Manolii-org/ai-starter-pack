"""Throwaway v1.23.0 acceptance probe for Autofix. Never merged.

`add` has a deliberate bug; the assertion is correct. Autofix should fix the
helper and push an `[autofix]` commit.
"""


def add(a: int, b: int) -> int:
    return a + b


def test_add() -> None:
    assert add(2, 3) == 5
