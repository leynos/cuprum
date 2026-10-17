"""Resolving and policing the output options a pipeline run is given.

Split from ``cuprum.sh.pipeline`` when the rebase onto ``main`` pushed that
module past the 400-line ceiling. These three helpers all answer the same
question — *what does this call's output configuration actually mean* — and
none of them needs a `Pipeline`, a `SafeCmd`, or an event loop. Keeping them
apart from the class is what lets `pipeline.py` stay about composing and
driving stages while this module owns the option surface.

The split is also what preserves the `cuprum.sh.pipeline` import path.
`cuprum.sh` re-exports `_DeprecatedOutputFlags` and `_resolve_pipeline_output`
from the pipeline module, and callers import them from there; the pipeline
module re-exports them from here rather than the facade reaching past it.

No `from __future__ import annotations` here: `pipeline.py` is reached from
`cuprum.sh`, whose public signatures are introspected with
`typing.get_type_hints`, and the imports below are all genuine runtime
imports, so there is nothing to postpone.
"""

import typing as typ
import warnings

from cuprum.sh.output import RunOutputOptions

__all__ = [
    "_DeprecatedOutputFlags",
    "_reject_stdio_targets",
    "_resolve_pipeline_output",
]


class _DeprecatedOutputFlags(typ.TypedDict, total=False):
    """Deprecated flat ``capture``/``echo`` flags for ``Pipeline.run``."""

    capture: bool
    echo: bool


def _resolve_pipeline_output(
    output: RunOutputOptions | None,
    flags: _DeprecatedOutputFlags,
) -> RunOutputOptions:
    """Resolve pipeline output options, deprecating flat ``capture``/``echo``.

    Parameters
    ----------
    output : RunOutputOptions | None
        The options given as ``output=``, or ``None``.
    flags : _DeprecatedOutputFlags
        The flat keywords the caller gave, collected by ``**``.

    Returns
    -------
    RunOutputOptions
        The supplied options, or options rebuilt from the deprecated flags, or
        the defaults when neither was given.

    Raises
    ------
    TypeError
        If a keyword outside the deprecated pair reached the call.
    ValueError
        If ``output`` and the deprecated flags were given together.
    """
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


def _reject_stdio_targets(output: RunOutputOptions) -> None:
    """Refuse standard-stream targets a pipeline cannot honour.

    ``RunOutputOptions`` carries ``stdin``/``stdout``/``stderr`` targets
    wherever it is accepted, but a pipeline reads only the capture, echo,
    sink, idle, and line-hook fields: the stage wiring that would carry a
    target to the right stage does not exist. Accepting one would be the
    silent loss ``RunOutputOptions`` refuses everywhere else — the run
    succeeds, exits ``0``, and the file the caller named is never created.

    Refusing is deliberately the whole fix. Making a target *mean* something
    for a pipeline is a design decision about which stage a stream belongs to,
    and that decision belongs with the stage wiring rather than being guessed
    here.

    Parameters
    ----------
    output : RunOutputOptions
        The resolved options a pipeline run was given.

    Raises
    ------
    ValueError
        If any of the three standard streams names a target.
    """
    named = [
        stream
        for stream in ("stdin", "stdout", "stderr")
        if getattr(output, stream) is not None
    ]
    if not named:
        return
    msg = (
        f"Pipeline.run does not support RunOutputOptions "
        f"{', '.join(named)} targets; redirect a single SafeCmd instead"
    )
    raise ValueError(msg)
