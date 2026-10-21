"""Whether a ``(stdout, stderr)`` capture pair retains anything.

Both the run options and the pipeline configuration tag their observation
events with "is this a capturing run?", and the two must not disagree. The
predicate lives here rather than beside the policy types because it is the
only thing both callers need and neither owns: keeping it next to the policies
pushed ``cuprum.capture_policy`` past the project's module-size ceiling, and a
second copy in either caller would be the drift this module exists to prevent.

A stream is captured when it holds a policy, or the ``True`` shorthand the
options constructor has not resolved yet. ``False`` is the shorthand for "no
policy", so it is tested as such: a caller may reach this with a field the
constructor has not yet resolved.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.capture_policy import CapturePolicy

__all__ = [
    "captures_any_stream",
]

type CaptureSetting = CapturePolicy | bool
"""One stream's capture setting before its constructor resolves it.

A policy, or the ``True``/``False`` shorthand for both streams. Named here,
rather than spelled inline, because ``bool`` in a parameter annotation reads
to the boolean-typed parameter rule as a flag — and these arguments are not
flags: they carry a declared field value, where ``bool`` is one of the
spellings the field accepts.
"""


def captures_any_stream(
    capture_stdout: CaptureSetting | None,
    capture_stderr: CaptureSetting | None,
) -> bool:
    """Whether a ``(stdout, stderr)`` capture pair captures anything.

    A run that captures anything at all is a capturing run; a run that named no
    policy for either stream captures nothing.

    Parameters
    ----------
    capture_stdout : CapturePolicy | bool | None
        The stdout policy, shorthand, or ``None`` when stdout is uncaptured.
    capture_stderr : CapturePolicy | bool | None
        The stderr policy, shorthand, or ``None`` when stderr is uncaptured.

    Returns
    -------
    bool
        ``True`` when either stream is captured.

    Examples
    --------
    >>> from cuprum.capture_policy import FullCapture
    >>> captures_any_stream(FullCapture(), None)
    True
    >>> captures_any_stream(False, None)
    False
    >>> captures_any_stream(False, True)
    True
    """
    return _is_captured(capture_stdout) or _is_captured(capture_stderr)


def _is_captured(capture: CaptureSetting | None) -> bool:
    """Whether one stream's capture setting retains anything.

    Returns
    -------
    bool
        ``False`` for ``None`` and ``False``; ``True`` for a policy or ``True``.
    """
    return capture is not None and capture is not False
