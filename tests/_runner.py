"""Test discovery for the dependency-free suites.

Each suite is a plain script run top-to-bottom (no pytest — see the CI TEST_CMD).
They used to end with a hand-maintained list of every test function to call, which
meant a test could be written, pass review, and silently never run: that is exactly
what happened to ``test_credential_refresh_backoff``. Discover instead.

Order is by source line so a suite still reads top-to-bottom, and a failure points
at the file the same way it always did.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable


def discover(namespace: dict[str, Any]) -> list[Callable[..., Any]]:
    """Every ``test_*`` callable defined IN this module, in source order.

    Filters on ``__module__`` so a test helper imported from another suite isn't
    run twice, and skips anything marked ``fn.skip = True``.
    """
    module_name = namespace.get("__name__")
    found = [
        fn for name, fn in namespace.items()
        if name.startswith("test_") and callable(fn)
        and getattr(fn, "__module__", None) == module_name
        and not getattr(fn, "skip", False)
    ]
    return sorted(found, key=lambda f: getattr(getattr(f, "__code__", None), "co_firstlineno", 0))


def run(namespace: dict[str, Any], banner: str) -> int:
    """Discover + run a suite's tests. Returns a process exit code."""
    tests = discover(namespace)
    if not tests:
        print("no tests discovered — check that they are named test_*")
        return 1
    for fn in tests:
        if inspect.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
    print(f"\n{banner} ({len(tests)} tests) ✅")
    return 0
