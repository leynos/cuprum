#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Check ``pyproject.toml``'s version against the release tag.

The shared pyproject.toml validator exists only inside ``release-to-pypi-uv``,
which also builds and publishes, so ``release.yml``'s ``check-version`` job
runs this instead: the declared string must equal the tag-derived version
exactly, and the step reports whether it is a pre-release. It uses the
runner's preinstalled ``python3`` and the standard library only, as
``scripts/release_assets.py`` does and for the same reason.

Examples
--------
>>> is_prerelease("0.2.0-beta1"), is_prerelease("1.2.3+rc")
(True, False)
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: Every PEP 440 label, longest spelling first, so a left-to-right scan splits
#: a run of letters as PEP 440 does: ``1adev3`` reads as ``a`` then ``dev``,
#: and ``preview`` never as ``pre``. SemVer ``0.2.0-beta.1`` uses the same
#: labels.
_LABEL = re.compile(r"preview|alpha|beta|post|pre|rev|dev|rc|a|b|c|r")
#: The labels that make a pre-release or development release; the others
#: (``post``, ``rev``, ``r``) spell a post-release.
_PRERELEASE_LABELS = frozenset({
    "preview",
    "alpha",
    "beta",
    "pre",
    "dev",
    "rc",
    "a",
    "b",
    "c",
})


def is_prerelease(version: str) -> bool:
    """Return whether ``version`` names a pre-release or development release.

    Parameters
    ----------
    version : str
        A release version in SemVer or PEP 440 spelling.

    Returns
    -------
    bool
        ``True`` for a marker in the public version; a ``+local`` label never
        makes a pre-release.

    Examples
    --------
    >>> is_prerelease("1.2.3"), is_prerelease("1.2.3rc1")
    (False, True)
    """
    public = version.lower().partition("+")[0]
    return any(label in _PRERELEASE_LABELS for label in _LABEL.findall(public))


def main(environ: cabc.Mapping[str, str], root: Path) -> int:
    """Compare the declared version with the tag's; report pre-release status."""
    expected = environ["TAG_VERSION"]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["version"]
    if declared != expected:
        print(
            "::error file=pyproject.toml,title=Tag/pyproject.toml mismatch::"
            f"Tag version {expected} does not match pyproject.toml version {declared}"
        )
        return 1
    prerelease = str(is_prerelease(declared)).lower()
    with Path(environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"prerelease={prerelease}\n")
    print(f"Release tag {expected} matches pyproject.toml.")
    return 0


if __name__ == "__main__":
    sys.exit(main(os.environ, Path.cwd()))
