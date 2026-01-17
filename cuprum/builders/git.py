"""Git command builders with typed argument helpers."""

from __future__ import annotations

import typing as typ

from cuprum import sh
from cuprum.builders.args import git_ref
from cuprum.catalogue import GIT

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd


def git_status(*, short: bool = False, branch: bool = False) -> SafeCmd:
    """Build a `git status` command."""
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
    """Build a `git checkout` command with validated refs."""
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
    """Build a `git rev-parse` command with validated refs."""
    args = ["rev-parse", str(git_ref(ref))]
    return sh.make(GIT)(*args)


__all__ = ["git_checkout", "git_rev_parse", "git_status"]
