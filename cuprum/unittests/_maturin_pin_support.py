"""The pyproject maturin pin reader, shared by two test modules.

The other pin readers stay inlined in `test_maturin_pins.py`, their only
consumer. This one is externalized because it genuinely has two: the pin
synchronization checks compare it across CI files, and the wheel snapshot test
asserts the built wheel's ``Generator`` matches it. That is the second concrete
consumer the helper module's re-use policy asks for before sharing anything.
"""

from __future__ import annotations

import re
import typing as typ

if typ.TYPE_CHECKING:
    import pathlib as pth

MATURIN_PIN_RE = re.compile(r"maturin==(\d+\.\d+\.\d+)")


def require_pin_match(
    match: re.Match[str] | None,
    location: str,
    *,
    subject: str = "maturin version pin",
) -> str:
    """Return the captured version from a pin match or raise with the location."""
    if match is None:
        msg = f"Could not locate {subject} in {location}"
        raise AssertionError(msg)
    return match.group(1)


def read_text(root: pth.Path, relative: str) -> str:
    """Read a repository-relative UTF-8 text file.

    Deliberately uncached. A full run of the pin tests reads seven files —
    ``pyproject.toml`` and ``build-wheels.yml`` three times each, ``action.yml``
    once — totalling roughly 93 microseconds against a suite runtime of about
    0.8 seconds, so a cache could save at most four redundant reads and about
    0.006% of the time.

    Against that, a cache would have to stay correct: these tests assert on
    repository files, and a memoized read would serve stale content to any
    future test that writes one. Paying a correctness hazard in a test helper
    for a saving three orders of magnitude below the noise is the wrong trade.
    """
    return (root / relative).read_text(encoding="utf-8")


def read_expected_maturin_version(root: pth.Path) -> str:
    """Read the maturin version pinned as the dev dependency in pyproject.toml."""
    return require_pin_match(
        MATURIN_PIN_RE.search(read_text(root, "pyproject.toml")),
        "pyproject.toml",
    )
