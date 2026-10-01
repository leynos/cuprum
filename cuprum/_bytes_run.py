"""Shared internals for the byte-exact command and pipeline entry points.

The ``run_bytes()`` entry points on ``SafeCmd`` and ``Pipeline`` are not
separate runners. A binary run is one of the ordinary runs — same allowlist
enforcement, same stdin resolution, same timeout precedence, same
finalization — carrying one extra fact: its captured streams must not be
decoded. That fact travels as ``capture_bytes`` on the run's resolved state
and, from there, on the stream configuration the drains read.

What both entry points owe regardless of which of them is called is a decision
about the options they were handed, and that decision lives here:

- :func:`_validate_bytes_output` rejects the one combination bytes mode cannot
  honour — a line observer — before anything is spawned;
- :func:`_bytes_output` normalizes the optional options object a caller may
  omit.

Keeping them together is what stops the command and pipeline paths from
growing two slightly different answers to "what may a binary run be asked
for". This module is imported by :mod:`cuprum._bytes_run_mixin`, where the
public entry points live.
"""

from __future__ import annotations

# Sourced from ``cuprum.sh.output`` rather than ``cuprum.sh``: the package
# facade imports the mixin module, which imports this one, so reaching back
# through the facade would close a cycle at import time.
from cuprum.sh.output import RunOutputOptions


def _bytes_output(output: RunOutputOptions | None) -> RunOutputOptions:
    """Return the options a bytes-mode call runs with, defaulting when omitted.

    Returns
    -------
    RunOutputOptions
        The caller's options, or the all-defaults options object.

    """
    return output or RunOutputOptions()


def _validate_bytes_output(options: RunOutputOptions) -> None:
    """Reject the options combinations a byte-exact run cannot honour.

    The check is deliberately narrow. Everything else ``RunOutputOptions``
    offers — capture, both echo shorthands, the sink, the idle options — is
    meaningful for binary output, so bytes mode keeps them; refusing the whole
    options object would take away far more than binary output costs. Line
    observation is the single exception, and it is refused rather than
    downgraded: ``on_line`` carries decoded text, decoding is exactly what
    bytes mode exists to avoid, and a callback that silently stopped firing
    would be harder to notice than a rejected call.

    Called before anything is spawned, so a caller that combines the two
    learns about it from a ``ValueError`` rather than from a child that ran
    to completion and reported nothing.

    Raises
    ------
    ValueError
        If ``options.on_line`` is set.

    """
    if options.on_line is not None:
        msg = (
            "on_line observes decoded text and cannot be combined with byte-exact "
            "capture; drop on_line, or use the text-mode entry point"
        )
        raise ValueError(msg)


__all__ = [
    "_bytes_output",
    "_validate_bytes_output",
]
