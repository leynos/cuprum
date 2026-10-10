"""Shared scaffolding for the structured-execution-event behaviour steps.

``structured_events.feature`` is driven by more than one step module so that no
single file has to carry the whole scenario matrix. Everything the modules
share lives here: the scenario-state keys, the two protocols that let one step
accept a command or a pipeline, and the run helpers that establish the
synchronous retention boundary.

Keep assertions out of this module. ``assert`` statements are permitted only in
``test_*.py`` files, where the repository's per-file lint exemptions cover them,
so the assertion helpers live in ``test_structured_events.py`` instead.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import io
import typing as typ

from cuprum import ScopeConfig, scoped, sh
from cuprum.events import ExecEvent
from cuprum.sh import (
    ExecutionContext,
    RunOutputOptions,
    SafeCmd,
    SafeCmdBuilder,
    StdinInput,
)
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.catalogue import ProgramCatalogue
    from cuprum.program import Program

# A fixed, JSON-like tag every scenario sets so correlation is checkable.
RUN_TAG = "bdd"

# The phases that carry output lines, and the lifecycle events every run reports.
LINE_PHASES = frozenset({"stdout", "stderr"})
LIFECYCLE_PHASES = frozenset({"plan", "start", "exit", "settled"})


class CommandCatalogue(typ.Protocol):
    """The surface the steps read off a program catalogue."""

    @property
    def allowlist(self) -> frozenset[Program]:
        """The programs the active scope permits."""
        ...


class Runnable(typ.Protocol):
    """The surface the run helpers need from a command or a pipeline.

    Both ``SafeCmd`` and ``Pipeline`` satisfy it. It deliberately declares
    neither ``__or__`` nor the concrete stage types: the two real ``__or__``
    operators accept exactly ``SafeCmd | Pipeline``, so a protocol restating
    them as ``Runnable | Runnable`` would be satisfied by neither operand —
    which is why the pipeline step pipes concretely typed stages and only
    widens afterwards.
    """

    def run_sync(
        self,
        *,
        output: RunOutputOptions | None = ...,
        context: ExecutionContext | None = ...,
        stdin: StdinInput | None = ...,
    ) -> object:
        """Execute the command synchronously."""
        ...


def join_script(*lines: str) -> str:
    """Join source lines into the single ``-c`` argument Python expects."""
    return "\n".join(lines)


def catalogue_and_builder() -> tuple[ProgramCatalogue, SafeCmdBuilder]:
    """Build the interpreter catalogue and a command builder bound to it."""
    catalogue, python_program = python_catalogue()
    return catalogue, sh.make(python_program, catalogue=catalogue)


def command_state(*args: str) -> dict[str, object]:
    """Build a command from ``args`` and return the scenario state for it."""
    catalogue, build = catalogue_and_builder()
    return {"catalogue": catalogue, "cmd": build(*args)}


def run_observed(
    behaviour_state: dict[str, object],
    catalogue: CommandCatalogue,
    cmd: Runnable,
) -> None:
    """Run ``cmd`` under an observe hook, retaining every event.

    ``run_sync`` drives its own event loop, so the retained list is complete by
    the time it returns: there are no tasks left to settle afterwards. The
    scenario text says the observer retains events "until asynchronous
    callbacks settle", which is what this synchronous boundary establishes.
    """
    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        """Collect execution events emitted during the run."""
        events.append(ev)

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        _ = cmd.run_sync(context=ExecutionContext(tags={"run_id": RUN_TAG}))
    behaviour_state["events"] = events


def run_captured_and_echoed(
    behaviour_state: dict[str, object],
    catalogue: CommandCatalogue,
    cmd: Runnable,
) -> None:
    """Run ``cmd`` with capture on, so the streams are drained and retained.

    Echo is what the scenario text adds on top, and it is routed to sink
    objects so the mirrored lines do not reach the test process's own stdout,
    which would corrupt pytest's captured output.
    """
    context = ExecutionContext(
        tags={"run_id": RUN_TAG},
        stdout_sink=io.StringIO(),
        stderr_sink=io.StringIO(),
    )
    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        """Collect execution events emitted during the run."""
        events.append(ev)

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        behaviour_state["result"] = cmd.run_sync(
            output=RunOutputOptions(capture=True, echo=True),
            context=context,
        )
    behaviour_state["events"] = events


def run_twice_with_distinct_contexts(
    behaviour_state: dict[str, object],
    catalogue: CommandCatalogue,
    cmd: Runnable,
) -> None:
    """Run one command twice concurrently, tagging each run differently.

    The two executions genuinely overlap, so a leak of hook or emitter state
    from one run into the other is observable rather than hidden behind
    sequential completion. Both runs share one observe hook, which is what
    makes the leak visible: a per-run hook would mask a shared emitter.

    The hook routes each event to the bucket named by its own ``run_id`` tag,
    so an event carrying another run's tag lands somewhere the assertions
    inspect rather than being quietly absorbed. A tag no run set fails the
    lookup, which is the loud outcome we want.
    """
    runs: dict[str, list[ExecEvent]] = {run_id: [] for run_id in ("first", "second")}
    command = typ.cast("SafeCmd", cmd)

    def hook(ev: ExecEvent) -> None:
        """Route one event to the bucket its own tag names."""
        runs[typ.cast("str", ev.tags["run_id"])].append(ev)

    async def execute(run_id: str) -> None:
        """Run the command once under its own tagged context."""
        _ = await command.run(
            context=ExecutionContext(tags={"run_id": run_id}),
            stdin=StdinInput(text=f"{run_id}-input\n"),
        )

    async def both_runs() -> None:
        """Launch both runs before awaiting either, so they overlap."""
        await asyncio.gather(execute("first"), execute("second"))

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        asyncio.run(both_runs())

    behaviour_state["runs"] = runs


def run_lifecycle_probe() -> list[ExecEvent]:
    """Run one command writing to both streams and return every event.

    Returns
    -------
    list[ExecEvent]
        The retained events, in observed emission order.
    """
    catalogue, python_program = python_catalogue()
    # Newlines are written with ``chr(10)`` rather than an escape sequence: the
    # doubled backslashes a ``'\\n'`` literal puts into the argv would read as a
    # UNC path to the snapshot-leak scanner, which flags the argv in the
    # committed snapshot. This matches the pipeline step's spelling.
    cmd = sh.make(python_program, catalogue=catalogue)(
        "-c",
        join_script(
            "import sys; sys.stdout.write('beta' + chr(10) + 'alpha' + chr(10));"
            "sys.stderr.write('gamma' + chr(10))",
        ),
    )
    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        """Collect execution events emitted during the run."""
        events.append(ev)

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        _ = cmd.run_sync(
            context=ExecutionContext(
                tags={"run_id": RUN_TAG},
                stdout_sink=io.StringIO(),
                stderr_sink=io.StringIO(),
            ),
        )
    return events


def catalogue_of(observed_command: dict[str, object]) -> CommandCatalogue:
    """Return the program catalogue the given step built the command with."""
    return typ.cast("CommandCatalogue", observed_command["catalogue"])


def runnable_of(observed_command: dict[str, object]) -> Runnable:
    """Return the command or pipeline the given step built."""
    return typ.cast("Runnable", observed_command["cmd"])


def retained_events(behaviour_state: dict[str, object]) -> list[ExecEvent]:
    """Return the retained events, failing loudly when there are none.

    Every assertion in this feature rests on the retained list, so an empty one
    would make them all vacuously true.

    Returns
    -------
    list[ExecEvent]
        The events the running scenario's observer collected.

    Raises
    ------
    AssertionError
        If the observer retained nothing.
    """
    events = typ.cast("list[ExecEvent]", behaviour_state.get("events", []))
    if not events:
        msg = "the observer must have retained at least one event"
        raise AssertionError(msg)
    return events


def expected_line_events(
    behaviour_state: dict[str, object],
) -> tuple[list[str], list[str]]:
    """Return the expected stdout and stderr lines for the scenario."""
    return (
        typ.cast("list[str]", behaviour_state["expected_stdout"]),
        typ.cast("list[str]", behaviour_state["expected_stderr"]),
    )


def observed_line_events(behaviour_state: dict[str, object]) -> list[ExecEvent]:
    """Return the retained line events, requiring the run to have emitted some."""
    return [ev for ev in retained_events(behaviour_state) if ev.phase in LINE_PHASES]


def normalize_event(event: ExecEvent) -> dict[str, object]:
    """Render one event for the snapshot, masking machine-specific values.

    Returns
    -------
    dict[str, object]
        The event's declared fields by name, with volatile values replaced by
        fixed markers and the line sequence left intact.
    """
    rendered: dict[str, object] = {}
    for field in dc.fields(ExecEvent):
        value = getattr(event, field.name)
        if field.name in _NORMALIZED_FIELDS:
            rendered[field.name] = "<normalized>"
        elif field.name in _PATH_FIELDS:
            rendered[field.name] = "<path>"
        elif field.name == "argv":
            rendered[field.name] = ("<path>", *typ.cast("tuple[str, ...]", value)[1:])
        else:
            rendered[field.name] = value
    return rendered


# Fields normalized for the snapshot: each carries a real-machine value that
# cannot be stable across runs. Nothing else is normalized — line content,
# phase, ordering, stage ownership, and the shape of the payload all stay in
# the snapshot, because those are what the hoist could plausibly break.
_NORMALIZED_FIELDS = frozenset({
    "pid",
    "timestamp",
    "duration_s",
    "exec_id",
    "max_rss_bytes",
    "user_cpu_seconds",
    "system_cpu_seconds",
})

# Fields compacted to a fixed marker because they are full paths, not data.
# ``cwd`` and ``env`` are absent (``None``) for these runs, so they need no
# normalization and need not be listed here.
_PATH_FIELDS = frozenset({"program"})
