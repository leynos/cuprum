"""Property-based tests for stream content preservation across chunk boundaries.

These tests use Hypothesis to generate random payloads and random chunk
boundaries. The pipeline writes bytes in configured chunk sizes and verifies
the downstream stage receives identical bytes by comparing hexadecimal output.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum._streams import _READ_SIZE
from tests.helpers.parity import (
    build_property_pipeline_case,
    chunk_sizes_from_cut_points,
    run_parity_pipeline,
)

_GENERAL_MAX_EXAMPLES = 12
_BOUNDARY_MAX_EXAMPLES = 6
_BOUNDARY_DELTA = 512
_BOUNDARY_MIN_SIZE = _READ_SIZE - _BOUNDARY_DELTA
_BOUNDARY_MAX_SIZE = _READ_SIZE + _BOUNDARY_DELTA


@st.composite
def payload_and_chunk_sizes(
    draw: st.DrawFn,
    *,
    min_size: int,
    max_size: int,
    max_cuts: int,
) -> tuple[bytes, tuple[int, ...]]:
    """Generate random payload bytes and random chunk partitions.

    Parameters
    ----------
    draw : st.DrawFn
        Hypothesis draw function used by ``@st.composite``.
    min_size : int
        Minimum payload size in bytes.
    max_size : int
        Maximum payload size in bytes.
    max_cuts : int
        Maximum number of cut points used to partition the payload.

    Returns
    -------
    tuple[bytes, tuple[int, ...]]
        Random payload bytes and a tuple of chunk sizes.
    """
    payload = draw(st.binary(min_size=min_size, max_size=max_size))
    payload_size = len(payload)

    if payload_size <= 1 or max_cuts == 0:
        return payload, (payload_size,) if payload_size == 1 else ()

    cut_ceiling = min(max_cuts, payload_size - 1)
    cut_points = draw(
        st.lists(
            st.integers(min_value=1, max_value=payload_size - 1),
            min_size=1,
            max_size=cut_ceiling,
            unique=True,
        ).map(lambda points: tuple(sorted(points))),
    )
    return payload, chunk_sizes_from_cut_points(payload_size, cut_points)


@settings(
    max_examples=_GENERAL_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(case=payload_and_chunk_sizes(min_size=0, max_size=1024, max_cuts=8))
def test_stream_preserves_random_payloads_across_random_chunk_boundaries(
    stream_backend: str,
    case: tuple[bytes, tuple[int, ...]],
) -> None:
    """Property: random payload bytes are preserved for random chunk boundaries.

    Parameters
    ----------
    stream_backend : str
        Active stream backend from fixture parametrisation.
    case : tuple[bytes, tuple[int, ...]]
        Random payload and random chunk partition.
    """
    payload, chunk_sizes = case
    property_case = build_property_pipeline_case(payload, chunk_sizes)
    result = run_parity_pipeline(property_case.pipeline, property_case.allowlist)

    assert result.ok is True
    assert result.stdout == property_case.expected_hex
    assert len(result.stages) == 2
    assert all(stage.exit_code == 0 for stage in result.stages)


@settings(
    max_examples=_BOUNDARY_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    case=payload_and_chunk_sizes(
        min_size=_BOUNDARY_MIN_SIZE,
        max_size=_BOUNDARY_MAX_SIZE,
        max_cuts=16,
    ),
)
def test_stream_preserves_random_payloads_around_python_read_size_boundary(
    stream_backend: str,
    case: tuple[bytes, tuple[int, ...]],
) -> None:
    """Property: random payloads around 4 KB are preserved across chunk splits.

    Parameters
    ----------
    stream_backend : str
        Active stream backend from fixture parametrisation.
    case : tuple[bytes, tuple[int, ...]]
        Random payload and random chunk partition.
    """
    payload, chunk_sizes = case
    property_case = build_property_pipeline_case(payload, chunk_sizes)
    result = run_parity_pipeline(property_case.pipeline, property_case.allowlist)

    assert result.ok is True
    assert result.stdout == property_case.expected_hex
    assert len(result.stages) == 2
    assert all(stage.exit_code == 0 for stage in result.stages)
