"""Build the caller-visible failure for a streaming stdin source.

Split from ``cuprum._subprocess_stdin_stream`` when the source-failure fix
pushed that module past the 400-line ceiling. The seam is the one the
docstring there already draws: it owns *which* failures are the producer's and
``cuprum._subprocess_stdin_write`` owns *how* a chunk reaches the pipe, while
this module owns the third, previously unnamed concern — how a failure that
has been classified becomes the exception the caller actually catches.

The pair moved together because they are one job. ``_stdin_source_error``
builds the message for a producer or encoder failure, and ``_source_error``
resolves the public type that message is raised as; neither reads the sink,
the process, or the observation, and nothing here calls back into the pull
loop, so the boundary between this module and the streaming writer is a leaf
edge rather than a cycle.

The lazy import below is the one constraint that travels with the code.
``cuprum.sh.execution`` defines :class:`~cuprum.sh.execution.StdinSourceError`,
but ``cuprum.sh`` pulls in the whole ``cuprum`` surface, and interior modules
like this one are loaded while that surface is still being built. Reaching the
type through the shared lazy shim is what keeps the exception the caller
catches the very class ``cuprum`` exports.
"""

from __future__ import annotations


def _stdin_source_error(exc: BaseException) -> Exception:
    """Build the public ``StdinSourceError`` for a producer or encoder failure.

    Returned rather than raised so each handler can spell ``raise ... from``
    itself: the repository's linter requires the raise to be visible in the
    handler body, and a helper that raised on the caller's behalf would hide
    it.

    Parameters
    ----------
    exc : BaseException
        The producer's or encoder's original failure.

    Returns
    -------
    Exception
        An instance of the public ``StdinSourceError``, ready to raise with
        *exc* chained as ``__cause__``.
    """
    msg = f"stdin producer failed: {type(exc).__name__}: {exc!s}"
    return _source_error(msg, exc)


def _source_error(msg: str, exc: BaseException) -> Exception:
    """Build a ``StdinSourceError`` from its defining module.

    The type lives in ``cuprum.sh.execution``, which this module must not
    import at runtime: ``cuprum.sh`` pulls in the whole ``cuprum`` surface,
    and interior modules are loaded while that surface is still being built.
    The defining module is reached through the same lazy shim the rest of the
    execution layer uses, so the exception the caller catches is the class
    that ``cuprum`` exports.

    Parameters
    ----------
    msg : str
        The message for the raised error.
    exc : BaseException
        The producer's original exception, used only for the fallback path.

    Returns
    -------
    Exception
        An instance of the public ``StdinSourceError``.

    Raises
    ------
    RuntimeError
        If the shim cannot resolve the public type at all, which means the
        cuprum surface is broken rather than the caller's producer.
    """
    from cuprum._subprocess_context import _sh_module

    sh_module = _sh_module()
    error_type = getattr(sh_module, "StdinSourceError", None)
    if error_type is None:
        msg = "cuprum.sh.StdinSourceError is unavailable"
        raise RuntimeError(msg) from exc
    return error_type(msg)


__all__ = [
    "_source_error",
    "_stdin_source_error",
]
