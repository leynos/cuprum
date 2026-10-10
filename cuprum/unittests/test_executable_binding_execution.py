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

# A working directory that is *relative* to the process's own rather than an
# absolute one. ``ExecutionContext.cwd`` accepts either, and only the relative
# spelling exposes a binding anchored once per consumer.
_RELATIVE_CWD = "srv/work"

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


def test_a_relative_execution_cwd_anchors_a_relative_binding_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative ``cwd`` anchors the binding once, not once per consumer.

    The execution's working directory may be given *relative* to the process's
    own, and a relative bound path is anchored at it. Those two facts compose
    into a trap: composing them naively yields a relative ``argv[0]``, which the
    child then resolves against the very directory it was already anchored to —
    applying the same prefix a second time. ``resolved_path`` would still report
    the singly-anchored path, so the audit surface and the child would disagree
    about which file ran.

    The decoy makes that divergence observable rather than a bare
    ``FileNotFoundError``: a second anchoring runs the decoy and prints its
    marker, while the reported path still names the intended script. The
    ``monkeypatch.chdir`` is what makes the prefix relative at all — an absolute
    ``cwd`` composes to an absolute ``argv[0]`` and hides the defect.
    """
    monkeypatch.chdir(tmp_path)
    workdir = tmp_path / "srv" / "work"
    (workdir / "bin").mkdir(parents=True)
    script = _write_script(workdir / "bin" / "tool.py", _APPROVED)
    # The file a doubly-anchored spawn would actually execute: the child's own
    # directory, joined with the relative ``cwd`` it was handed, joined again
    # with the relative binding.
    doubled = workdir / "srv" / "work" / "bin"
    doubled.mkdir(parents=True)
    _write_script(doubled / "tool.py", _IMPOSTOR)
    catalogue, (tool,) = _catalogue_for("relative-cwd-tool")

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, "bin/tool.py", allow_relative=True),
    ):
        result = _run_capturing(
            sh.make(tool, catalogue=catalogue)(),
            cwd=_RELATIVE_CWD,
        )

    # The child's own report comes first: it is the witness that the file which
    # ran is the file the audit surface names. Under a double anchoring the
    # reported path is still the singly-anchored one, so asserting it first
    # would leave the divergence itself unobserved here.
    assert result.stdout == f"{_APPROVED}:{script}\n", (
        f"a relative execution cwd must anchor exactly once; the child reported "
        f"{result.stdout!r} while {result.resolved_path!r} was claimed"
    )
    assert result.resolved_path == str(script), (
        f"a relative execution cwd must report the path it ran, got "
        f"{result.resolved_path!r}"
    )


def test_a_pipeline_anchors_a_relative_cwd_once_for_every_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative ``cwd`` anchors each pipeline stage exactly once.

    The single-command path and the pipeline path build their observations in
    different modules, so repairing one leaves the other latent. Both must
    anchor: a pipeline is the harder case, because every stage composes the
    same relative directory with the same relative binding, and a stage whose
    composition stayed relative would run the doubly-anchored decoy while its
    ``resolved_path`` named the intended file.

    Each stage gets its own decoy under the doubled prefix, so a regression that
    anchored only some stages is visible per stage rather than as a single pass
    or fail.
    """
    monkeypatch.chdir(tmp_path)
    workdir = tmp_path / "srv" / "work"
    (workdir / "bin").mkdir(parents=True)
    producer = _write_script(workdir / "bin" / "producer.py", _APPROVED)
    consumer = _write_script(workdir / "bin" / "consumer.py", _APPROVED)
    doubled = workdir / "srv" / "work" / "bin"
    doubled.mkdir(parents=True)
    _write_script(doubled / "producer.py", _IMPOSTOR)
    _write_script(doubled / "consumer.py", _IMPOSTOR)
    catalogue, (producer_prog, consumer_prog) = _catalogue_for(
        "relative-pipeline-producer",
        "relative-pipeline-consumer",
    )
    pipeline = (
        sh.make(producer_prog, catalogue=catalogue)()
        | sh.make(consumer_prog, catalogue=catalogue)()
    )

    with (
        scoped(ScopeConfig(allowlist=frozenset([producer_prog, consumer_prog]))),
        bind_executable(producer_prog, "bin/producer.py", allow_relative=True),
        bind_executable(consumer_prog, "bin/consumer.py", allow_relative=True),
    ):
        result = pipeline.run_sync(
            output=RunOutputOptions(capture=True, echo=False),
            context=ExecutionContext(cwd=_RELATIVE_CWD),
        )

    paths = [stage.resolved_path for stage in result.stages]
    assert paths == [str(producer), str(consumer)], (
        f"each stage must report its singly-anchored path, got {paths!r}"
    )
    assert _IMPOSTOR not in (result.stdout or ""), (
        f"no stage may run its doubly-anchored decoy, got {result.stdout!r}"
    )


def test_line_iteration_anchors_a_relative_cwd_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line-iteration path anchors the same way the capture path does.

    ``SafeCmd.lines`` builds its observation through the same preparation as
    ``run_sync``, but it is a separate public entry point with its own call
    site, so a future change that special-cased one would leave the other
    wrong. The witness is the same decoy: a doubly-anchored spawn iterates the
    decoy's output, and the marker says which file produced it.
    """
    monkeypatch.chdir(tmp_path)
    workdir = tmp_path / "srv" / "work"
    (workdir / "bin").mkdir(parents=True)
    script = _write_script(workdir / "bin" / "lined.py", _APPROVED)
    doubled = workdir / "srv" / "work" / "bin"
    doubled.mkdir(parents=True)
    _write_script(doubled / "lined.py", _IMPOSTOR)
    catalogue, (tool,) = _catalogue_for("relative-line-tool")

    async def collect() -> tuple[list[str], CommandResult | None]:
        command = sh.make(tool, catalogue=catalogue)()
        async with command.lines(
            output=RunOutputOptions(capture=True, echo=False),
            context=ExecutionContext(cwd=_RELATIVE_CWD),
        ) as stream:
            lines = [event.text async for event in stream]
            return lines, stream.result

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, "bin/lined.py", allow_relative=True),
    ):
        lines, result = asyncio.run(collect())

    assert lines == [f"{_APPROVED}:{script}"], (
        f"the iterated line must come from the singly-anchored file, got {lines!r}"
    )
    assert result is not None, "line iteration must expose the final result"
    assert result.resolved_path == str(script), (
        f"the streamed run must report the path it ran, got {result.resolved_path!r}"
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
