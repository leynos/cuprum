"""Construction rules for the ``max_line_chars`` option.

The option's behaviour under a real drain and under pipeline resolution is
pinned elsewhere; this module owns only what the caller can supply: which
values are refused, which are accepted, what the default is, and that the new
keyword-only field leaves the existing positional order intact.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import RunOutputOptions
from cuprum._constants import DEFAULT_MAX_LINE_CHARS

if typ.TYPE_CHECKING:
    from cuprum.lines import LineEvent


@pytest.mark.parametrize("invalid_bound", [0, -1, True, 1.5, "8"])
def test_run_output_options_rejects_invalid_line_bound(
    invalid_bound: object,
) -> None:
    """Invalid ``max_line_chars`` values are rejected at construction.

    ``True`` is the interesting case: as an ``int`` subclass it would pass a
    bare ``isinstance(value, int)`` check, yet a bool is not the size the
    caller meant, so it is refused exactly as ``max_echo_line_bytes`` refuses
    it. A float and a numeric string are refused for the same reason: the
    field types a decoded-character count, not something coercible to one.
    """
    with pytest.raises(ValueError, match="max_line_chars must be a positive integer"):
        RunOutputOptions(
            max_line_chars=typ.cast("int | None", invalid_bound),
        )


@pytest.mark.parametrize(
    "bound",
    [pytest.param(None, id="unbounded"), pytest.param(1, id="one-char")],
)
def test_run_output_options_accepts_valid_line_bounds(bound: int | None) -> None:
    """``None`` disables the bound and any positive int sets it."""
    options = RunOutputOptions(max_line_chars=bound)

    assert options.max_line_chars == bound


def test_run_output_options_default_line_bound_matches_constant() -> None:
    """The default line bound is the shared 64 Ki decoded-character constant."""
    assert RunOutputOptions().max_line_chars == DEFAULT_MAX_LINE_CHARS
    assert DEFAULT_MAX_LINE_CHARS == 64 * 1024


def test_max_line_chars_leaves_on_line_in_its_positional_slot() -> None:
    """Adding the bound did not shift ``on_line`` or any later field.

    The field is keyword-only precisely so an existing call that passes
    ``on_line`` positionally keeps binding it to the same parameter; the new
    bound then keeps its default instead of capturing that callable.
    """
    events: list[LineEvent] = []

    def hook(event: LineEvent) -> None:
        """Record the observed event; the test only needs the identity."""
        events.append(event)

    # Positional order is the contract under test, not an API flag a keyword
    # would express more clearly.
    options = RunOutputOptions(True, False, None, None, None, hook)  # ruff: ignore[boolean-positional-value-in-call] - positional order under test

    assert options.on_line is hook, (
        "the sixth positional argument must still bind on_line"
    )
    assert options.max_line_chars == DEFAULT_MAX_LINE_CHARS, (
        "the keyword-only bound must not consume a positional argument"
    )
