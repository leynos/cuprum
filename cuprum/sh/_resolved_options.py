"""The resolved views of ``RunOutputOptions``' declared fields.

``RunOutputOptions`` declares each output decision twice: once in the spelling
the caller used — a shorthand ``bool``, a per-stream ``None`` meaning "inherit",
a policy object, a ``BrokenPipePolicy`` or its string — and once as the run
will actually apply it. The second half is the query surface: ``resolved_echo``,
``resolved_capture``, ``resolved_broken_pipe_policy``, and the one-bool
``captures_any_stream`` reading that the observation tags report.

That surface lives here, as a mixin the options dataclass inherits, because
``cuprum/sh/output.py`` sits at the repository's 400-line ceiling. The split is
the same one ADR-007 records for the surrounding modules and that
``cuprum/sh/bytes_entry_points.py`` makes for ``SafeCmd``: a coherent block of
one class's public surface moves into its own module, the class inherits it,
and every name still resolves as an ordinary bound member with an unchanged
public signature.

What moves is the *query* half of the options — what the caller's spelling
resolved to — while the fields every query reads stay where they are declared,
on the dataclass. The mixin declares ``__slots__`` empty rather than omitting
it, so an un-slotted mixin cannot donate a ``__dict__`` to the frozen, slotted
dataclass inheriting it. No annotation here names the options type; each
property reads attributes the dataclass declares, and only the type checker
needs the class, through a ``TYPE_CHECKING`` import that costs nothing at run
time and keeps the import cycle out.
"""

from __future__ import annotations

import typing as typ

from cuprum.capture_presence import captures_any_stream as _captures_any_stream
from cuprum.echo_events import BrokenPipePolicy, _parse_broken_pipe_policy

if typ.TYPE_CHECKING:
    from cuprum.capture_policy import CapturePolicy
    from cuprum.sh.output import RunOutputOptions

__all__ = ["_ResolvedOptionsMixin"]


class _ResolvedOptionsMixin:
    """Resolved views of the options fields a run reads at spawn time.

    The mixin contributes the four queries to ``RunOutputOptions``. It is not a
    dataclass, and it carries no state: every property here derives its answer
    from fields the options dataclass declares, so the mixin adds behaviour
    without adding a second place for the values to live.
    """

    __slots__ = ()

    @property
    def resolved_broken_pipe_policy(self) -> BrokenPipePolicy:
        """The policy behind the declared ``BrokenPipePolicy | str`` field.

        ``__post_init__`` normalizes the field, but the declared type stays
        wide because a caller may spell the policy as a string. This is the
        narrow view the execution layer reads, so it never has to parse.

        Returns
        -------
        BrokenPipePolicy
            The normalized policy member, never a string and never ``None``.
        """
        options = typ.cast("RunOutputOptions", self)
        return _parse_broken_pipe_policy(options.broken_pipe_policy)

    @property
    def resolved_capture(self) -> tuple[CapturePolicy | None, CapturePolicy | None]:
        """The resolved ``(capture_stdout, capture_stderr)`` policies.

        ``__post_init__`` turns each stream's field into a policy, so the
        returned pair is always concrete and says exactly what the run will
        do; ``None`` means that stream is not captured at all. The declared
        fields keep the caller's own spelling, so
        ``dataclasses.replace(options, capture=False)`` re-resolves the pair
        rather than inheriting it: a replacement is a new value whose shorthand
        still decides any stream left unset.

        Returns
        -------
        tuple[CapturePolicy | None, CapturePolicy | None]
            The stdout and stderr policies, in that order, each either a
            policy object or ``None`` for a stream that is not captured.

        Examples
        --------
        >>> from cuprum.capture_policy import FullCapture
        >>> from cuprum.sh.output import RunOutputOptions
        >>> RunOutputOptions().resolved_capture
        (FullCapture(), FullCapture())
        >>> RunOutputOptions(capture=False).resolved_capture
        (None, None)
        >>> RunOutputOptions(capture=False, capture_stderr=True).resolved_capture
        (None, FullCapture())
        >>> import dataclasses as dc
        >>> dc.replace(RunOutputOptions(), capture=False).resolved_capture
        (None, None)
        """
        options = typ.cast("RunOutputOptions", self)
        return options._resolved_capture

    @property
    def captures_any_stream(self) -> bool:
        """Whether either stream is captured under the resolved policies.

        The one-bool reading of ``resolved_capture``: the observation tags
        report capture as a single flag, and this keeps that flag meaning what
        it always did now that the policies differ per stream. A run that
        captures anything at all is a capturing run.

        Returns
        -------
        bool
            ``True`` when either stream has a policy, ``False`` when neither
            does.

        Examples
        --------
        >>> from cuprum.sh.output import RunOutputOptions
        >>> RunOutputOptions().captures_any_stream
        True
        >>> RunOutputOptions(capture=False).captures_any_stream
        False
        >>> RunOutputOptions(capture=False, capture_stderr=True).captures_any_stream
        True
        """
        options = typ.cast("RunOutputOptions", self)
        return _captures_any_stream(*options._resolved_capture)

    @property
    def resolved_echo(self) -> tuple[bool, bool]:
        """The resolved ``(echo_stdout, echo_stderr)`` gates.

        ``__post_init__`` fills any ``None`` per-stream field from ``echo``,
        so the returned pair is always concrete and reflects an explicit
        per-stream override where one was supplied.

        Returns
        -------
        tuple[bool, bool]
            The stdout and stderr echo gates, in that order.
        """
        options = typ.cast("RunOutputOptions", self)
        # The frozen dataclass's ``__post_init__`` resolves both fields, but
        # their declared types stay ``bool | None``; the cast states the
        # constructor's contract, which the declared types cannot express.
        return (options.echo_stdout, options.echo_stderr)  # ty: ignore[invalid-return-type]
