"""Spawn-time executable bindings: what runs, what is reported, what is not.

``cuprum.context.resolve_executable`` and the binding scope are pinned by
``test_executable_context``. What nothing exercised was the *join* between that
resolution and the process that actually starts: the binding had to reach the
child's ``argv[0]``, the resolved path had to appear on the lifecycle events
and on the returned ``CommandResult``, and resolution had to stay strictly
after allowlist enforcement.

That join is where the interesting failures live, and every one of them leaves
an existing test green. Binding a program but spawning the catalogued name
still passes every resolution test; spawning the bound path but reporting
``None`` still passes every execution test; resolving before the allowlist
check still passes every allowlist test. The arrangements below are chosen so
each of those regressions is caught.

Three carry the weight:

- an *impostor* holding the catalogued program's own path, which an execution
  resolves past — so a regression that ran the catalogued name would run it,
  and its marker would appear in the captured output;
- a *resolver* that counts its calls, so a regression that re-resolved per
  event rather than once per execution shows up as a count above one; and
- a program that is *bound but not allowlisted*, so a regression that resolved
  before enforcing shows up as a resolver that ran for a refused command.

The fixtures here — the reporting scripts, the catalogue builder, and the
capturing runners — are shared with
``test_executable_binding_relative_paths``, which is where the examples that
compose a relative binding with a relative execution directory live. They are
imported from this module because this is where the bound-run examples they
were written for live.
"""

from __future__ import annotations

import asyncio
import stat
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.catalogue import ECHO, LS, ProgramCatalogue
from cuprum.context import current_context
from cuprum.context._scope import ForbiddenProgramError
from cuprum.context.registration import bind_executable
from cuprum.program import Program
from cuprum.sh import ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.events import ExecEvent
    from cuprum.sh import CommandResult, Pipeline, PipelineResult, SafeCmd

_APPROVED = "APPROVED"
_IMPOSTOR = "IMPOSTOR"

# Each script reports the marker it was written with and the path it was
# started as, so an assertion about which file ran reads the child's own
# report. ``{marker}`` is substituted per script; ``__file__`` is the path the
# interpreter was handed, which is exactly what ``argv[0]`` carries.
_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "import sys\n"
    "sys.stdout.write('{marker}:' + sys.argv[0] + '\\n')\n"
)

# A middle pipeline stage that forwards its predecessor's report *and* adds its
# own. A pipeline's captured stdout belongs to the last stage alone, so a
# consumer that consumed its stdin without echoing it would leave the producer's
# identity observable only through metadata. Forwarding makes both stages'
# reports readable from one captured stream, which is what lets the pipeline
# regression assert each stage's executable independently rather than inferring
# the producer from the consumer's success.
_RELAY_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "import sys\n"
    "relayed = sys.stdin.read()\n"
    "sys.stdout.write(relayed)\n"
    "sys.stdout.write('{marker}:' + sys.argv[0] + '\\n')\n"
)


def _write_script(path: Path, marker: str, template: str = _SCRIPT) -> Path:
    """Write an executable script reporting ``marker`` and its own ``argv[0]``.

    Parameters
    ----------
    path : Path
        Destination for the generated script.
    marker : str
        Marker the script writes ahead of its own ``argv[0]``.
    template : str
        Script body, which must carry a ``{marker}`` placeholder. Defaults to
        the reporting script; pass :data:`_RELAY_SCRIPT` for a stage that also
        forwards its stdin.

    Returns
    -------
    Path
        The script's path, so assertions can name it without re-deriving it.
    """
    path.write_text(template.format(marker=marker), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _catalogue_for(*programs: str) -> tuple[ProgramCatalogue, tuple[Program, ...]]:
    """Build a catalogue allowlisting ``programs`` under those logical names.

    Returns
    -------
    tuple[ProgramCatalogue, tuple[Program, ...]]
        The catalogue and the programs in the order supplied.
    """
    entries = tuple(Program(name) for name in programs)
    catalogue = ProgramCatalogue.from_programs(
        *entries,
        name="executable-binding-tests",
        documentation_locations=(
            "docs/users-guide.md#bind-a-catalogued-program-to-a-specific-executable",
        ),
    )
    return catalogue, entries


def _run_capturing(cmd: SafeCmd, *, cwd: str | Path | None = None) -> CommandResult:
    """Run one command capturing output, optionally in ``cwd``."""
    return cmd.run_sync(
        output=RunOutputOptions(capture=True, echo=False),
        context=None if cwd is None else ExecutionContext(cwd=str(cwd)),
    )


def _run_pipeline_capturing(pipeline: Pipeline) -> PipelineResult:
    """Run a pipeline capturing output."""
    return pipeline.run_sync(output=RunOutputOptions(capture=True, echo=False))


def test_a_bound_program_runs_the_bound_file_not_the_catalogued_one(
    tmp_path: Path,
) -> None:
    """The binding decides the executable; the catalogue entry does not.

    The catalogued program names a real, executable script — the impostor —
    that would run if the binding were ignored. The run is arranged so that
    falling back to the catalogued path is not merely possible but would
    succeed, which is what makes this a witness: the approved marker can only
    appear if the binding is what selected the file.
    """
    impostor = _write_script(tmp_path / "catalogued.py", _IMPOSTOR)
    approved = _write_script(tmp_path / "approved.py", _APPROVED)
    catalogue, (tool,) = _catalogue_for(str(impostor))

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, str(approved)),
    ):
        result = _run_capturing(sh.make(tool, catalogue=catalogue)())

    assert result.resolved_path == str(approved), (
        f"the result must report the bound executable, got {result.resolved_path!r}"
    )
    assert result.stdout == f"{_APPROVED}:{approved}\n", (
        f"the bound script must be the one that ran, got {result.stdout!r}"
    )
    assert _IMPOSTOR not in (result.stdout or ""), (
        "the catalogued executable must not run once the program is bound"
    )


