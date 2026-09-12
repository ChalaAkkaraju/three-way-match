"""A test harness small enough to read in one sitting.

No third-party test runner, for the same reason the rest of the project has
no dependencies: there is nothing here a hundred lines cannot do, and the
whole point of the project is that its guarantees are inspectable.
"""

from __future__ import annotations

import sys
import traceback
from typing import List, Tuple

FAILURES: List[Tuple[str, str]] = []
PASSES = 0


def check(name: str):
    def deco(fn):
        global PASSES
        try:
            fn()
            PASSES += 1
            print(f"  ok    {name}")
        except AssertionError as e:
            FAILURES.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception:
            FAILURES.append((name, traceback.format_exc()))
            print(f"  ERROR {name}")
            traceback.print_exc()
        return fn
    return deco


def section(title: str) -> None:
    print(f"\n{title}")


def summary(label: str = "") -> int:
    print()
    print("=" * 60)
    if FAILURES:
        print(f"{PASSES} passed, {len(FAILURES)} FAILED{(' — ' + label) if label else ''}")
        for name, err in FAILURES:
            first = err.splitlines()[0] if err else ""
            print(f"  - {name}: {first}")
        return 1
    print(f"{PASSES} passed{(' — ' + label) if label else ''}")
    return 0


def run_all() -> int:
    """Import every test module, then report once.

    Called from tests/__main__.py rather than from here: running this file as
    __main__ would load a second copy of it under its real name, and the
    counters the test modules increment would not be the ones printed.
    """
    from tests import test_matching, test_rag        # noqa: F401
    return summary("all")
