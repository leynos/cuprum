"""Resolve a ``RunOutputOptions`` capture field into a concrete policy.

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
    capture_stdout: CapturePolicy | bool | None,
    capture_stderr: CapturePolicy | bool | None,
) -> tuple[CapturePolicy | None, CapturePolicy | None]:
    """Read a run's resolved per-stream policies as the pair the run uses.

    The declared field types stay ``CapturePolicy | bool | None`` because a
    caller may spell a stream as the shorthand bool; only the constructor's
    resolution narrows them, which the declared types cannot express to a type
    checker. Keeping the read beside the resolution is what makes that
    widening local rather than something each importer rediscovers.

    Returns
    -------
    tuple[CapturePolicy | None, CapturePolicy | None]
        The stdout and stderr policies, in that order, each either a policy
        object or ``None`` for a stream that is not captured.
    """
    return (  # ty: ignore[invalid-return-type]
        capture_stdout,
        capture_stderr,
    )


__all__ = ["_resolve_capture", "_resolved_capture"]
