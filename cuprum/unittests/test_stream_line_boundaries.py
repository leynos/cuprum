"""Regression tests for Python's complete decoded-line boundary set."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum._stream_line_boundaries import (
    _LineAccumulator,
    _split_complete_lines,
    _strip_line_ending,
)

_BOUNDARIES = (
    "\r\n",
    "\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
)

_PROPERTY_SETTINGS = settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.mark.parametrize("boundary", _BOUNDARIES)
def test_complete_python_line_boundary_is_emitted_without_its_separator(
    boundary: str,
) -> None:
    """Every ``str.splitlines()`` boundary completes one emitted line."""
    lines, remainder = _split_complete_lines(f"line{boundary}")

    assert lines == ["line"], f"{boundary!r} must complete and strip one line"
    assert not remainder, f"{boundary!r} must leave no completed remainder"
    assert _strip_line_ending(f"line{boundary}") == "line", (
        f"{boundary!r} must be stripped from an emitted line"
    )


def test_non_final_carriage_return_remains_the_only_held_boundary() -> None:
    """Only a potentially split CRLF pair remains pending between chunks."""
    lines, remainder = _split_complete_lines("line\r", final=False)

    assert lines == [], "a non-final carriage return may still form CRLF"
    assert remainder == "line\r", "the potential CRLF pair must remain intact"


def _drain(accumulator: _LineAccumulator, chunks: tuple[str, ...]) -> list[str]:
    """Feed every chunk, then finish, as the stream drain does."""
    lines = [line for chunk in chunks for line in accumulator.feed(chunk)]
    trailing = accumulator.finish()
    if trailing is not None:
        lines.append(trailing)
    return lines


@pytest.mark.parametrize("max_line_chars", [1, 2, 3, 10])
def test_accumulator_emits_fragments_no_longer_than_the_bound(
    max_line_chars: int,
) -> None:
    """A newline-free run is split into fragments of exactly the bound."""
    text = "x" * (3 * max_line_chars)

    lines = _drain(_LineAccumulator(max_line_chars), (text,))

    assert all(len(line) <= max_line_chars for line in lines), (
        f"no fragment may exceed {max_line_chars} chars, got {lines!r}"
    )
    assert "".join(lines) == text, "fragments must preserve every character"


def test_accumulator_bounds_a_run_before_its_terminator_arrives() -> None:
    """Fragments are emitted as the bound fills, not only once the line ends."""
    accumulator = _LineAccumulator(4)

    first = accumulator.feed("abcdefgh")
    second = accumulator.feed("ij\n")

    assert first == ["abcd"], (
        f"the first full fragment must arrive with the chunk that filled it, {first!r}"
    )
    assert second == ["efgh", "ij"], (
        f"the remainder must complete on its terminator, got {second!r}"
    )
    assert accumulator.pending_chars == 0, "a completed line retains nothing"


def test_accumulator_splits_one_large_chunk_without_recopying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Splitting a large chunk is linear even when the bound is one.

    ``_take`` copies the retained head whenever it keeps a partial part, so
    taking one fragment per bound-sized slice would copy the rest of the chunk
    once per fragment: quadratic in the chunk for a small bound. The spy counts
    ``_take`` calls, and each call beyond the first re-copies the retained
    remainder. A linear split of one chunk takes nothing during ``feed`` and
    once during ``finish``, so the count is deterministic rather than a
    wall-clock measurement.
    """
    from cuprum import _stream_line_boundaries

    copied: list[int] = []
    original = _stream_line_boundaries._LineAccumulator._take

    def counting_take(self: _LineAccumulator, count: int) -> str:
        """Record the characters a take copies, then delegate."""
        copied.append(count)
        return original(self, count)

    monkeypatch.setattr(
        _stream_line_boundaries._LineAccumulator,
        "_take",
        counting_take,
    )
    text = "x" * 4096

    lines = _drain(_LineAccumulator(1), (text,))

    assert "".join(lines) == text, "splitting must preserve every character"
    assert copied == [1], (
        "one chunk must be sliced once by finish(), not once per fragment; "
        f"_take ran {len(copied)} times over {len(text)} characters"
    )


def test_accumulator_treats_a_line_exactly_at_the_bound_as_one_line() -> None:
    """Terminators are not counted, so no empty fragment follows."""
    accumulator = _LineAccumulator(4)

    lines = accumulator.feed("abcd\n")

    assert lines == ["abcd"], f"an exactly-bounded line is one event, got {lines!r}"
    assert accumulator.finish() is None, "a terminated line leaves nothing pending"