def test_the_child_receives_the_bound_path_as_its_own_argv0(tmp_path: Path) -> None:
    """The bound executable is what the child sees as ``argv[0]``.

    The script reports ``sys.argv[0]``, so this reads the child's own view
    rather than the parent's intention. A regression that spawned the
    catalogued name while reporting the bound path would satisfy every
    recomposition-shaped assertion but fails here.
    """
    catalogued = _write_script(tmp_path / "flag.py", _IMPOSTOR)
    approved = _write_script(tmp_path / "tool.py", _APPROVED)
    catalogue, (tool,) = _catalogue_for(str(catalogued))

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, str(approved)),
    ):
        result = _run_capturing(sh.make(tool, catalogue=catalogue)("--flag"))

    assert result.stdout == f"{_APPROVED}:{approved}\n", (
        f"the child must see the bound path as argv[0], got {result.stdout!r}"
    )
    assert result.argv == ("--flag",), (
        f"arguments are unchanged by binding, got {result.argv!r}"
    )


def test_an_unbound_program_runs_under_its_catalogued_name() -> None:
    """Without a binding the catalogued executable runs and no path is reported.

    The negative control for the tests above: it separates "the binding changed
    what ran" from "something unexpected always runs". ``resolved_path``
    staying ``None`` is what tells a consumer nothing was substituted.
    """
    catalogue, python = python_catalogue()

    with scoped(ScopeConfig(allowlist=frozenset([python]))):
        result = sh.make(python, catalogue=catalogue)("-c", "pass").run_sync(
            output=RunOutputOptions(capture=True, echo=False),
        )

    assert result.exit_code == 0, f"the unbound run must succeed, got {result}"
    assert result.resolved_path is None, (
        f"an unbound program must report no resolved path, got {result.resolved_path!r}"
    )


def test_a_resolver_runs_once_per_execution_not_once_per_event(
    tmp_path: Path,
) -> None:
    """A lazy binding is evaluated exactly once, however many events follow.

    A stage emits several lifecycle events, and resolving inside the event
    emitter rather than once when the observation was built would call a
    resolver several times per run. That matters in practice: a resolver may
    probe the filesystem or consult a toolchain that is still being installed,
    so calling it per event is both wasteful and observably wrong.
    """
    approved = _write_script(tmp_path / "lazy.py", _APPROVED)
    catalogue, (tool,) = _catalogue_for("lazy-tool")
    calls: list[int] = []

    def resolver() -> str:
        """Record each evaluation, so a per-event call becomes visible."""
        calls.append(len(calls))
        return str(approved)

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, resolver),
    ):
        result = _run_capturing(sh.make(tool, catalogue=catalogue)())

    assert result.resolved_path == str(approved), (
        f"the resolver's value must reach the result, got {result.resolved_path!r}"
    )
    assert len(calls) == 1, (
        f"a resolver must run exactly once per execution, ran {len(calls)} times"
    )


