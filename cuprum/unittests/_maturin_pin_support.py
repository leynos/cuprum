"""The maturin pin readers and patterns shared by more than one test module.

Nothing lives here on the strength of a single caller. The other pin readers
stay inlined in `test_maturin_pins.py`, their only consumer. These are
externalized because each genuinely has two, which is the second concrete
consumer the helper re-use policy asks for before sharing anything:

- `read_expected_maturin_version` (and the `read_text` / `require_pin_match` it
  is built from) — the pin synchronization checks compare it across CI files,
  and the wheel snapshot test asserts the built wheel's ``Generator`` matches
  it.
- `MANYLINUX_CONTAINER_SHA256_RE` — `test_maturin_pins.py` asserts the pinned
  container reference matches it, and
  `test_manylinux_container_ref_properties.py` generates references to probe
  the same pattern. It lives here so neither test module has to import a
  private name from the other.
"""

from __future__ import annotations

import re
import typing as typ

if typ.TYPE_CHECKING:
    import pathlib as pth

MATURIN_PIN_RE = re.compile(r"maturin==(\d+\.\d+\.\d+)")

MANYLINUX_CONTAINER_SHA256_RE = re.compile(
    r"^ghcr\.io/rust-cross/manylinux_2_28-cross@sha256:[0-9a-f]{64}$"
)


def require_pin_match(
    match: re.Match[str] | None,
    location: str,
    *,
    subject: str = "maturin version pin",
) -> str:
    """Return the captured version from a pin match, or fail naming the file.

    Parameters
    ----------
    match : re.Match[str] or None
        The result of searching a file for its pin; ``None`` when absent.
    location : str
        The repository-relative path searched, named in the failure so the
        reader knows which file to look at.
    subject : str, optional
        What was being looked for, by default ``"maturin version pin"``.

    Returns
    -------
    str
        The first capture group, which every pin pattern here defines as the
        pinned value.

    Raises
    ------
    AssertionError
        If `match` is ``None``. This is a test helper, so a missing pin is a
        test failure rather than an exceptional condition.

    Examples
    --------
    >>> require_pin_match(MATURIN_PIN_RE.search("maturin==1.2.3"), "x.toml")
    '1.2.3'
    """
    if match is None:
        msg = f"Could not locate {subject} in {location}"
        raise AssertionError(msg)
    return match.group(1)


def read_text(root: pth.Path, relative: str) -> str:
    """Read a repository-relative UTF-8 text file.

    Parameters
    ----------
    root : pathlib.Path
        The repository root, from `tests.helpers.docs.repo_root`.
    relative : str
        The path to read, relative to `root`.

    Returns
    -------
    str
        The file's decoded contents.

    Raises
    ------
    OSError
        If the file cannot be read — most often `FileNotFoundError` when a
        pinned CI file has been renamed or removed. Deliberately not caught:
        these tests assert that specific repository files carry specific pins,
        so a missing file is a genuine failure and its own traceback names the
        path more precisely than any wrapper would.
    UnicodeDecodeError
        If the file is not valid UTF-8. Every file read here is repository
        source, so this indicates corruption rather than an input to handle.

    Notes
    -----
    Deliberately uncached. A full run of the pin tests reads seven files —
    ``pyproject.toml`` and ``build-wheels.yml`` three times each, ``action.yml``
    once — totalling roughly 93 microseconds against a suite runtime of about
    0.8 seconds, so a cache could save at most four redundant reads and about
    0.006% of the time.

    Against that, a cache would have to stay correct: these tests assert on
    repository files, and a memoized read would serve stale content to any
    future test that writes one. Paying a correctness hazard in a test helper
    for a saving three orders of magnitude below the noise is the wrong trade.

    Examples
    --------
    >>> from tests.helpers.docs import repo_root
    >>> "maturin" in read_text(repo_root(), "pyproject.toml")
    True
    """  # noqa: DOC502 - propagates filesystem decoding and access errors
    return (root / relative).read_text(encoding="utf-8")


def read_expected_maturin_version(root: pth.Path) -> str:
    """Read the maturin version pinned as the dev dependency in pyproject.toml.

    Parameters
    ----------
    root : pathlib.Path
        The repository root, from `tests.helpers.docs.repo_root`.

    Returns
    -------
    str
        The pinned version, as ``"MAJOR.MINOR.PATCH"``.

    Raises
    ------
    AssertionError
        If ``pyproject.toml`` carries no ``maturin==`` pin.
    OSError
        If ``pyproject.toml`` cannot be read; see `read_text`.

    Examples
    --------
    >>> from tests.helpers.docs import repo_root
    >>> read_expected_maturin_version(repo_root()).count(".")
    2
    """  # noqa: DOC502 - propagates from the shared reader and pin matcher
    return require_pin_match(
        MATURIN_PIN_RE.search(read_text(root, "pyproject.toml")),
        "pyproject.toml",
    )
