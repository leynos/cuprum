"""Rejection of ``subprocess`` sentinels passed as borrowed descriptors.

``subprocess`` reads a negative integer as a directive rather than a
descriptor: ``-1`` is ``PIPE``, ``-2`` is ``STDOUT``, and ``-3`` is
``DEVNULL``. A binding that names one is therefore a lie about what the child
receives, and on an *output* stream the lie is a hang rather than a miswrite —
``subprocess`` builds a pipe the resolved binding says does not exist, nothing
reads it, and the child blocks on a full buffer.

Two independent routes reach ``subprocess``, so two guards exist and both are
exercised here:

- ``StdioTarget.fd(-1)`` carries the sentinel as a literal payload, refused by
  ``cuprum.sh.stdio._reject_sentinel_descriptor`` when the target is built.
- A caller's own file object can return one from ``fileno()``, which no
  constructor can foresee. That value is refused by
  ``cuprum._stdio_binding_guards._reject_negative_descriptor`` at resolution,
  against the descriptor actually handed to ``subprocess``.

Every rejection is paired with an accepted near-miss naming the same stream, so
a guard that refused every descriptor would fail the pairing rather than pass
the rejection. The genuine-descriptor controls are the ones that keep these
rows from being evidence of a blanket refusal.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum._stdio_binding_guards import _reject_negative_descriptor
from cuprum._stdio_plan import _resolve_output_binding
from cuprum.sh import RunOutputOptions, StdioTarget

# The three sentinels ``subprocess`` defines, named individually rather than
# represented by one sample: each has a *different* meaning to the child's
# runtime, and a guard that happened to refuse only ``-1`` would still let a
# caller ask for ``DEVNULL`` on a stream the binding reports as borrowed.
_SENTINELS = (
    pytest.param(-1, "PIPE", id="pipe"),
    pytest.param(-2, "STDOUT", id="stdout-dup"),
    pytest.param(-3, "DEVNULL", id="devnull"),
)


class _LyingFile:
    """A file object whose ``fileno`` returns whatever it was handed.

    ``fileno`` is caller-supplied code, so it can return a sentinel just as an
    ``int`` payload can — and unlike the payload, no constructor sees the value
    until resolution. This stands in for a wrapper that forwards ``fileno``
    from an inner descriptor, which is the shape that would produce one in
    practice.
    """

    def __init__(self, descriptor: int) -> None:
        """Remember the descriptor this wrapper should report."""
        self._descriptor = descriptor

    def fileno(self) -> int:
        """Report the descriptor this object was built to lie about."""
        return self._descriptor

    def flush(self) -> None:
        """Accept the pre-spawn flush cuprum performs on a borrowed object."""


def _lying_fd_target(descriptor: int) -> StdioTarget:
    """Bind a borrowed stream to a file double reporting *descriptor*.

    The cast is the honest description of what is happening: ``_LyingFile``
    implements the part of the file protocol cuprum reaches — ``fileno`` and
    ``flush`` — and not the rest, so it is passed as the borrowed object it
    stands in for. ``fileno`` is the whole point of the double, and ``flush``
    exists so the pre-spawn flush is not the thing under test. This mirrors
    ``borrowed_flush_target`` in ``_stdio_classification_support``, which
    confines the same kind of cast to one place for the same reason.

    Returns
    -------
    StdioTarget
        An ``fd`` target whose borrowed object reports *descriptor*.
    """
    return StdioTarget.fd(typ.cast("typ.IO[str]", _LyingFile(descriptor)))


@pytest.mark.parametrize(("sentinel", "meaning"), _SENTINELS)
def test_a_sentinel_payload_is_refused_when_the_target_is_built(
    sentinel: int,
    meaning: str,
) -> None:
    """A literal sentinel never becomes a target at all.

    The refusal is at construction rather than at the spawn because this is a
    caller error with a caller-visible remedy: the message points at the two
    variants that mean what a sentinel is being misused to express.
    """
    with pytest.raises(ValueError, match=r"negative\s+values are subprocess") as info:
        StdioTarget.fd(sentinel)

    message = str(info.value)
    assert str(sentinel) in message, (
        f"the refusal must name the offending value; got {message!r}"
    )
    assert meaning in message, (
        f"the refusal must say what {sentinel} means to subprocess; got {message!r}"
    )
    assert "StdioTarget.pipe()" in message, (
        f"the refusal must name the variant that means {meaning!r}; got {message!r}"
    )


@pytest.mark.parametrize("descriptor", [0, 1, 2, 7])
def test_a_genuine_descriptor_payload_is_accepted(descriptor: int) -> None:
    """The near-miss: the rule rejects sentinels, not small or standard fds.

    ``0``, ``1``, and ``2`` are the standard streams and ``7`` is an arbitrary
    spare; all four are real descriptors and all four must build. A guard that
    refused every non-positive value, or every value below some threshold,
    would pass every rejection above and fail here.
    """
    target = StdioTarget.fd(descriptor)

    assert target.kind == "fd", "a genuine descriptor must build an fd target"
    assert target.fd_value == descriptor, (
        "a genuine descriptor must be read back unchanged, "
        f"got {target.fd_value!r} for {descriptor!r}"
    )


@pytest.mark.parametrize(("sentinel", "meaning"), _SENTINELS)
def test_a_file_object_reporting_a_sentinel_is_refused_at_resolution(
    sentinel: int,
    meaning: str,
) -> None:
    """A sentinel arriving through ``fileno`` is caught before the spawn.

    Construction cannot see this one — the payload is a file object, and the
    guard that refuses literal sentinels leaves file objects alone because they
    are resolved later. That is exactly why resolution needs its own guard: the
    value ``subprocess`` would receive does not exist until ``fileno`` is
    called.
    """
    options = RunOutputOptions(
        capture=False,
        stdout=_lying_fd_target(sentinel),
    )

    with pytest.raises(ValueError, match=r"resolved to the descriptor") as info:
        _resolve_output_binding("stdout", _target_from(options))

    message = str(info.value)
    assert str(sentinel) in message, (
        f"the refusal must name the descriptor fileno returned; got {message!r}"
    )
    assert "_LyingFile" in message, (
        "the refusal must name the source the descriptor came from, so a "
        f"caller with two redirected streams knows which lied; got {message!r}"
    )
    assert meaning in message, (
        f"the refusal must say what {sentinel} means to subprocess; got {message!r}"
    )


def test_a_file_object_reporting_a_genuine_descriptor_resolves() -> None:
    """The near-miss for the resolution guard: a real fd through ``fileno``.

    Pairing this with the rows above is what shows the resolution guard scopes
    to the *value* rather than to file objects as a class. A guard that refused
    every ``fileno`` result would satisfy each rejection above.
    """
    options = RunOutputOptions(capture=False, stdout=_lying_fd_target(7))

    binding = _resolve_output_binding("stdout", _target_from(options))

    assert binding.descriptor == 7, (
        "a genuine descriptor from fileno must reach the spawn, "
        f"got {binding.descriptor!r}"
    )
    assert binding.borrowed is not None, (
        "a file-object route must carry the object for the pre-spawn flush"
    )


def test_the_guard_refuses_a_negative_descriptor_and_names_its_source() -> None:
    """The resolver's guard is total over the value, independent of the route.

    Called directly because the two routes above reach it through different
    payload types, and this is the one place the contract is stated: a negative
    descriptor is refused, and the message names both the stream and where the
    value came from.
    """
    with pytest.raises(ValueError, match=r"negative values are subprocess") as info:
        _reject_negative_descriptor("stderr", -1, resource="SomeWrapper")

    message = str(info.value)
    assert "stderr" in message, f"the refusal must name the stream; got {message!r}"
    assert "-1" in message, f"the refusal must name the value; got {message!r}"
    assert "SomeWrapper" in message, (
        f"the refusal must name the source; got {message!r}"
    )


@pytest.mark.parametrize("descriptor", [0, 1, 2, 7, 1024])
def test_the_guard_accepts_every_non_negative_descriptor(descriptor: int) -> None:
    """The direct near-miss: zero and above pass, and the call returns ``None``."""
    assert _reject_negative_descriptor("stdout", descriptor, resource="int") is None, (
        f"a non-negative descriptor {descriptor} must be accepted"
    )


def _target_from(options: RunOutputOptions) -> StdioTarget:
    """Read the stdout target back out of built options."""
    target: StdioTarget | None = options.stdout
    assert target is not None, "the options must carry the target they were built with"
    return target
