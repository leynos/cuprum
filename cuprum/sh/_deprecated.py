"""The deprecated ``IOOptions`` alias for ``RunOutputOptions``.

:class:`IOOptions` is older than the options dataclass it subclasses and is
retained for callers that have not migrated: it resolves every field exactly
as its base does and then warns.

It lives here rather than beside the dataclass because it describes how to
*reach* the options vocabulary rather than what that vocabulary means. The
options module sits against the repository's module-length ceiling, and
spending its remaining lines on a spelling that is scheduled to disappear
would leave no room for the live contract beside it.

The deprecated flat ``capture=``/``echo=`` keywords of ``Pipeline.run`` and
``Pipeline.run_sync`` are not here: :mod:`cuprum.sh.pipeline_output` owns them,
and ``cuprum.sh.pipeline`` re-exports them from there.
"""

from __future__ import annotations

import warnings

from cuprum.sh.output import RunOutputOptions

__all__ = [
    "IOOptions",
]


class IOOptions(RunOutputOptions):
    """Deprecated alias for command output stream options."""

    def __post_init__(self) -> None:
        """Resolve the inherited options, then warn about the deprecated alias."""
        # Zero-arg ``super()`` breaks under ``slots=True``: the decorator
        # rebuilds the class, so the method's ``__class__`` cell refers to the
        # pre-rebuild class and the instance fails the ``super`` type check.
        RunOutputOptions.__post_init__(self)
        warnings.warn(
            "IOOptions is deprecated; use RunOutputOptions instead",
            DeprecationWarning,
            stacklevel=2,
        )