def test_a_pipeline_resolves_each_stage_independently(tmp_path: Path) -> None:
    """Two stages of one pipeline carry their own bound executables.

    A pipeline resolves every stage from the same observation builder, so a
    regression that reused the first stage's executable, or that resolved only
    the stage it happened to spawn first, shows up as stages reporting one
    another's paths. Each stage runs a distinct script and both are asserted,
    so the two cannot be confused.
    """
    producer_path = _write_script(tmp_path / "producer.py", _APPROVED)
    consumer_path = _write_script(tmp_path / "consumer.py", _APPROVED)
    catalogue, (producer_prog, consumer_prog) = _catalogue_for(
        "stage-producer",
        "stage-consumer",
    )
    pipeline = (
        sh.make(producer_prog, catalogue=catalogue)()
        | sh.make(
            consumer_prog,
            catalogue=catalogue,
        )()
    )

    with (
        scoped(ScopeConfig(allowlist=frozenset([producer_prog, consumer_prog]))),
        bind_executable(producer_prog, str(producer_path)),
        bind_executable(consumer_prog, str(consumer_path)),
    ):
        result = _run_pipeline_capturing(pipeline)

    paths = [stage.resolved_path for stage in result.stages]
    assert paths == [str(producer_path), str(consumer_path)], (
        f"each stage must resolve its own binding, got {paths!r}"
    )


def test_an_unbound_pipeline_reports_no_resolved_path() -> None:
    """An unbound pipeline reports ``None`` on every stage.

    The counterpart to the per-stage test: it separates "each stage resolved
    its own binding" from "stages report paths unconditionally". Without it, a
    regression that invented a path for unbound stages would pass.
    """
    catalogue, python = python_catalogue()
    python_cmd = sh.make(python, catalogue=catalogue)("-c", "pass")
    pipeline = python_cmd | python_cmd

    with scoped(ScopeConfig(allowlist=frozenset([python]))):
        result = _run_pipeline_capturing(pipeline)

    paths = [stage.resolved_path for stage in result.stages]
    assert paths == [None, None], (
        f"an unbound pipeline must report no paths, got {paths!r}"
    )


def test_a_bound_but_unlisted_program_is_refused_without_resolving(
    tmp_path: Path,
) -> None:
    """The allowlist still decides first; a binding cannot admit a program.

    The bound program is known to the catalogue but absent from the allowlist.
    Two things must hold together: the run is refused, and the resolver never
    ran. A binding that could admit an unlisted program would be a hole in the
    allowlist, and resolving before enforcing would run side-effecting
    resolvers for commands that never execute.
    """
    approved = _write_script(tmp_path / "unlisted.py", _APPROVED)
    # Both programs are catalogued, and only ``ECHO`` is allowlisted, so the
    # refusal under test is the allowlist's rather than the catalogue's.
    catalogue, (echo_prog, unlisted) = _catalogue_for(ECHO, LS)
    calls: list[int] = []

    def resolver() -> str:
        """Record evaluation, so an unpermitted run would leave a trace."""
        calls.append(len(calls))
        return str(approved)

    with (
        scoped(ScopeConfig(allowlist=frozenset([echo_prog]))),
        bind_executable(unlisted, resolver),
        pytest.raises(ForbiddenProgramError, match=str(LS)),
    ):
        _run_capturing(sh.make(unlisted, catalogue=catalogue)())

    # ``len(...) == 0`` rather than a falsy check: the list must be empty
    # because the resolver never ran, and the message should say how many
    # times it did run when that assertion fails.
    assert len(calls) == 0, (
        f"a resolver must not run for a program the allowlist refuses, ran "
        f"{len(calls)} times"
    )


