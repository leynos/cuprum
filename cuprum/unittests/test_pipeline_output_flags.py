"""Construction and resolution tests for output convenience flags."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from cuprum._constants import DEFAULT_ECHO_MAX_LINE_BYTES
from cuprum.sh import (
    IOOptions,
    RunOutputOptions,
    _DeprecatedOutputFlags,
    _resolve_pipeline_output,
)
from cuprum.sinks import GitHubActionsSink
from cuprum.unittests._sink_test_support import RecordingSink

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@pytest.mark.parametrize("invalid_bound", [0, -1])
def test_run_output_options_rejects_non_positive_echo_bound(
    invalid_bound: int,
) -> None:
    """Zero and negative integer echo bounds raise ``ValueError``."""
    with pytest.raises(
        ValueError,
        match="max_echo_line_bytes must be a positive integer or None",
    ):
        RunOutputOptions(max_echo_line_bytes=invalid_bound)


@pytest.mark.parametrize("invalid_type", [True, 1.5, "8"])
def test_run_output_options_rejects_non_integer_echo_bound(
    invalid_type: object,
) -> None:
    """Non-integer echo bounds raise ``TypeError`` with field context."""
    with pytest.raises(
        TypeError,
        match="max_echo_line_bytes must be a positive integer or None",
    ):
        RunOutputOptions(max_echo_line_bytes=typ.cast("int | None", invalid_type))


def test_group_and_annotate_failure_default_off() -> None:
    """Both convenience flags default to ``False`` and synthesize nothing."""
    options = RunOutputOptions()

    assert options.group is False, "group must default to False"
    assert options.annotate_failure is False, "annotate_failure must default to False"
    assert options.sink is None, (
        "the defaults must not synthesize a sink; a run with no flags and no "
        "sink must keep its plain destinations"
    )


def _options_with_invalid_group(value: object) -> RunOutputOptions:
    """Construct options with an invalid group value for validation tests."""
    return RunOutputOptions(group=typ.cast("bool", value))


def _options_with_invalid_annotation(value: object) -> RunOutputOptions:
    """Construct options with an invalid annotation value for validation tests."""
    return RunOutputOptions(annotate_failure=typ.cast("bool", value))


@pytest.mark.parametrize(
    ("flag", "construct"),
    [
        pytest.param("group", _options_with_invalid_group, id="group"),
        pytest.param(
            "annotate_failure", _options_with_invalid_annotation, id="annotate"
        ),
    ],
)
@pytest.mark.parametrize("invalid", [1, 0, "yes", None, 1.0])
def test_run_output_options_rejects_non_bool_flags(
    flag: str,
    construct: cabc.Callable[[object], RunOutputOptions],
    invalid: object,
) -> None:
    """A non-``bool`` flag value is rejected at construction.

    ``1`` is the interesting case: it is truthy, so a lenient implementation
    would silently frame where the caller wrote something that is not the
    documented ``bool``. ``max_echo_line_bytes`` already draws this line.
    """
    with pytest.raises(ValueError, match=f"{flag} must be a bool"):
        construct(invalid)


def test_flags_synthesize_github_actions_sink() -> None:
    """Each flag maps onto the adapter toggle of the same effect.

    The synthesized adapter carries no ``force``, so it stays inactive outside
    GitHub Actions exactly as an explicitly passed sink would.
    """
    both = RunOutputOptions(group=True, annotate_failure=True)
    group_only = RunOutputOptions(group=True)
    annotate_only = RunOutputOptions(annotate_failure=True)

    assert isinstance(both.sink, GitHubActionsSink), (
        f"the flags must synthesize a GitHubActionsSink; got {both.sink!r}"
    )
    assert (both.sink.emit_group, both.sink.emit_annotation) == (True, True), (
        "both flags must map onto both adapter toggles"
    )
    assert isinstance(group_only.sink, GitHubActionsSink), (
        "group=True must synthesize a GitHub Actions adapter"
    )
    assert (
        group_only.sink.emit_group,
        group_only.sink.emit_annotation,
    ) == (True, False), "group=True alone must enable only the group toggle"
    assert isinstance(annotate_only.sink, GitHubActionsSink), (
        "annotate_failure=True must synthesize a GitHub Actions adapter"
    )
    assert (
        annotate_only.sink.emit_group,
        annotate_only.sink.emit_annotation,
    ) == (False, True), (
        "annotate_failure=True alone must enable only the annotation toggle"
    )
    assert both.sink.force is False, (
        "the synthesized sink must stay environment-gated, not forced"
    )


def test_explicit_sink_wins_over_flags() -> None:
    """An explicit sink is kept verbatim; the flags become no-ops."""
    recording = RecordingSink()
    options = RunOutputOptions(
        group=True,
        annotate_failure=True,
        sink=recording,
    )

    assert options.sink is recording, (
        f"an explicit sink must win over the flags; got {options.sink!r}"
    )
    assert not isinstance(options.sink, GitHubActionsSink), (
        "no adapter may be synthesized when a sink was supplied"
    )
    assert options.group is True, "the flags are still recorded as requested"


def test_replace_rebuilds_generated_sink_when_flags_change() -> None:
    """Frozen copies refresh generated sinks but retain replacement sinks."""
    original = RunOutputOptions(group=True)
    changed_flags = dc.replace(
        original,
        group=False,
        annotate_failure=True,
    )
    disabled_flags = dc.replace(original, group=False)
    explicit_sink = GitHubActionsSink(
        emit_group=False,
        emit_annotation=True,
    )
    replaced_sink = dc.replace(original, sink=explicit_sink)

    assert isinstance(changed_flags.sink, GitHubActionsSink), (
        "changing generated options must keep the GitHub Actions adapter"
    )
    assert (
        changed_flags.sink.emit_group,
        changed_flags.sink.emit_annotation,
    ) == (False, True), "the generated adapter must follow changed flag values"
    assert disabled_flags.sink is None, (
        "disabling all generated flags must remove the synthesized adapter"
    )
    assert replaced_sink.sink is explicit_sink, (
        "an explicitly replaced adapter must retain precedence over the flags"
    )
    assert (
        replaced_sink.sink.emit_group,
        replaced_sink.sink.emit_annotation,
    ) == (False, True), (
        "an explicit GitHubActionsSink must not be mistaken for a generated one"
    )


def test_reusing_generated_sink_as_explicit_preserves_it() -> None:
    """A sink from another options object is still an explicit sink."""
    generated = RunOutputOptions(group=True).sink
    assert isinstance(generated, GitHubActionsSink), (
        "group=True must provide a sink that can be reused explicitly"
    )

    reused = RunOutputOptions(sink=generated)

    assert reused.sink is generated, (
        "a supplied sink must be retained even when both flags are false"
    )
    assert (reused.group, reused.annotate_failure) == (False, False), (
        "reusing the sink must not enable either convenience flag"
    )


def test_io_options_inherits_the_flags() -> None:
    """The deprecated alias resolves the flags through the same initializer."""
    with pytest.warns(DeprecationWarning, match="IOOptions is deprecated"):
        options = IOOptions(group=True, annotate_failure=True)

    assert isinstance(options.sink, GitHubActionsSink), (
        "IOOptions must inherit the synthesis from RunOutputOptions"
    )
    assert (options.group, options.annotate_failure) == (True, True), (
        "IOOptions must inherit both flags unchanged"
    )


@pytest.mark.parametrize(
    ("group", "annotate_failure"),
    [
        pytest.param(True, False, id="group"),
        pytest.param(False, True, id="annotate"),
        pytest.param(True, True, id="both"),
    ],
)
def test_resolve_pipeline_output_preserves_flags(
    group: bool,
    annotate_failure: bool,
) -> None:
    """Pipeline resolution carries both flags onto the resolved options.

    Resolution returns the caller's own options object unchanged when they
    supplied one, so the flags — and the sink the initializer synthesized from
    them — survive onto the pipeline run.
    """
    supplied = RunOutputOptions(group=group, annotate_failure=annotate_failure)

    resolved = _resolve_pipeline_output(supplied, _DeprecatedOutputFlags())

    assert resolved is supplied, (
        "pipeline resolution must forward the caller's own options object"
    )
    assert resolved.group is group, "group must survive pipeline resolution"
    assert resolved.annotate_failure is annotate_failure, (
        "annotate_failure must survive pipeline resolution"
    )
    assert isinstance(resolved.sink, GitHubActionsSink), (
        "the synthesized sink must survive pipeline resolution with the flags"
    )
    assert (
        resolved.sink.emit_group,
        resolved.sink.emit_annotation,
    ) == (group, annotate_failure), (
        "the resolved sink's toggles must still reflect the caller's flags"
    )


def test_flags_are_output_only_not_flat_kwargs() -> None:
    """The new flags travel in ``output=``, never as flat pipeline kwargs.

    ``_resolve_pipeline_output`` admits only the deprecated ``capture`` and
    ``echo`` keys, so ``group=True`` passed directly to ``Pipeline.run`` is
    rejected rather than silently ignored. That keeps the flags on the same
    footing as every other option: one carrier, ``RunOutputOptions``.
    """
    unknown: dict[str, bool] = {"group": True}

    with pytest.raises(TypeError, match="unexpected keyword arguments: group"):
        _resolve_pipeline_output(None, typ.cast("_DeprecatedOutputFlags", unknown))

    # The same key works through the supported carrier.
    assert RunOutputOptions(group=True).group is True, (
        "group must be settable via RunOutputOptions"
    )


def test_run_output_options_default_bound_matches_github_log_limit() -> None:
    """The default bound mirrors the GitHub Actions 64 KiB per-line limit."""
    assert RunOutputOptions().max_echo_line_bytes == DEFAULT_ECHO_MAX_LINE_BYTES
    assert DEFAULT_ECHO_MAX_LINE_BYTES == 64 * 1024
