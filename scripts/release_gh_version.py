#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Require the GitHub CLI release that verifies provenance safely.

``release.yml`` pipes ``gh --version`` into this program before it verifies
anything, because an older CLI cannot be trusted to enforce the flags the
release policy depends on. ``2.102.0`` fixes
[GHSA-wjmr-j3rp-mh2g](https://github.com/cli/cli/security/advisories/GHSA-wjmr-j3rp-mh2g),
which let an affected version accept a different signer workflow whose path
merely shared the configured prefix, and
[GHSA-4mq3-hpgx-9cx8](https://github.com/cli/cli/security/advisories/GHSA-4mq3-hpgx-9cx8),
which made ``--source-ref`` matching case-insensitive.

The release jobs run it with the runner's preinstalled ``python3`` and the
standard library only, as ``scripts/release_version.py`` does and for the same
reason.

Examples
--------
>>> parse_version("gh version 2.102.0 (2026-01-15)")
(2, 102, 0)
>>> parse_version("release notes unavailable")
"""

from __future__ import annotations

import re
import sys
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The oldest GitHub CLI whose attestation verification enforces the release
#: policy's identity flags as documented.
MINIMUM_VERSION = (2, 102, 0)
_VERSION = re.compile(r"\bgh version (\d+)\.(\d+)\.(\d+)\b")


def parse_version(line: str) -> tuple[int, int, int] | None:
    """Return the release number named in a ``gh --version`` line.

    Parameters
    ----------
    line : str
        The first line ``gh --version`` printed.

    Returns
    -------
    tuple[int, int, int] | None
        The major, minor, and patch numbers, or ``None`` when the line names
        no recognizable release.

    Examples
    --------
    >>> parse_version("gh version 2.97.0 (2026-07-31)")
    (2, 97, 0)
    >>> parse_version("gh version 3.0.0-beta1 (2027-01-01)")
    (3, 0, 0)
    """
    match = _VERSION.search(line)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _spell(version: tuple[int, int, int]) -> str:
    """Render a release number the way ``gh --version`` does."""
    return ".".join(str(part) for part in version)


def main(lines: cabc.Iterable[str]) -> int:
    """Require the first ``gh --version`` line to name the minimum release.

    Parameters
    ----------
    lines : collections.abc.Iterable[str]
        The lines ``gh --version`` printed, in order.

    Returns
    -------
    int
        ``0`` when the release is new enough, ``1`` otherwise.
    """
    first = next(iter(lines), "")
    found = parse_version(first)
    if found is not None and found >= MINIMUM_VERSION:
        return 0
    shown = "unknown" if found is None else _spell(found)
    print(
        f"::error title=gh-version::GitHub CLI {_spell(MINIMUM_VERSION)} or newer "
        f"is required; found {shown}.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.stdin))
