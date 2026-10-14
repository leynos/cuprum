"""Deprecated spellings that still resolve to ``RunOutputOptions``.

Two compatibility surfaces live here, both older than the options dataclass
they produce and both retained for callers that have not migrated:

- :class:`IOOptions`, the deprecated alias for the options dataclass, which
  resolves every field exactly as its base does and then warns.
- :class:`_DeprecatedOutputFlags` and :func:`_resolve_pipeline_output`, the
  flat ``capture=``/``echo=`` keyword arguments of ``Pipeline.run`` and
  ``Pipeline.run_sync``, translated into a ``RunOutputOptions``.

They are separated from :mod:`cuprum.sh.output` because they describe how to
reach that module's vocabulary rather than what the vocabulary means. The
options module sits against the repository's module-length ceiling, and
spending its remaining lines on spelling that is scheduled to disappear would
leave no room for the live contract beside it.
"""

from __future__ import annotations

import typing as typ
import warnings

from cuprum.sh.output import RunOutputOptions

__all__ = [
    "IOOptions",
    "_DeprecatedOutputFlags",
    "_resolve_pipeline_output",
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


class _DeprecatedOutputFlags(typ.TypedDict, total=False):
    """Deprecated flat ``capture``/``echo`` flags for ``Pipeline.run``."""

    capture: bool
    echo: bool


def _resolve_pipeline_output(
    output: RunOutputOptions | None,
    flags: _DeprecatedOutputFlags,
) -> RunOutputOptions:
    """Resolve pipeline output options, deprecating flat ``capture``/``echo``."""
    # Callers forward their ``Unpack[_DeprecatedOutputFlags]`` kwargs verbatim,
    # so the parameter keeps the precise ``TypedDict`` surface. Unknown keys
    # can still arrive at runtime (a ``TypedDict`` is open), and are rejected
    # here to preserve the strict keyword surface.
    unknown = set(flags) - {"capture", "echo"}
    if unknown:
        joined = ", ".join(sorted(unknown))
        msg = f"Pipeline.run/run_sync got unexpected keyword arguments: {joined}"
        raise TypeError(msg)
    if not flags:
        return output or RunOutputOptions()
    if output is not None:
        # Reject combining the deprecated flat flags with ``output``: the
        # caller's intent would otherwise be ambiguous.
        msg = "Pass either 'output' or the deprecated 'capture'/'echo' flags, not both"
        raise ValueError(msg)
    warnings.warn(
        "Pipeline.run/run_sync 'capture' and 'echo' keyword arguments are "
        "deprecated; pass output=RunOutputOptions(...) instead",
        DeprecationWarning,
        stacklevel=3,
    )
    return RunOutputOptions(
        capture=flags.get("capture", True),
        echo=flags.get("echo", False),
    )
