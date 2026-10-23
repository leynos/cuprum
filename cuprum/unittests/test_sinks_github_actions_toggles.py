"""Independent group and annotation toggle tests for the sink."""

from __future__ import annotations

import typing as typ

import pytest

from cuprum.sinks import GitHubActionsSink, SessionOutcome, TerminalOutcome
from cuprum.sinks.github_actions import _STOP_PREFIX
from cuprum.unittests.test_sinks_github_actions import _open_gha_session

if typ.TYPE_CHECKING:
    import collections.abc as cabc


# ---------------------------------------------------------------------------
# Independent group and annotation toggles
# ---------------------------------------------------------------------------

_FRAMING_COMMANDS = ("::group::", _STOP_PREFIX, "::endgroup::")


def test_emit_group_false_suppresses_group_framing() -> None:
    """A group-less session writes no group, no lease, and no endgroup.

    The lease exists only to shield an open group, so suppressing the group
    must suppress the lease with it: a lease left open with no group would
    suppress workflow-command interpretation for the rest of the step with
    nothing to show for it. The annotation is a separate toggle and still
    fires.
    """
    session, buffer = _open_gha_session(("false",), sink_options={"emit_group": False})

    assert not buffer.getvalue(), (
        f"a group-less session must open nothing; got {buffer.getvalue()!r}"
    )
    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=3))

    value = buffer.getvalue()
    for command in _FRAMING_COMMANDS:
        assert command not in value, (
            f"emit_group=False must suppress {command!r}; got {value!r}"
        )
    assert value == "::error title=project%3A program::exit_nonzero\n", (
        f"the annotation is a separate toggle and must still fire on a "
        f"failure; got {value!r}"
    )


def test_emit_group_false_keeps_the_log_destination() -> None:
    """A group-less session still hands the run its configured destination."""
    session, buffer = _open_gha_session(
        ("echo", "hi"), sink_options={"emit_group": False}
    )

    session.log.write("framed output\n")

    assert buffer.getvalue() == "framed output\n", (
        f"log routing must be unaffected by the group toggle; got {buffer.getvalue()!r}"
    )


@pytest.mark.parametrize("outcome", list(TerminalOutcome))
def test_emit_annotation_false_suppresses_every_error(
    outcome: TerminalOutcome,
) -> None:
    """No outcome annotates once the annotation toggle is off.

    Every member of the closed outcome set is exercised, not just the
    non-zero exit: an implementation that suppressed only the success path
    would still be caught here.
    """
    session, buffer = _open_gha_session(
        ("deploy",), sink_options={"emit_annotation": False}
    )

    session.close(SessionOutcome(outcome, exit_code=3))

    value = buffer.getvalue()
    assert "::error" not in value, (
        f"emit_annotation=False must suppress {outcome} annotations; got {value!r}"
    )
    assert value == (
        f"::group::project: program\n::stop-commands::{session.stop_token}\n"
        f"::{session.stop_token}::\n::endgroup::\n"
    ), f"the group must open and close around a suppressed annotation; got {value!r}"


@pytest.mark.parametrize(
    ("emit_group", "emit_annotation", "expected_commands"),
    [
        pytest.param(True, True, 4, id="both-on"),
        pytest.param(True, False, 3, id="annotation-off"),
        pytest.param(False, True, 1, id="group-off"),
        pytest.param(False, False, 0, id="both-off"),
    ],
)
def test_toggles_compose_independently(
    *,
    emit_group: bool,
    emit_annotation: bool,
    expected_commands: int,
) -> None:
    """The two toggles vary independently over the four combinations.

    A failing outcome is used throughout so the annotation toggle has
    something to suppress, and the count is the number of workflow commands a
    runner would act on: group, lease, lease release, endgroup, annotation.
    """
    session, buffer = _open_gha_session(
        ("false",),
        sink_options={"emit_group": emit_group, "emit_annotation": emit_annotation},
    )
    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    value = buffer.getvalue()
    commands = sum(
        len([line for line in value.splitlines() if line.startswith(prefix)])
        for prefix in (*_FRAMING_COMMANDS, "::error ")
    )
    assert commands == expected_commands, (
        f"emit_group={emit_group}/emit_annotation={emit_annotation} must write "
        f"{expected_commands} workflow command(s); got {commands} in {value!r}"
    )
    assert ("::error " in value) is emit_annotation, (
        f"the annotation presence must follow its own toggle; got {value!r}"
    )


def _sink_with_invalid_group(value: object) -> GitHubActionsSink:
    """Construct a sink with an invalid group value for validation tests."""
    return GitHubActionsSink(emit_group=typ.cast("bool", value))


def _sink_with_invalid_annotation(value: object) -> GitHubActionsSink:
    """Construct a sink with an invalid annotation value for validation tests."""
    return GitHubActionsSink(emit_annotation=typ.cast("bool", value))


@pytest.mark.parametrize(
    ("toggle", "construct"),
    [
        pytest.param("emit_group", _sink_with_invalid_group, id="group"),
        pytest.param("emit_annotation", _sink_with_invalid_annotation, id="annotation"),
    ],
)
@pytest.mark.parametrize("invalid", [1, 0, "yes", None, 1.0])
def test_sink_rejects_non_bool_toggles(
    toggle: str,
    construct: cabc.Callable[[object], GitHubActionsSink],
    invalid: object,
) -> None:
    """A non-``bool`` toggle is rejected where the sink is constructed.

    The toggles gate workflow commands, so a merely truthy value would frame a
    run on the strength of something the caller never documented as a flag.
    ``1`` is the interesting case and is checked alongside values that are
    falsy or not remotely boolean-shaped.
    """
    with pytest.raises(TypeError, match=f"{toggle} must be a bool"):
        construct(invalid)