@pytest.mark.parametrize("boundary", _BOUNDARIES)
def test_accumulator_handles_every_boundary_char_for_char(
    boundary: str,
) -> None:
    """Every boundary completes a line, whichever chunk it is split across."""
    text = f"a{boundary}b{boundary}"

    lines = _drain(_LineAccumulator(1024), tuple(text))

    assert lines == ["a", "b"], f"{boundary!r} must complete two lines, got {lines!r}"


def test_accumulator_holds_a_lone_carriage_return_until_the_next_chunk() -> None:
    """A trailing CR is pending, and only a following LF is absorbed."""
    accumulator = _LineAccumulator(1024)

    assert accumulator.feed("one\r") == [], "a lone CR may still pair with LF"
    assert accumulator.feed("two\r\nthree") == ["one", "two"], (
        "a CR not followed by LF is its own terminator; a CRLF is one"
    )
    assert accumulator.finish() == "three", "the unterminated tail is the last line"


def test_accumulator_finishes_the_remainder_without_a_terminator() -> None:
    """``finish`` reports the unterminated tail and nothing else."""
    accumulator = _LineAccumulator(1024)

    assert accumulator.feed("tail") == [], "an unterminated line is not complete"
    assert accumulator.finish() == "tail", "the tail must be reported at end of input"
    assert accumulator.finish() is None, "finishing twice must not repeat the tail"


def test_accumulator_finish_after_a_boundary_reports_nothing() -> None:
    """A stream ending on a terminator has no trailing line to report."""
    accumulator = _LineAccumulator(1024)

    assert accumulator.feed("line\n") == ["line"], "the terminated line must complete"
    assert accumulator.finish() is None, "nothing may be invented after a terminator"


def test_accumulator_finish_after_a_held_carriage_return_reports_the_line() -> None:
    """A held CR becomes a terminator once input ends, closing that line."""
    accumulator = _LineAccumulator(1024)

    assert accumulator.feed("line\r") == [], "the CR is held while input continues"
    assert accumulator.finish() == "line", (
        "at end of input the held CR terminates the line before it"
    )


def test_accumulator_with_no_bound_emits_the_whole_line() -> None:
    """``None`` restores unbounded retention."""
    text = "x" * 5000

    lines = _drain(_LineAccumulator(None), (text,))

    assert lines == [text], "``None`` must not fragment a long line"


def test_accumulator_keeps_content_count_independent_of_boundaries() -> None:
    """Pending characters count content only, never terminators."""
    accumulator = _LineAccumulator(4)

    accumulator.feed("ab\r\n")

    assert accumulator.pending_chars == 0, "terminators are not retained content"


@_PROPERTY_SETTINGS
@given(
    text=st.text(max_size=60),
    cuts=st.lists(st.integers(min_value=0, max_value=60), max_size=6),
    max_line_chars=st.sampled_from([1, 2, 3, 7, 16, None]),
)
def test_accumulator_matches_splitlines_and_stays_within_the_bound(
    text: str,
    cuts: list[int],
    max_line_chars: int | None,
) -> None:
    """Property: fragmentation is chunk-independent and the bound always holds.

    The joined output must equal the text with its terminators removed, which
    is what ``str.splitlines()`` exposes, and no single event may exceed the
    bound. Feeding the text as one chunk or several must not change the result,
    because the accumulator's state is the only thing that may carry across.
    """
    bounds = sorted({cut for cut in cuts if 0 < cut < len(text)})
    starts = [0, *bounds]
    ends = [*bounds, len(text)]
    chunks = tuple(text[start:end] for start, end in zip(starts, ends, strict=True))
    expected_content = "".join(text.splitlines())

    whole = _drain(_LineAccumulator(max_line_chars), (text,))
    split = _drain(_LineAccumulator(max_line_chars), chunks)

    assert "".join(split) == expected_content, (
        f"fragments must rebuild the terminator-free text for {text!r} in {chunks!r}"
    )
    assert split == whole, (
        f"chunk boundaries must not change output for {text!r} in {chunks!r}"
    )
    if max_line_chars is None:
        assert split == text.splitlines(), (
            f"unbounded output must match str.splitlines() for {text!r}"
        )
    else:
        assert all(len(line) <= max_line_chars for line in split), (
            f"no event may exceed {max_line_chars} chars for {text!r}, got {split!r}"
        )
