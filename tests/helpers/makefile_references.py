"""Trace assigned Makefile variables that depend on a named variable."""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_documents import require
from tests.helpers.makefile import _variable_records
from tests.helpers.makeutil import DEFAULT_RUNNER, MAKEFILE, Runner, makeutil_document

if typ.TYPE_CHECKING:
    from pathlib import Path


def variables_referencing(
    name: str,
    *,
    makefile: str = MAKEFILE,
    root: Path | None = None,
    runner: Runner = DEFAULT_RUNNER,
) -> frozenset[str]:
    """Return assigned variables whose values transitively reference ``name``.

    Function names and automatic variables are ignored unless the Makefile
    assigns them. This lets contracts follow a selector through derived lists
    without interpreting Make functions such as ``filter`` or ``wildcard``.

    Parameters
    ----------
    name : str
        The assigned variable whose consumers should be found.
    makefile : str
        Path to the Makefile, relative to the working directory.
    root : pathlib.Path, optional
        The directory to parse in, as :func:`makeutil_document` takes it.
    runner : Runner, optional
        The process boundary, as :func:`makeutil_document` takes it.

    Returns
    -------
    frozenset of str
        ``name`` and every assigned variable that transitively references it.

    Raises
    ------
    AssertionError
        If ``name`` is not assigned or if parsing the Makefile fails.
    """  # ruff: ignore[docstring-extraneous-exception] - require() reports contract errors.
    records = _variable_records(
        makeutil_document(makefile=makefile, root=root, runner=runner)
    )
    require(
        condition=name in records,
        message=f"the Makefile must assign {name}",
    )
    references = re.compile(r"\$\(([A-Za-z0-9_.\-/]+)\)")
    reachable = {name}
    while True:
        dependants = {
            candidate
            for candidate, value in records.items()
            if candidate not in reachable
            and any(reference in reachable for reference in references.findall(value))
        }
        if not dependants:
            break
        reachable.update(dependants)
    return frozenset(reachable)
