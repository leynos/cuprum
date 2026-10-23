"""Property tests for the GitHub Actions workflow-command escaping helpers.

Escaping is the adapter's only defence against a value from the run being read
as workflow syntax, so the properties are the contract: a data value must
never introduce a line break, and a property value must not carry the
delimiters that separate the property list. A round-trip check pins the
encodings as a bijection rather than merely as substitutions that happen to
hold for the example values.
"""

from __future__ import annotations

import typing as typ

from hypothesis import given
from hypothesis import strategies as st

from cuprum.sinks import SessionOutcome, TerminalOutcome
from cuprum.sinks.github_actions import _escape_data, _escape_property
from cuprum.unittests.test_sinks_github_actions import _open_gha_session

if typ.TYPE_CHECKING:
    from cuprum.sinks._github_actions_options import _GitHubActionsSinkOptionsKwargs


def _assert_session_commands(
    argv: tuple[str, ...],
    *,
    group_title: str,
    annotation_title: str,
    sink_options: _GitHubActionsSinkOptionsKwargs | None = None,
) -> None:
    """Assert the adapter emits the expected escaped group and annotation."""
    session, buffer = _open_gha_session(argv, sink_options=sink_options)
    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))
    output = buffer.getvalue()
    expected_group = f"::group::{_escape_data(group_title)}"
    assert output.split("\n", maxsplit=1)[0] == expected_group, (
        f"expected group command {expected_group!r}; got {output!r}"
    )
    expected_annotation = (
        f"::error title={_escape_property(annotation_title)}::exit_nonzero\n"
    )
    assert expected_annotation in output, (
        f"expected annotation command {expected_annotation!r}; got {output!r}"
    )


def _unescape_data(value: str) -> str:
    """Decode data-position escaping, for round-trip checks only.

    The production code has no decoder: escaping is one-way. Decoding
    ``%0A``/``%0D`` before ``%25`` is what makes the inverse total, since
    escaping a literal ``%`` yields ``%25`` and never a bare code.

    Returns
    -------
    str
        The value the escaped form encodes.
    """
    return value.replace("%0A", "\n").replace("%0D", "\r").replace("%25", "%")


@given(value=st.text())
def test_escape_data_keeps_the_value_on_one_line(*, value: str) -> None:
    """Escaped data carries no bare carriage return or newline."""
    escaped = _escape_data(value)

    assert "\n" not in escaped, f"escaped data must stay on one line; found {escaped!r}"
    assert "\r" not in escaped, (
        f"escaped data must carry no bare carriage return; found {escaped!r}"
    )
    assert len(escaped) >= len(value), "escaping never shrinks a value"


@given(value=st.text())
def test_escape_data_round_trips(*, value: str) -> None:
    """Decoding the escaped form recovers the original data."""
    escaped = _escape_data(value)

    assert _unescape_data(escaped) == value, (
        f"decoding the escaped form {escaped!r} must recover {value!r}"
    )


@given(value=st.text())
def test_escape_property_masks_the_property_delimiters(*, value: str) -> None:
    """A property value carries no bare ``:`` or ``,`` and stays single-line."""
    escaped = _escape_property(value)

    assert ":" not in escaped, (
        f"escaped property data must carry no bare ':' delimiter; found {escaped!r}"
    )
    assert "," not in escaped, (
        f"escaped property data must carry no bare ',' delimiter; found {escaped!r}"
    )
    assert "\n" not in escaped, (
        f"escaped property data must stay on one line; found {escaped!r}"
    )
    assert escaped == (_escape_data(value).replace(":", "%3A").replace(",", "%2C")), (
        "property escaping is data escaping plus the two delimiters"
    )


@given(argv=st.lists(st.text()).map(tuple), title=st.text())
def test_session_titles_keep_argv_out_of_default_titles(
    *,
    argv: tuple[str, ...],
    title: str,
) -> None:
    """Only explicit options may put argv into emitted workflow commands."""
    label = "project: program"
    _assert_session_commands(
        argv,
        group_title=label,
        annotation_title=label,
    )

    expected_argv_title = " ".join(argv) if argv else label
    _assert_session_commands(
        argv,
        group_title=expected_argv_title,
        annotation_title=label,
        sink_options={"argv_title": True},
    )

    _assert_session_commands(
        argv,
        group_title=title,
        annotation_title=title or label,
        sink_options={"title": title, "argv_title": True},
    )