def test_a_pipeline_refused_at_a_later_stage_resolves_no_stage(
    tmp_path: Path,
) -> None:
    """A forbidden later stage stops the pipeline before any stage resolves.

    A pipeline enforces every stage before it resolves any, and that ordering
    is what this pins: the earlier stage *is* allowed, so a regression that
    enforced and resolved stage by stage would run its resolver and only then
    discover the refusal. That is not merely wasteful. A resolver may probe the
    filesystem or consult a toolchain still being installed, and doing so for a
    pipeline the caller is told was refused leaves side effects behind for a
    run that never happened.

    Both stages carry counting resolvers, so the assertion covers the allowed
    stage as well as the refused one. The point is not only that the refusal
    happens, but that nothing was resolved on the way to it.
    """
    approved = _write_script(tmp_path / "refused-pipeline.py", _APPROVED)
    catalogue, (first_prog, later_prog) = _catalogue_for(
        "refusal-producer",
        "refusal-consumer",
    )
    first_calls: list[int] = []
    later_calls: list[int] = []

    def first_resolver() -> str:
        """Record evaluation of the stage the allowlist *does* permit."""
        first_calls.append(len(first_calls))
        return str(approved)

    def later_resolver() -> str:
        """Record evaluation of the stage under refusal."""
        later_calls.append(len(later_calls))
        return str(approved)

    pipeline = (
        sh.make(first_prog, catalogue=catalogue)()
        | sh.make(later_prog, catalogue=catalogue)()
    )

    with (
        # Only the first stage is allowlisted, so the refusal under test is
        # the later stage's and not a catalogue miss.
        scoped(ScopeConfig(allowlist=frozenset([first_prog]))),
        bind_executable(first_prog, first_resolver),
        bind_executable(later_prog, later_resolver),
        pytest.raises(ForbiddenProgramError, match=str(later_prog)),
    ):
        _run_pipeline_capturing(pipeline)

    assert len(first_calls) == 0, (
        f"the permitted stage's resolver must not run for a refused pipeline, "
        f"ran {len(first_calls)} times"
    )
    assert len(later_calls) == 0, (
        f"the refused stage's resolver must not run, ran {len(later_calls)} times"
    )


def test_a_permitted_pipeline_resolves_each_stage_exactly_once(
    tmp_path: Path,
) -> None:
    """An allowed pipeline runs each stage's resolver once and reports its path.

    This is the positive control for the pair above. That test asserts zero
    calls on both stages of a refused pipeline; zero is also what a broken
    counter, an unregistered binding, or a resolver that never runs at all
    would produce, so on its own it cannot distinguish "correctly withheld"
    from "never wired up".

    Running the same shape with every stage permitted supplies the other half:
    the resolvers *do* run, exactly once each, and each stage reports the path
    its own resolver returned. Both halves together make the ordering claim
    falsifiable in each direction — a regression that resolved before
    enforcing fails the refusal test, and one that resolved per event rather
    than per stage fails this one.
    """
    producer_path = _write_script(tmp_path / "counted-producer.py", _APPROVED)
    consumer_path = _write_script(tmp_path / "counted-consumer.py", _APPROVED)
    catalogue, (producer_prog, consumer_prog) = _catalogue_for(
        "counted-producer",
        "counted-consumer",
    )
    producer_calls: list[int] = []
    consumer_calls: list[int] = []

    def producer_resolver() -> str:
        """Record evaluation of the producing stage."""
        producer_calls.append(len(producer_calls))
        return str(producer_path)

    def consumer_resolver() -> str:
        """Record evaluation of the consuming stage."""
        consumer_calls.append(len(consumer_calls))
        return str(consumer_path)

    pipeline = (
        sh.make(producer_prog, catalogue=catalogue)()
        | sh.make(consumer_prog, catalogue=catalogue)()
    )

    with (
        scoped(
            ScopeConfig(allowlist=frozenset([producer_prog, consumer_prog])),
        ),
        bind_executable(producer_prog, producer_resolver),
        bind_executable(consumer_prog, consumer_resolver),
    ):
        result = _run_pipeline_capturing(pipeline)

    assert len(producer_calls) == 1, (
        f"the producing stage must resolve exactly once, ran "
        f"{len(producer_calls)} times"
    )
    assert len(consumer_calls) == 1, (
        f"the consuming stage must resolve exactly once, ran "
        f"{len(consumer_calls)} times"
    )
    paths = [stage.resolved_path for stage in result.stages]
    assert paths == [str(producer_path), str(consumer_path)], (
        f"each stage must report the path its own resolver returned, got {paths!r}"
    )


def test_the_bound_path_reaches_every_event_of_the_execution(tmp_path: Path) -> None:
    """Every lifecycle event of a bound run reports the same executable.

    ``plan``, ``start`` and ``exit`` describe one execution, so a consumer
    correlating them needs the executable to agree across all three. A
    regression that annotated one phase but not the others would still pass the
    ``CommandResult`` assertions above.

    Each event is checked individually rather than collapsed into a mapping
    keyed by phase: a mapping keeps only the *last* event per phase, so an
    earlier event carrying a wrong path would be silently overwritten by a
    later correct one. The logical ``Program`` is asserted on every event too,
    because a binding that replaced the identity with the executable string
    would leave the path assertions passing while breaking the correlation the
    path exists to support.
    """
    approved = _write_script(tmp_path / "events.py", _APPROVED)
    catalogue, (tool,) = _catalogue_for("event-tool")
    events: list[ExecEvent] = []

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, str(approved)),
        sh.observe(events.append),
    ):
        _run_capturing(sh.make(tool, catalogue=catalogue)())

    assert events, "a bound run must still emit its observation events"
    phases = {event.phase for event in events}
    assert {"plan", "start", "exit"} <= phases, (
        f"the run must emit its full lifecycle, got {sorted(phases)}"
    )
    for event in events:
        assert event.resolved_path == str(approved), (
            f"the {event.phase} event must report the bound executable, "
            f"got {event.resolved_path!r}"
        )
        assert event.program == tool, (
            f"the {event.phase} event must keep the logical program, "
            f"got {event.program!r}"
        )


