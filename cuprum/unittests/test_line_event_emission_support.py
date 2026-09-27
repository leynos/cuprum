"""Shared stand-ins and helpers for the line-event emission tests.

The per-line emission tests are split by subject — payload parity, preparation
cost, and hook delivery — because a single module carrying all three trips the
repository's cohesion and file-size limits. Everything the three share lives
here: the command/program/project stand-ins the observation reads, the
observation factory, and the two recorders the cost tests use.

``_make_observation`` builds the observation the composed callback is driven
against. It deliberately mirrors the real populated shape: the observation
class gives ``exec_id`` a default factory, so this helper passes it only when a
case pins a token, and the metadata fields stay ``None`` exactly as a
non-observed run leaves them.

``_record_event_details`` patches ``_EventDetails`` where ``_line_callbacks``
resolves it. That module defers its import to call time, so patching the
defining module's attribute catches the production path without the test
reaching into the callback's internals.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum._pipeline_types import _ExecutionHooks, _StageObservation
from cuprum.events import ExecEvent, ExecHook, ExecId

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum.sh import SafeCmd


class _NullProject:
    """Project stand-in carrying a name."""

    name = "test-project"


class _NullProgram:
    """Program stand-in whose ``str`` form is the event's program name."""

    def __str__(self) -> str:
        """Return the program name."""
        return "test-program"


class _NullCmd:
    """Command stand-in exposing only what observation emission reads."""

    program = _NullProgram()

    def __init__(self, argv: tuple[str, ...] = ("arg",)) -> None:
        """Store the argv this stand-in reports."""
        self.argv = argv

    @property
    def argv_with_program(self) -> tuple[str, ...]:
        """The full argv, program name first."""
        return (str(self.program), *self.argv)

    @property
    def project(self) -> _NullProject:
        """The project stand-in."""
        return _NullProject()


class _CountingCmd(_NullCmd):
    """Command stand-in counting accesses to its derived argv property."""

    accesses = 0

    @property
    def argv_with_program(self) -> tuple[str, ...]:
        """Count the access, then return the tuple."""
        type(self).accesses += 1
        return (str(self.program), *self.argv)


class _ExecutionStub:
    """The three attributes ``_create_stream_callback`` reads off a real one.

    Standing in for ``_SubprocessExecution`` keeps this test off the spawn
    machinery while still driving the real factory function.
    """

    def __init__(
        self,
        observation: _StageObservation,
        on_line: cabc.Callable[[typ.Any], object] | None = None,
    ) -> None:
        """Bind the observation the callback composes against."""
        self.observation = observation
        self.on_line = on_line
        self.started_at = 0.0


def _make_observation(
    observe_hooks: tuple[ExecHook, ...] = (),
    *,
    cmd: object | None = None,
    clock: cabc.Callable[[], float] | None = None,
    exec_id: ExecId | None = None,
) -> _StageObservation:
    """Build a minimal observation with the requested observe hooks.

    The observation owns its own pending-task list, which is the list it
    extends with each scheduled async-hook task. A caller reads it back through
    ``observation.pending_tasks`` to see what a hook failure preserved.

    Returns
    -------
    _StageObservation
        An observation wired to the given hooks, clock, and its own task list.
    """
    hooks = _ExecutionHooks(
        before_hooks=(),
        after_hooks=(),
        observe_hooks=observe_hooks,
    )
    kwargs: dict[str, object] = {}
    if exec_id is not None:
        kwargs["exec_id"] = exec_id
    return _StageObservation(
        cmd=typ.cast("SafeCmd", _NullCmd() if cmd is None else cmd),
        hooks=hooks,
        tags={"project": "test-project"},
        cwd=None,
        env_overlay=None,
        pending_tasks=[],
        wall_clock=(lambda: 1234.5) if clock is None else clock,
        **typ.cast("typ.Any", kwargs),
    )


def _fields(event: ExecEvent) -> dict[str, object]:
    """Return every declared field of ``event`` by name."""
    return {field.name: getattr(event, field.name) for field in dc.fields(ExecEvent)}


def _record_event_details(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record every ``_EventDetails`` construction for the test's duration.

    ``_line_callbacks`` defers its import of ``_EventDetails`` to call time, so
    patching the defining module's attribute catches the production path
    without the test reaching into the callback's internals.

    Returns
    -------
    list[dict[str, object]]
        The keyword arguments of each recorded construction, in call order.
    """
    constructed: list[dict[str, object]] = []
    import cuprum._pipeline_types as pipeline_types

    real = pipeline_types._EventDetails

    def spy(**kwargs: object) -> object:
        """Record one construction's keywords, then build the real payload."""
        constructed.append(kwargs)
        return real(**typ.cast("typ.Any", kwargs))

    monkeypatch.setattr(pipeline_types, "_EventDetails", spy)
    return constructed


# Removed in EP-M2, when the hoist makes these assertions true. ``strict=True``
# turns the leftover marker into a failure the moment they start passing.
RED_REASON = (
    "5.2.1 red test: the un-hoisted callback rebuilds argv and _EventDetails "
    "on every line; EP-M2 removes this marker"
)


def _deliver(callback: cabc.Callable[[str], object], line_count: int) -> None:
    """Deliver ``line_count`` lines through ``callback``."""
    for index in range(line_count):
        callback(f"line-{index}")
