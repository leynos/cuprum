"""What a single-command run settles with, and the result it settles into.

Two halves of one concern live here because they change for the same reasons:

- :class:`_DirectCompletion` is the settled record of a run — the exit code and
  the instant it settled, then whatever each stream reconciled to. The waiting
  helpers in :mod:`cuprum._subprocess_execution` produce it; this module only
  says what it holds.
- :func:`_build_command_result` turns that record into the public result class
  the run's mode calls for, and :func:`_relay_fallbacks_for_result` flattens the
  per-stream echo diagnostics the same results carry.

The split from the orchestration module is a size boundary, not an ownership
one: the orchestration module owns *when* a run settles, this one owns *what*
it reports once it has. Keeping the result construction beside the record it
consumes is what stops the two from being edited apart — a field added to the
record without a matching reading at the construction site is visible in one
file rather than two.

The two classes are built only when :mod:`cuprum._result_assembly`'s narrowing
rules say the settled payloads permit it; those rules stay there and are
imported, not restated.

``cuprum._subprocess_execution`` re-exports :func:`_relay_fallbacks_for_result`
and :class:`_DirectCompletion`, so importers that have always reached for them
under that module's name keep resolving.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum._result_assembly import (
    _is_captured_text_pair,
    _recorded_captures,
    _require_bytes,
    _require_captured_text,
    _require_text,
    _RunMeasurements,
)
from cuprum._subprocess_context import _sh_module

if typ.TYPE_CHECKING:
    from cuprum._result_types import _AnyCommandResult
    from cuprum._streams import _RelayDiagnostics
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import _StreamPayload
    from cuprum.capture_policy import CapturedStream
    from cuprum.echo_events import RelayFallback
    from cuprum.program import Program


@dc.dataclass(frozen=True, slots=True)
class _DirectCompletion:
    """The settled facts of a direct run, once its streams have reconciled.

    ``stdout_text``, ``stderr_text``, ``captures``, and ``relay_diagnostics``
    stay ``None`` (or, for ``captures``, empty) on the direct path, which
    captures nothing; the stream path overwrites all four. Returning them as
    one record keeps the waiting and the result assembly in separate helpers,
    so neither accumulates enough locals to trip the repository's
    ``too-many-locals`` ceiling.

    The two payload fields are typed ``str | bytes`` because the drain reports
    whichever the run's mode asked for. Nothing here inspects the type: the
    mode is read once, at the seam that chooses which result class to build,
    so the payload reaches its field unexamined.

    ``captures`` carries the drain's own records, in stdout-then-stderr order,
    and travels beside the payloads rather than being read off the drain a
    second time: the drain happens exactly once per run, and only its result
    knows how each policy retained its stream.
    """

    exit_code: int
    exited_at: float
    stdout_text: _StreamPayload | None
    stderr_text: _StreamPayload | None
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics] | None
    captures: tuple[CapturedStream | None, CapturedStream | None] = (None, None)


def _relay_fallbacks_for_result(
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics] | None,
) -> tuple[RelayFallback, ...]:
    """Flatten per-stream diagnostics into one result tuple.

    The order is stdout's records then stderr's; each record carries its own
    stream, and this order does not reconstruct chronological interleaving
    between the two streams.

    Returns
    -------
    tuple[RelayFallback, ...]
        The command's categorical echo-failure and delivery-loss records,
        empty when its diagnostics are absent or recorded nothing.
    """
    if relay_diagnostics is None:
        return ()
    return relay_diagnostics[0].snapshot() + relay_diagnostics[1].snapshot()


def _build_command_result(
    execution: _SubprocessExecution,
    completion: _DirectCompletion,
    measurements: _RunMeasurements,
) -> _AnyCommandResult:
    """Assemble the run's result, choosing the class its mode calls for.

    This is the only place either result class is constructed, and the only
    place the run's mode is read on the way out. One builder rather than two
    keeps every measurement — duration, rusage, the relay diagnostics — from
    being computed twice under two names, which is how a binary run would come
    to report figures that a text run of the same command did not. The two
    constructions differ in the class, in the narrowing of the captured
    payloads, and in the capture records the class can carry; both narrowing
    rules are read from :mod:`cuprum._result_assembly` rather than restated
    here.

    Every class carries ``captures``. A byte-exact run needs it most: a spilled
    stream is read back, so a failed write or a failed flush can shorten the
    payload it hands over, and the payload alone cannot say so. The text run
    needs it for the complementary reason: a ``FileBackedCapture`` that spilled
    reports ``None`` rather than text, and the record is then the only thing
    naming the file that holds the stream.

    Text capture narrows to ``CapturedResult`` only when *both* settled
    payloads are decoded text. That class promises two strings, and a run that
    captured one stream alone, or spilled one to a file, settles the other as
    ``None`` — a stream it did capture, which the narrowed class would have to
    reject. Those runs report the payloads and their records as an ordinary
    ``CommandResult``.

    Returns
    -------
    CommandResult | BytesCommandResult
        A ``BytesCommandResult`` when the run captured bytes, a
        ``CapturedResult`` when both text streams are decoded text, or an
        ordinary ``CommandResult`` otherwise.
    """
    sh = _sh_module()
    shared = _shared_result_fields(execution, completion, measurements)
    if execution.capture_bytes:
        return sh.BytesCommandResult(
            **shared,
            stdout=_require_bytes(completion.stdout_text, "stdout"),
            stderr=_require_bytes(completion.stderr_text, "stderr"),
        )
    if _is_captured_text_pair(completion.stdout_text, completion.stderr_text):
        return sh.CapturedResult(
            **shared,
            stdout=_require_captured_text(
                completion.stdout_text,
                "stdout",
                program=execution.cmd.program,
                exec_id=str(execution.observation.exec_id),
            ),
            stderr=_require_captured_text(
                completion.stderr_text,
                "stderr",
                program=execution.cmd.program,
                exec_id=str(execution.observation.exec_id),
            ),
        )
    return sh.CommandResult(
        **shared,
        stdout=_require_text(completion.stdout_text, "stdout"),
        stderr=_require_text(completion.stderr_text, "stderr"),
    )


class _SharedResultFields(typ.TypedDict):
    """The result fields every output mode of a settled run declares.

    Declared rather than left as ``dict[str, object]`` so unpacking it into a
    result constructor keeps each field's type: an opaque value type would
    make the ``**`` argument unrepresentable to the type checker, and the
    fields would stop being checked against the declarations they fill.
    """

    program: Program
    argv: tuple[str, ...]
    exit_code: int
    pid: int
    started_at: float
    duration: float
    max_rss_bytes: int | None
    user_cpu_seconds: float | None
    system_cpu_seconds: float | None
    resolved_path: str | None
    relay_fallbacks: tuple[RelayFallback, ...]
    captures: tuple[CapturedStream, ...]


def _shared_result_fields(
    execution: _SubprocessExecution,
    completion: _DirectCompletion,
    measurements: _RunMeasurements,
) -> _SharedResultFields:
    """Build the fields every result class of a settled run declares.

    Returns
    -------
    _SharedResultFields
        The shared constructor fields, keyed by parameter name.
    """
    return {
        "program": execution.cmd.program,
        "argv": execution.cmd.argv,
        "exit_code": completion.exit_code,
        **measurements.as_kwargs(),
        "captures": _recorded_captures(completion.captures),
    }


__all__ = [
    "_DirectCompletion",
    "_build_command_result",
    "_relay_fallbacks_for_result",
    "_shared_result_fields",
]
