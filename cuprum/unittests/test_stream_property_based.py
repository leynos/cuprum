"""Property-based tests for stream content preservation across chunk boundaries.

These tests use Hypothesis to generate random payloads and random chunk
boundaries. The pipeline writes bytes in configured chunk sizes and verifies
the downstream stage receives identical bytes by comparing hexadecimal output.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from cuprum._streams import _consume_stream, _StreamConfig
from cuprum._streams_pump import _READ_SIZE
from tests.helpers.parity import (
    PropertyPipelineCase,
    build_property_pipeline_case,
    chunk_sizes_from_cut_points,
    run_parity_pipeline,
)

if typ.TYPE_CHECKING:
    from cuprum.sh import PipelineResult

_GENERAL_MAX_EXAMPLES = 12
_BOUNDARY_MAX_EXAMPLES = 6
_BOUNDARY_DELTA = 512
_BOUNDARY_MIN_SIZE = max(0, _READ_SIZE - _BOUNDARY_DELTA)
_BOUNDARY_MAX_SIZE = _READ_SIZE + _BOUNDARY_DELTA
_ARGV_PAYLOAD_CEILING = 98_300
_IN_PROCESS_READ_SIZES = (1, 2, 7, _READ_SIZE, _READ_SIZE * 2)


@st.composite
def _payload_and_chunk_sizes(
    draw: st.DrawFn,
    *,
    min_size: int,
    max_size: int,
    max_cuts: int,
) -> tuple[bytes, tuple[int, ...]]:
    """Generate random payload bytes and chunk sizes for tests."""
    payload = draw(st.binary(min_size=min_size, max_size=max_size))
    payload_size = len(payload)

    if payload_size <= 1:
        return payload, (payload_size,) if payload_size > 0 else ()

    if max_cuts == 0:
        return payload, (payload_size,)

    cut_ceiling = min(max_cuts, payload_size - 1)
    cut_points: tuple[int, ...] = draw(
        st.lists(
            st.integers(min_value=1, max_value=payload_size - 1),
            min_size=1,
            max_size=cut_ceiling,
            unique=True,
        ).map(lambda points: tuple(sorted(points))),
    )
    return payload, chunk_sizes_from_cut_points(payload_size, cut_points)


def _assert_pipeline_result(
    result: PipelineResult,
    property_case: PropertyPipelineCase,
) -> None:
    """Assert shared success invariants for property-based stream pipelines."""
    assert result.ok is True, f"expected result.ok to be True but got {result.ok}"
    assert result.stdout == property_case.expected_hex, (
        f"stdout mismatch: expected {property_case.expected_hex!r} but got "
        f"{result.stdout!r}"
    )
    assert len(result.stages) == 2, f"expected 2 stages but got {len(result.stages)}"
    assert all(stage.exit_code == 0 for stage in result.stages), (
        "one or more stages had non-zero exit_code: "
        f"{[stage.exit_code for stage in result.stages]}"
    )


async def _consume_at_read_size(
    payload: bytes,
    *,
    read_size: int,
    lines: list[str] | None = None,
) -> str | None:
    """Consume payload through a real reader using an injected read size."""
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    config = _StreamConfig(
        capture_output=True,
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="replace",
    )
    return await _consume_stream(
        reader,
        config,
        on_line=None if lines is None else lines.append,
        read_size=read_size,
    )


@settings(max_examples=16, deadline=None, derandomize=True)
@example(payload=b"abcdef", read_size=3)
@given(
    payload=st.binary(min_size=0, max_size=8192),
    read_size=st.sampled_from(_IN_PROCESS_READ_SIZES),
)
def test_capture_matches_whole_payload_decode_at_each_read_size(
    payload: bytes,
    read_size: int,
) -> None:
    """Property: capture matches the whole-payload decode oracle."""
    captured = asyncio.run(_consume_at_read_size(payload, read_size=read_size))

    assert captured == payload.decode("utf-8", errors="replace"), (
        "capture must match the independent whole-payload decoder for "
        f"read_size={read_size}, payload={payload!r}, captured={captured!r}"
    )


@st.composite
def _line_boundary_case(draw: st.DrawFn) -> tuple[int, str]:
    """Construct text whose line ending lands beside a read boundary."""
    read_size = draw(st.sampled_from(_IN_PROCESS_READ_SIZES[:4]))
    offset = draw(st.sampled_from((-1, 0, 1)))
    ending = draw(st.sampled_from(("\r\n", "\n", "\r")))
    ending_end = max(len(ending), read_size + offset)
    prefix = "x" * (ending_end - len(ending))
    suffix = draw(st.text(alphabet="ab\u2603", min_size=0, max_size=12))
    return read_size, f"{prefix}{ending}{suffix}"


@settings(max_examples=18, deadline=None, derandomize=True)
@example(case=(1, "\u2603\r\nx"))
@given(case=_line_boundary_case())
def test_line_emission_matches_splitlines_at_read_boundaries(
    case: tuple[int, str],
) -> None:
    """Property: emitted lines match the decoded-text line oracle."""
    read_size, text = case
    lines: list[str] = []
    captured = asyncio.run(
        _consume_at_read_size(text.encode(), read_size=read_size, lines=lines),
    )

    assert captured == text, (
        "capture must preserve constructed text at "
        f"read_size={read_size}, got {captured!r}"
    )
    assert lines == text.splitlines(), (
        "emitted lines must match the decoded-text splitlines oracle for "
        f"read_size={read_size}, text={text!r}, lines={lines!r}"
    )
    assert all("\n" not in line and "\r" not in line for line in lines), (
        f"emitted lines must not retain CR or LF endings, got {lines!r}"
    )


def test_boundary_window_fits_argv_budget() -> None:
    """The largest boundary payload stays below the measured argv ceiling."""
    assert _BOUNDARY_MAX_SIZE <= _ARGV_PAYLOAD_CEILING, (
        "the parity payload must fit in one argv entry; update the measured "
        f"ceiling before raising _READ_SIZE, got {_BOUNDARY_MAX_SIZE}"
    )


@settings(
    max_examples=_GENERAL_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(case=_payload_and_chunk_sizes(min_size=0, max_size=1024, max_cuts=8))
def test_stream_preserves_random_payloads_across_random_chunk_boundaries(
    stream_backend: str,
    case: tuple[bytes, tuple[int, ...]],
) -> None:
    """Property: random payload bytes are preserved for random chunk boundaries.

    Parameters
    ----------
    stream_backend : str
        Active stream backend from fixture parameterization.
    case : tuple[bytes, tuple[int, ...]]
        Random payload and random chunk partition.
    """
    payload, chunk_sizes = case
    property_case = build_property_pipeline_case(payload, chunk_sizes)
    result = run_parity_pipeline(property_case.pipeline, property_case.allowlist)

    _assert_pipeline_result(result, property_case)


@settings(
    max_examples=_BOUNDARY_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    case=_payload_and_chunk_sizes(
        min_size=_BOUNDARY_MIN_SIZE,
        max_size=_BOUNDARY_MAX_SIZE,
        max_cuts=16,
    ),
)
def test_stream_preserves_random_payloads_around_python_read_size_boundary(
    stream_backend: str,
    case: tuple[bytes, tuple[int, ...]],
) -> None:
    """Property: payloads around _READ_SIZE (+/- _BOUNDARY_DELTA) are preserved.

    Parameters
    ----------
    stream_backend : str
        Active stream backend from fixture parameterization.
    case : tuple[bytes, tuple[int, ...]]
        Random payload and random chunk partition.
    """
    payload, chunk_sizes = case
    assert len(chunk_sizes) > 1, (
        f"boundary cases must cross an upstream chunk boundary, got {chunk_sizes!r}"
    )
    property_case = build_property_pipeline_case(payload, chunk_sizes)
    result = run_parity_pipeline(property_case.pipeline, property_case.allowlist)

    _assert_pipeline_result(result, property_case)
