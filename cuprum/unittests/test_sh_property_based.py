"""Property-based tests for argv construction and pipeline composition.

These tests pin down the algebraic behaviour of ``sh.build_argv`` (the
pure helper behind ``sh.make`` builders) and ``Pipeline.concat`` over
generated inputs rather than fixed examples.

The invariants checked here are:

- ``build_argv``: positional arguments are stringified in order and
  precede keyword flags; keyword flags preserve insertion order and
  normalize underscores in keys to hyphens; ``None`` and ``bytes`` are
  rejected with ``TypeError`` in both positional and keyword positions.
- ``make``: builders produce ``SafeCmd`` instances whose argv agrees
  with ``build_argv`` and whose program/project come from the catalogue
  entry; unknown programs are rejected with ``UnknownProgramError``;
  a keyword naming an execution option (an ``ExecutionContext`` field
  or a ``run_sync`` parameter) is rejected with ``TypeError`` instead.
- ``Pipeline.concat``: composition is associative, loses no stages,
  preserves left-to-right order, and agrees with the ``|`` operator.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from cuprum.catalogue import DEFAULT_CATALOGUE, ProjectSettings, UnknownProgramError
from cuprum.program import Program
from cuprum.sh import Pipeline, SafeCmd, build_argv, make
from cuprum.sh.factory import _RESERVED_OPTIONS

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Bounded alphabets keep examples small and shrinkable while still
# covering multi-character tokens and key normalization.
_TEXT_VALUES = st.text(alphabet="abc xyz0-_./", max_size=8)
_ARG_VALUES = st.one_of(
    _TEXT_VALUES,
    st.integers(min_value=-1000, max_value=1000),
    st.booleans(),
    st.builds(Path, st.text(alphabet="abc/", min_size=1, max_size=8)),
)
_KWARG_KEYS = st.text(alphabet="abcd_", min_size=1, max_size=8)
_ARGS = st.lists(_ARG_VALUES, max_size=6)
_KWARGS = st.dictionaries(_KWARG_KEYS, _ARG_VALUES, max_size=4)
# Bytes are rejected outright, so the property ranges over arbitrary
# payloads -- including non-UTF-8 -- rather than over curated examples.
_BYTE_VALUES = st.binary(max_size=8)

_TEST_PROJECT = ProjectSettings(
    name="property-tests",
    programs=(),
    documentation_locations=("https://example.test/property-tests",),
    noise_rules=(),
)


def _cmd(name: str) -> SafeCmd:
    """Build a lightweight ``SafeCmd`` without a catalogue lookup."""
    return SafeCmd(program=Program(name), argv=(), project=_TEST_PROJECT)


_STAGE_NAMES = st.text(alphabet="abc-", min_size=1, max_size=6)
_CMDS = st.builds(_cmd, _STAGE_NAMES)


@settings(max_examples=200)
@given(args=_ARGS, kwargs=_KWARGS)
def test_build_argv_orders_and_normalizes(
    args: list[str | int | bool | Path],
    kwargs: dict[str, str | int | bool | Path],
) -> None:
    """Positionals precede flags; keys normalize underscores to hyphens."""
    argv = build_argv(*args, **kwargs)
    expected_positional = tuple(str(arg) for arg in args)
    expected_flags = tuple(
        f"--{key.replace('_', '-')}={value!s}" for key, value in kwargs.items()
    )
    assert argv == expected_positional + expected_flags, (
        "argv must be positionals in order followed by flags in order"
    )


@settings(max_examples=100)
@given(args=_ARGS, kwargs=_KWARGS, data=st.data())
def test_build_argv_rejects_none_anywhere(
    args: list[str | int | bool | Path],
    kwargs: dict[str, str | int | bool | Path],
    data: st.DataObject,
) -> None:
    """``None`` raises TypeError in any positional or keyword position."""
    # Deliberately defeat static typing: the property under test is the
    # runtime rejection of None, which the annotations forbid.
    poisoned = typ.cast("str", None)
    position = data.draw(
        st.integers(min_value=0, max_value=len(args)),
        label="insertion position",
    )
    positional = [*args[:position], poisoned, *args[position:]]
    with pytest.raises(TypeError, match="None is not a valid argv element"):
        build_argv(*positional, **kwargs)
    key = data.draw(_KWARG_KEYS, label="poisoned key")
    with pytest.raises(TypeError, match="None is not a valid argv element"):
        build_argv(*args, **{**kwargs, key: poisoned})


@settings(max_examples=100)
@given(args=_ARGS, kwargs=_KWARGS, payload=_BYTE_VALUES, data=st.data())
def test_build_argv_rejects_bytes_anywhere(
    args: list[str | int | bool | Path],
    kwargs: dict[str, str | int | bool | Path],
    payload: bytes,
    data: st.DataObject,
) -> None:
    """``bytes`` raises TypeError in any positional or keyword position."""
    # Deliberately defeat static typing: the property under test is the
    # runtime rejection of bytes, which the annotations forbid.
    poisoned = typ.cast("str", payload)
    position = data.draw(
        st.integers(min_value=0, max_value=len(args)),
        label="insertion position",
    )
    positional = [*args[:position], poisoned, *args[position:]]
    with pytest.raises(TypeError, match="bytes is not a valid argv element"):
        build_argv(*positional, **kwargs)
    key = data.draw(_KWARG_KEYS, label="poisoned key")
    with pytest.raises(TypeError, match="bytes is not a valid argv element"):
        build_argv(*args, **{**kwargs, key: poisoned})


@settings(max_examples=200)
@given(
    program=st.sampled_from(sorted(DEFAULT_CATALOGUE.allowlist)),
    args=_ARGS,
    kwargs=_KWARGS,
)
def test_make_builder_agrees_with_build_argv(
    program: Program,
    args: list[str | int | bool | Path],
    kwargs: dict[str, str | int | bool | Path],
) -> None:
    """Builders attach catalogue metadata and argv equal to build_argv."""
    entry = DEFAULT_CATALOGUE.lookup(program)
    cmd = make(program)(*args, **kwargs)
    assert cmd.program == entry.program, "Builder must keep the catalogue program"
    assert cmd.project is entry.project, "Builder must attach the owning project"
    assert cmd.argv == build_argv(*args, **kwargs), (
        "Builder argv must agree with the pure build_argv helper"
    )
    assert cmd.argv_with_program == (str(program), *cmd.argv), (
        "argv_with_program must prefix the program name"
    )


# Drawn from a literal specimen rather than from the production set: a reserved
# set that lost a name must fail the assertion below, not make the strategy
# unsamplable. The specimens cover both message forms plus the names the
# builder most often sees.
_SPECIMEN_RESERVED = ("cwd", "env", "stdin", "timeout")

# Insertion order decides which reserved name the builder reports, so anything
# drawn before the injected key (including safe keywords) must not shadow it.
_RESERVED_KEYS = st.sampled_from(_SPECIMEN_RESERVED)


@settings(max_examples=200)
@given(
    program=st.sampled_from(sorted(DEFAULT_CATALOGUE.allowlist)),
    args=_ARGS,
    kwargs=_KWARGS.filter(lambda mapping: not mapping.keys() & _RESERVED_OPTIONS),
    reserved=_RESERVED_KEYS,
)
def test_make_rejects_reserved_execution_option_keywords(
    program: Program,
    args: list[str | int | bool | Path],
    kwargs: dict[str, str | int | bool | Path],
    reserved: str,
) -> None:
    """No arrangement of arguments lets an execution option reach the child."""
    assert set(_SPECIMEN_RESERVED) <= _RESERVED_OPTIONS, (
        "the reserved set must still cover every specimen name"
    )
    poisoned = {**kwargs, reserved: "value"}

    with pytest.raises(TypeError) as excinfo:
        make(program)(*args, **poisoned)

    assert str(excinfo.value).startswith(f"{reserved} is an execution option;"), (
        "the reported keyword must be the reserved name the caller supplied"
    )
    assert "run_sync" in str(excinfo.value), (
        "the error must point at the run_sync spelling"
    )


@settings(max_examples=100)
@given(name=_STAGE_NAMES)
def test_make_rejects_programs_outside_catalogue(name: str) -> None:
    """Programs absent from the catalogue are rejected at build time."""
    assume(not DEFAULT_CATALOGUE.is_allowed(name))
    with pytest.raises(UnknownProgramError):
        make(Program(name))


@settings(max_examples=200)
@given(cmds=st.lists(_CMDS, min_size=2, max_size=6))
def test_pipeline_composition_preserves_stages(cmds: list[SafeCmd]) -> None:
    """Folding stages with ``|`` keeps every stage in order."""
    pipeline = cmds[0] | cmds[1]
    for cmd in cmds[2:]:
        pipeline |= cmd
    assert pipeline.parts == tuple(cmds), (
        "Pipeline must contain exactly the composed stages in order"
    )


@settings(max_examples=200)
@given(
    left=st.lists(_CMDS, min_size=1, max_size=4),
    right=st.lists(_CMDS, min_size=1, max_size=4),
)
def test_concat_concatenates_operand_stages(
    left: list[SafeCmd],
    right: list[SafeCmd],
) -> None:
    """``concat`` flattens both operands without losing stages."""
    left_operand = _as_operand(left)
    right_operand = _as_operand(right)
    combined = Pipeline.concat(left_operand, right_operand)
    assert combined.parts == (*left, *right), (
        "concat must append right-hand stages after left-hand stages"
    )
    assert (left_operand | right_operand).parts == combined.parts, (
        "The | operator must agree with Pipeline.concat"
    )


@settings(max_examples=200)
@given(a=_CMDS, b=_CMDS, c=_CMDS)
def test_concat_is_associative(a: SafeCmd, b: SafeCmd, c: SafeCmd) -> None:
    """Grouping does not change the composed stage sequence."""
    left_grouped = Pipeline.concat(Pipeline.concat(a, b), c)
    right_grouped = Pipeline.concat(a, Pipeline.concat(b, c))
    assert left_grouped.parts == right_grouped.parts == (a, b, c), (
        "concat must be associative over stage sequences"
    )


def _as_operand(cmds: cabc.Sequence[SafeCmd]) -> SafeCmd | Pipeline:
    """Wrap one command as itself and several as a Pipeline."""
    if len(cmds) == 1:
        return cmds[0]
    return Pipeline(tuple(cmds))
