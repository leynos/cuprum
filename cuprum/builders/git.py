"""Git command builders with typed argument helpers."""

from __future__ import annotations

import typing as typ

from cuprum import sh
from cuprum.builders.args import git_ref
from cuprum.catalogue import GIT

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd


def git_status(*, short: bool = False, branch: bool = False) -> SafeCmd:
    """Build a `git status` command.

    Parameters
    ----------
    short : bool
        When True, request short-format output.
    branch : bool
        When True, include branch information.

    Returns
    -------
    SafeCmd
        The assembled `git status` command.
    """
    args: list[str] = ["status"]
    if short:
        args.append("--short")
    if branch:
        args.append("--branch")
    return sh.make(GIT)(*args)


def git_checkout(
    ref: str,
    *,
    create_branch: bool = False,
    detach: bool = False,
    force: bool = False,
) -> SafeCmd:
    """Build a `git checkout` command with validated refs.

    Parameters
    ----------
    ref : str
        Reference to check out.
    create_branch : bool
        When True, create a new branch at the reference.
    detach : bool
        When True, check out in detached HEAD state.
    force : bool
        When True, force the checkout, overwriting local changes.

    Returns
    -------
    SafeCmd
        The assembled `git checkout` command.

    Raises
    ------
    TypeError
        If ``ref`` is not a ``str`` (propagated from ``git_ref``).
    ValueError
        If both ``create_branch`` and ``detach`` are True, or if ``ref`` is
        not a valid git reference (the latter propagated from ``git_ref``).
    """  # noqa: DOC502 - TypeError/ValueError propagate from git_ref
    if create_branch and detach:
        msg = "create_branch and detach cannot both be True"
        raise ValueError(msg)

    args: list[str] = ["checkout"]
    if create_branch:
        args.append("-B" if force else "-b")
    else:
        if detach:
            args.append("--detach")
        if force:
            args.append("--force")

    args.append(str(git_ref(ref)))
    return sh.make(GIT)(*args)


def git_rev_parse(ref: str) -> SafeCmd:
    """Build a `git rev-parse` command with validated refs.

    Parameters
    ----------
    ref : str
        Reference to resolve.

    Returns
    -------
    SafeCmd
        The assembled `git rev-parse` command.

    Raises
    ------
    TypeError
        If ``ref`` is not a string.
    ValueError
        If ``ref`` is not a valid git reference.
    """  # noqa: DOC502 - TypeError/ValueError propagate from git_ref
    args = ["rev-parse", str(git_ref(ref))]
    return sh.make(GIT)(*args)


__all__ = ["git_checkout", "git_rev_parse", "git_status"]
