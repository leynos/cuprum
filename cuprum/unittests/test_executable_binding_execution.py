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
"""

from __future__ import annotations

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


def _write_script(path: Path, marker: str) -> Path:
    """Write an executable script reporting ``marker`` and its own ``argv[0]``.

    Returns
    -------
    Path
        The script's path, so assertions can name it without re-deriving it.
    """
    path.write_text(_SCRIPT.format(marker=marker), encoding="utf-8")
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
        documentation_locations=("docs/users-guide.md#executable-bindings",),
    )
    return catalogue, entries


def _run_capturing(cmd: SafeCmd, *, cwd: Path | None = None) -> CommandResult:
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


def test_the_bound_path_reaches_every_event_of_the_execution(tmp_path: Path) -> None:
    """Every lifecycle event of a bound run reports the same executable.

    ``plan``, ``start`` and ``exit`` describe one execution, so a consumer
    correlating them needs the executable to agree across all three. A
    regression that annotated one phase but not the others would still pass the
    ``CommandResult`` assertions above.
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
    reported = {event.phase: event.resolved_path for event in events}
    assert {"plan", "start", "exit"} <= set(reported), (
        f"the run must emit its full lifecycle, got {sorted(reported)}"
    )
    assert all(path == str(approved) for path in reported.values()), (
        f"every phase must report the bound executable, got {reported!r}"
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


def test_a_relative_binding_resolves_against_the_execution_cwd(
    tmp_path: Path,
) -> None:
    """A relative binding is anchored at the directory the child runs in.

    The execution's working directory is what a relative bound path means, so
    the path a consumer is shown must be the same file the child ran. Anchoring
    at the parent's directory instead would resolve to a file that does not
    exist, or to a different one.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    script = _write_script(workdir / "relative.py", _APPROVED)
    catalogue, (tool,) = _catalogue_for("relative-tool")

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, f"./{script.name}", allow_relative=True),
    ):
        result = _run_capturing(sh.make(tool, catalogue=catalogue)(), cwd=workdir)

    assert result.resolved_path == str(script), (
        f"a relative binding must anchor at the execution cwd, got "
        f"{result.resolved_path!r}"
    )
    assert result.stdout == f"{_APPROVED}:{script}\n", (
        f"the anchored script must be the one that ran, got {result.stdout!r}"
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
