"""Property tests for the GitHub Actions workflow-command escaping helpers.

Escaping is the adapter's only defence against a value from the run being read
as workflow syntax, so the properties are the contract: a data value must
never introduce a line break, and a property value must not carry the
delimiters that separate the property list. A round-trip check pins the
encodings as a bijection rather than merely as substitutions that happen to
hold for the example values.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from cuprum.sinks.github_actions import _escape_data, _escape_property


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

    assert "\n" not in escaped
    assert "\r" not in escaped
    assert len(escaped) >= len(value), "escaping never shrinks a value"


@given(value=st.text())
def test_escape_data_round_trips(*, value: str) -> None:
    """Decoding the escaped form recovers the original data."""
    assert _unescape_data(_escape_data(value)) == value


@given(value=st.text())
def test_escape_property_masks_the_property_delimiters(*, value: str) -> None:
    """A property value carries no bare ``:`` or ``,`` and stays single-line."""
    escaped = _escape_property(value)

    assert ":" not in escaped
    assert "," not in escaped
    assert "\n" not in escaped
    assert escaped == (_escape_data(value).replace(":", "%3A").replace(",", "%2C")), (
        "property escaping is data escaping plus the two delimiters"
    )