def test_resolution_reads_the_scoped_context_not_the_module_global(
    tmp_path: Path,
) -> None:
    """The innermost scope's binding is what an execution resolves.

    Nesting is what makes the binding scoped rather than global. An inner
    registration for the same program overrides the outer one for the inner
    block only, and the outer binding is restored afterwards. Both halves are
    asserted, so a regression that leaked the inner binding outward, or that
    ignored the inner one entirely, is caught.
    """
    outer = _write_script(tmp_path / "outer.py", "OUTER")
    inner = _write_script(tmp_path / "inner.py", "INNER")
    catalogue, (tool,) = _catalogue_for("scoped-tool")
    builder = sh.make(tool, catalogue=catalogue)

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, str(outer)),
    ):
        outer_result = _run_capturing(builder())
        with bind_executable(tool, str(inner)):
            inner_result = _run_capturing(builder())
        restored_result = _run_capturing(builder())

    assert outer_result.resolved_path == str(outer), "the outer scope binds first"
    assert inner_result.resolved_path == str(inner), (
        "the inner scope must override the outer binding"
    )
    assert restored_result.resolved_path == str(outer), (
        f"leaving the inner scope must restore the outer binding, got "
        f"{restored_result.resolved_path!r}"
    )


def test_the_context_reports_the_binding_the_execution_uses(tmp_path: Path) -> None:
    """``resolve_executable`` on the active context agrees with the run.

    The public query and the spawn-time resolution are two roads to one answer.
    A caller inspecting the context before running a command must see the same
    executable the child will start; a divergence would mean the inspectable
    answer is not the executed one.
    """
    approved = _write_script(tmp_path / "query.py", _APPROVED)
    catalogue, (tool,) = _catalogue_for("query-tool")

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, str(approved)),
    ):
        queried = current_context().resolve_executable(tool, cwd=None)
        result = _run_capturing(sh.make(tool, catalogue=catalogue)())

    assert queried == str(approved), (
        f"the context must report the binding, got {queried!r}"
    )
    assert result.resolved_path == queried, (
        "the executed path must equal the one the context reports"
    )


def test_a_handle_reports_the_binding_its_child_ran_under(tmp_path: Path) -> None:
    """``start()`` reports the bound executable exactly as ``run()`` does.

    The handle spawns through the same resolution the blocking path uses, so a
    result that reported no resolved path would say the program ran under its
    catalogued name while the caller's own binding is what chose the file. The
    two routes are asserted against each other rather than only against the
    literal path, because the claim under test is that they agree — a
    divergence is the regression, and only the pair can show one.
    """
    approved = _write_script(tmp_path / "handle.py", _APPROVED)
    catalogue, (tool,) = _catalogue_for("handle-tool")
    builder = sh.make(tool, catalogue=catalogue)

    async def orchestrate() -> CommandResult:
        """Run the bound program through a handle and await its outcome."""
        handle = await builder().start(output=RunOutputOptions(capture=True))
        return await handle.wait()

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, str(approved)),
    ):
        # The blocking route is run outside the loop: ``run_sync`` drives its
        # own, so calling it from inside this one would nest two event loops.
        blocking_result = _run_capturing(builder())
        handle_result = asyncio.run(orchestrate())

    assert blocking_result.resolved_path == str(approved), (
        "the blocking route must report the bound executable, got "
        f"{blocking_result.resolved_path!r}"
    )
    assert handle_result.resolved_path == blocking_result.resolved_path, (
        "a handle must report the binding its child ran under, not one route's "
        f"account of it: run() said {blocking_result.resolved_path!r} while "
        f"start() said {handle_result.resolved_path!r}"
    )
    assert handle_result.stdout == blocking_result.stdout, (
        "both routes must run the same file, got "
        f"{handle_result.stdout!r} against {blocking_result.stdout!r}"
    )
