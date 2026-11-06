"""Read toolchain pins from the Makefile, the single place that states them."""

from __future__ import annotations

import re

from tests.helpers.docs import repo_root


def makefile_default(name: str) -> str:
    """Return the default a Makefile variable is given with ``?=`` or ``=``.

    Parameters
    ----------
    name
        The variable's name, for example ``DEV_FAST_TOOLCHAIN``.

    Returns
    -------
    str
        The assigned value, without surrounding whitespace.

    Raises
    ------
    LookupError
        If the Makefile does not assign the variable.
    """
    text = (repo_root() / "Makefile").read_text(encoding="utf-8")
    pattern = rf"^{re.escape(name)}\s*\??=\s*(\S+)\s*$"
    match = re.search(pattern, text, flags=re.MULTILINE)
    if match is None:
        message = f"the Makefile assigns no {name}"
        raise LookupError(message)
    return match.group(1)


DEV_FAST_TOOLCHAIN = makefile_default("DEV_FAST_TOOLCHAIN")
RUSTFMT_TOOLCHAIN = makefile_default("RUSTFMT_TOOLCHAIN")
