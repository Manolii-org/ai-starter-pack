"""Throwaway Autofix model probe. Never merged.

Two helpers in two files each carry a deliberate bug; the assertion is correct.
Autofix should fix both helpers and push one `[autofix]` commit.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from _probe_calc import subtotal  # noqa: E402
from _probe_fmt import money  # noqa: E402


def test_receipt_total() -> None:
    assert money(subtotal([150, 250])) == "$4.00"
