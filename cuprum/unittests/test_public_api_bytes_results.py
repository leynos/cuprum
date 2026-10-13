"""Public-API pins for the byte-exact result mode.

Split from ``test_public_api.py`` when issue #444's byte-exact additions took
that module over CodeScene's 600-line file ceiling and its cohesion score to
7.55. The tests here are the byte-mode half of a larger public-surface
contract: which result classes are reachable from which facade, and what
annotations those classes and entry points present to a static checker.

The distinction the mode exists to draw is *static* — ``BytesCommandResult``
does not subclass ``CommandResult`` — so these checks assert on annotations
that ``typing.get_type_hints`` resolves and on ``typing.assert_type`` calls
read by a checker, not on values at run time. A record whose ``stdout``
annotation drifted back to ``str | None`` would still construct and compare;
only these checks would notice.
"""

from __future__ import annotations

import inspect
import typing as typ

import cuprum as c
from cuprum import sh


def test_bytes_result_classes_are_exported_from_their_definition_site() -> None:
    """The byte-exact result classes are public from both facades.

    Pinned by identity against the module that defines them, so a dropped
    re-export or one re-pointed at a second definition fails here rather than
    in a caller's import.
    """
    from cuprum.sh import results

    assert c.BytesCommandResult is results.BytesCommandResult, (
        "cuprum.BytesCommandResult must be cuprum.sh.results' record"
    )
    assert c.BytesPipelineResult is results.BytesPipelineResult, (
        "cuprum.BytesPipelineResult must be cuprum.sh.results' record"
    )
    assert sh.BytesCommandResult is results.BytesCommandResult, (
        "cuprum.sh.BytesCommandResult must be cuprum.sh.results' record"
    )
    assert sh.BytesPipelineResult is results.BytesPipelineResult, (
        "cuprum.sh.BytesPipelineResult must be cuprum.sh.results' record"
    )


def test_bytes_result_type_hints_carry_bytes_not_text() -> None:
    """The byte-exact records declare bytes, and their helpers return bytes.

    This is the distinction the mode exists to draw, so it is asserted on the
    annotations a type checker reads rather than only on values at run time:
    a record whose ``stdout`` annotation drifted back to ``str | None`` would
    still construct and compare, and only this check would notice.
    """
    command_hints = typ.get_type_hints(c.BytesCommandResult)
    assert command_hints["stdout"] == bytes | None, (
        f"BytesCommandResult.stdout must be bytes | None, got "
        f"{command_hints['stdout']!r}"
    )
    assert command_hints["stderr"] == bytes | None, (
        f"BytesCommandResult.stderr must be bytes | None, got "
        f"{command_hints['stderr']!r}"
    )
    # The text-mode record keeps its own annotation: the two classes draw the
    # distinction, and widening the binary one must not have widened this.
    assert typ.get_type_hints(sh.CommandResult)["stdout"] == str | None, (
        f"CommandResult.stdout must stay str | None, got "
        f"{typ.get_type_hints(sh.CommandResult)['stdout']!r}"
    )

    pipeline_hints = typ.get_type_hints(sh.BytesPipelineResult)
    assert pipeline_hints["stages"] == tuple[sh.BytesCommandResult, ...], (
        f"BytesPipelineResult.stages must be a tuple of byte-exact stages, got "
        f"{pipeline_hints['stages']!r}"
    )
    # Read through ``fget``: the annotations belong to the accessor, and a
    # property object itself carries none for ``get_type_hints`` to resolve.
    stdout_getter = sh.BytesPipelineResult.stdout.fget
    assert stdout_getter is not None, "the stdout property must have a getter"
    assert typ.get_type_hints(stdout_getter)["return"] == bytes | None, (
        "BytesPipelineResult.stdout must return bytes | None"
    )
    final_getter = sh.BytesPipelineResult.final.fget
    assert final_getter is not None, "the final property must have a getter"
    assert typ.get_type_hints(final_getter)["return"] is sh.BytesCommandResult, (
        "final must return the byte-exact stage class"
    )


async def _assert_text_results_are_text(
    command: sh.SafeCmd,
    pipeline: sh.Pipeline,
) -> None:
    """Pin what the text-mode entry points hand a static checker.

    Never awaited: the body exists to be type-checked, which is what makes
    the assertion meaningful. ``assert_type`` fails a ``ty check`` run rather
    than a test run, and it fires on the *value* each entry point produces —
    the distinction a caller actually relies on when it reads ``.stdout``.
    """
    typ.assert_type(await command.run(), sh.CommandResult)
    typ.assert_type(command.run_sync(), sh.CommandResult)
    typ.assert_type(await pipeline.run(), sh.PipelineResult)
    typ.assert_type(pipeline.run_sync(), sh.PipelineResult)


async def _assert_bytes_results_are_bytes(
    command: sh.SafeCmd,
    pipeline: sh.Pipeline,
) -> None:
    """Pin the same distinction for the byte-exact entry points."""
    typ.assert_type(await command.run_bytes(), sh.BytesCommandResult)
    typ.assert_type(command.run_bytes_sync(), sh.BytesCommandResult)
    typ.assert_type(await pipeline.run_bytes(), sh.BytesPipelineResult)
    typ.assert_type(pipeline.run_bytes_sync(), sh.BytesPipelineResult)


def test_bytes_entry_points_are_statically_typed_apart_from_text() -> None:
    """``assert_type`` pins what each entry point hands a static checker.

    ``BytesCommandResult`` does not subclass ``CommandResult``, so the two
    entry points are distinguishable before the code runs — which is the whole
    point of a typed mode. The checks live in the two coroutines above, whose
    bodies ``ty check`` reads without this test ever awaiting them. Because
    nothing here awaits them, a check that was deleted would fail no test at
    all, so this reads each coroutine's source and asserts every entry point's
    ``assert_type`` is still in the body it belongs to. A count alone would let
    a swap pass: four ``assert_type`` calls naming the wrong classes read the
    same as the right ones until a checker actually compares the names.
    """
    expected_pairs = {
        _assert_text_results_are_text: {
            ("await command.run()", "sh.CommandResult"),
            ("command.run_sync()", "sh.CommandResult"),
            ("await pipeline.run()", "sh.PipelineResult"),
            ("pipeline.run_sync()", "sh.PipelineResult"),
        },
        _assert_bytes_results_are_bytes: {
            ("await command.run_bytes()", "sh.BytesCommandResult"),
            ("command.run_bytes_sync()", "sh.BytesCommandResult"),
            ("await pipeline.run_bytes()", "sh.BytesPipelineResult"),
            ("pipeline.run_bytes_sync()", "sh.BytesPipelineResult"),
        },
    }

    for checker, expected in expected_pairs.items():
        source = inspect.getsource(checker)
        for expr, annotation in expected:
            fragment = f"assert_type({expr}, {annotation})"
            assert fragment in source, (
                f"{checker.__name__} must still assert_type {expr!r} as "
                f"{annotation}, but {fragment!r} is not in its body"
            )
