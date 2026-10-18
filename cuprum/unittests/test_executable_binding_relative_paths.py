"""Relative bindings composed with a relative execution directory.

A relative bound path means *relative to the directory the child runs in*, and
that directory may itself be given relative to the process's own. Those two
facts compose into the trap these examples pin: composing them naively yields
a relative ``argv[0]``, which the child re-anchors against the very directory
it was already anchored to — applying the prefix a second time. The reported
``resolved_path`` would still name the singly-anchored file, so the audit
surface and the child would disagree about which file ran.

Each example therefore plants a *decoy* under the doubled prefix and reads the
child's own report, so the divergence is observable rather than a bare
``FileNotFoundError``. The anchor is resolved at a different call site for
each public entry point — capture, pipeline, and line iteration — because a
change that special-cased one would leave the others latent.

The scripts, catalogue builder, and capturing runners shared with the rest of
the spawn-time binding suite live in ``test_executable_binding_execution``.
"""

from __future__ import annotations

import asyncio
import typing as typ
from pathlib import Path

from cuprum import ScopeConfig, scoped, sh
from cuprum.context.registration import bind_executable
from cuprum.sh import ExecutionContext, RunOutputOptions
from cuprum.unittests.test_executable_binding_execution import (
    _APPROVED,
    _IMPOSTOR,
    _RELAY_SCRIPT,
    _catalogue_for,
    _run_capturing,
    _write_script,
)
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import pytest

    from cuprum.events import ExecEvent
    from cuprum.sh import CommandResult


# A working directory that is *relative* to the process's own rather than an
# absolute one. ``ExecutionContext.cwd`` accepts either, and only the relative
# form exposes a binding anchored once per consumer.
_RELATIVE_CWD = "srv/work"


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


def test_a_relative_execution_cwd_reaches_events_unanchored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Events report the caller's configured ``cwd``, not the anchored form.

    Resolving a relative binding needs the anchored directory — that is the
    whole reason ``_execution_cwd`` exists — but the *event* does not. Its
    ``cwd`` is the configured directory: the value the caller passed,
    represented as a ``Path``, which normalizes lexical detail such as a
    trailing ``/`` or a ``./`` segment. The spawn receives that same directory
    through ``_cwd_arg``, so an event carrying the anchored form makes the
    audit surface disagree with both. The regression scope is wider than
    bindings: it changed ``ExecEvent.cwd`` for every relative-directory run,
    bound or not, so the second phase below runs an *unbound* command and still
    requires the configured directory.

    The assertion is ``==`` against ``Path(_RELATIVE_CWD)`` rather than a
    truthiness check or a containment test, because those pass for both forms
    and pin nothing. Equality against a ``Path`` is also what keeps the *type*
    honest: ``"srv/work" == Path("srv/work")`` is ``False``, so emitting the
    string the caller typed fails here rather than passing on a coincidence of
    the value.
    """
    monkeypatch.chdir(tmp_path)
    workdir = tmp_path / _RELATIVE_CWD
    workdir.mkdir(parents=True)
    script = _write_script(workdir / "tool.py", _APPROVED)
    catalogue, (tool,) = _catalogue_for("relative-cwd-event-tool")
    events: list[ExecEvent] = []

    with (
        scoped(ScopeConfig(allowlist=frozenset([tool]))),
        bind_executable(tool, f"./{script.name}", allow_relative=True),
        sh.observe(events.append),
    ):
        bound = _run_capturing(
            sh.make(tool, catalogue=catalogue)(),
            cwd=_RELATIVE_CWD,
        )

    assert bound.resolved_path == str(script), (
        f"resolution still needs the anchored directory, got {bound.resolved_path!r}"
    )
    assert events, "a bound run must still emit its observation events"
    for event in events:
        assert event.cwd == Path(_RELATIVE_CWD), (
            f"the {event.phase} event must report the configured cwd, got {event.cwd!r}"
        )

    # Unbound, in the same relative directory: the anchoring was never about
    # bindings, so the configured directory must reach an unbound event too. The
    # catalogue's own executable is used because no binding is in play to say
    # which file should run, and this phase has to spawn *something*.
    unbound_catalogue, python = python_catalogue()
    unobserved: list[ExecEvent] = []
    with (
        scoped(ScopeConfig(allowlist=frozenset([python]))),
        sh.observe(unobserved.append),
    ):
        _run_capturing(
            sh.make(python, catalogue=unbound_catalogue)("-c", "pass"),
            cwd=_RELATIVE_CWD,
        )

    assert unobserved, "the unbound phase must emit its observation events"
    for event in unobserved:
        assert event.cwd == Path(_RELATIVE_CWD), (
            f"an unbound run's {event.phase} event must report the configured cwd "
            f"too, got {event.cwd!r}"
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
    consumer = _write_script(
        workdir / "bin" / "consumer.py",
        _APPROVED,
        template=_RELAY_SCRIPT,
    )
    doubled = workdir / "srv" / "work" / "bin"
    doubled.mkdir(parents=True)
    _write_script(doubled / "producer.py", _IMPOSTOR)
    _write_script(doubled / "consumer.py", _IMPOSTOR, template=_RELAY_SCRIPT)
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

    # Both reports are asserted, in order, against the file each stage was
    # bound to. The consumer forwards the producer's line, so a producer that
    # ran its decoy is visible here even though a pipeline's captured stdout
    # belongs to the last stage alone.
    assert result.stdout == (f"{_APPROVED}:{producer}\n{_APPROVED}:{consumer}\n"), (
        f"each stage must run the file it was bound to; the pipeline reported "
        f"{result.stdout!r}"
    )
    paths = [stage.resolved_path for stage in result.stages]
    assert paths == [str(producer), str(consumer)], (
        f"each stage must report its singly-anchored path, got {paths!r}"
    )
    # Both reports above come from a child that exited zero. Asserting the
    # codes makes "the stage ran the file it was bound to" a claim about a
    # successful execution rather than one about output a failing child could
    # equally have produced. The comprehension is repeated rather than bound to
    # a name because this function already sits at the module's local-variable
    # limit; the message is only built when the assertion fails.
    assert [stage.exit_code for stage in result.stages] == [0, 0], (
        f"both stages must run to completion, got exit codes "
        f"{[stage.exit_code for stage in result.stages]!r}"
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
