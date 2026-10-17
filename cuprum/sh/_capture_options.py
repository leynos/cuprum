"""Resolve ``RunOutputOptions`` capture fields into concrete policies.

One stream's capture field accepts three spellings — ``None`` to inherit the
``capture`` shorthand, a ``bool`` as that shorthand's two ends, or a policy
object — and the run needs exactly one of them: a policy, or ``None`` for a
stream that is not captured. Deciding that in one place keeps stdout and stderr
resolving identically, which is what lets the two streams be configured
independently without the second one drifting from the first.

The validation lives here rather than at the drain so a caller learns from a
``ValueError`` before anything is spawned, instead of from a child that ran and
captured something unintended.
"""

from __future__ import annotations

from cuprum.capture_policy import (
    BoundedCapture,
    CapturePolicy,
    FileBackedCapture,
    FullCapture,
)


def _resolve_capture(
    *,
    shorthand: bool,
    declared: CapturePolicy | bool | None,
    field: str,
) -> CapturePolicy | None:
    """Resolve one stream's capture field against the ``capture`` shorthand.

    ``None`` inherits the shorthand, which is how ``echo_stdout`` and
    ``echo_stderr`` already resolve from ``echo``; ``True`` and ``False`` are
    accepted as the shorthand's spelling for the two ends of the range, so a
    caller can write ``capture_stderr=False`` without naming a policy class
    for the stream they want off.

    A value that is neither a policy nor a ``bool`` is rejected here rather
    than at the drain, so a caller learns from a ``ValueError`` before
    anything is spawned instead of from a child that ran and captured
    something unintended.

    Returns
    -------
    CapturePolicy | None
        The policy this stream resolves to, or ``None`` when the stream is
        not captured.

    Raises
    ------
    ValueError
        If the value is not ``None``, a ``bool``, or a capture policy.
    """
    match declared:
        case None:
            return FullCapture() if shorthand else None
        case bool():
            return FullCapture() if declared else None
        case FullCapture() | BoundedCapture() | FileBackedCapture():
            return declared
    msg = (
        f"RunOutputOptions {field} must be a capture policy, a bool, or None, "
        f"got {declared!r}"
    )
    raise ValueError(msg)


def _resolved_capture(
    *,
    shorthand: bool,
    capture_stdout: CapturePolicy | bool | None,
    capture_stderr: CapturePolicy | bool | None,
) -> tuple[CapturePolicy | None, CapturePolicy | None]:
    """Resolve both streams' capture fields against the ``capture`` shorthand.

    The pair is resolved together because the shorthand is one decision for the
    run and the two streams are two answers to it. Resolving them apart is what
    let a caller's ``capture=False`` be silently overridden by a pair stored
    back over the declared fields: ``dataclasses.replace`` re-passes every init
    field, so a resolution written there returns as an explicit per-stream
    declaration. Resolving here, into a non-init field, leaves the declared
    fields holding the caller's own spelling and lets a replacement re-resolve.

    Returns
    -------
    tuple[CapturePolicy | None, CapturePolicy | None]
        The stdout and stderr policies, in that order, each either a policy
        object or ``None`` for a stream that is not captured.

    Raises
    ------
    ValueError
        If either value is not ``None``, a ``bool``, or a capture policy.
    """
    return (
        _resolve_capture(
            shorthand=shorthand,
            declared=capture_stdout,
            field="capture_stdout",
        ),
        _resolve_capture(
            shorthand=shorthand,
            declared=capture_stderr,
            field="capture_stderr",
        ),
    )


__all__ = ["_resolve_capture", "_resolved_capture"]
