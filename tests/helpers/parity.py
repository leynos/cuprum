"""Shared helpers for stream parity and property-based stream tests."""

from __future__ import annotations

import base64
import random
import typing as typ

from cuprum import ECHO, ScopeConfig, scoped
from tests.helpers.catalogue import (
    cat_program,
    combine_programs_into_catalogue,
    python_catalogue,
)

if typ.TYPE_CHECKING:
    from cuprum.catalogue import ProgramCatalogue
    from cuprum.program import Program
    from cuprum.sh import Pipeline, PipelineResult


# Fixed seed ensures deterministic output across runs.
_SEED = 20260217

# Characters from each UTF-8 byte-width category.
_ONE_BYTE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_TWO_BYTE = "\u00e9\u00fc\u00f1\u00e4\u00f6\u00df\u00e8\u00ea"
_THREE_BYTE = "\u2603\u2665\u266b\u2602\u263a\u2605\u2764\u2744"
_FOUR_BYTE = "\U0001f600\U0001f4a9\U0001f680\U0001f308\U0001f525"
_POOLS = (_ONE_BYTE, _TWO_BYTE, _THREE_BYTE, _FOUR_BYTE)


def parity_catalogue() -> tuple[ProgramCatalogue, Program, Program, Program]:
    """Build a catalogue for parity tests.

    Returns
    -------
    tuple[ProgramCatalogue, Program, Program, Program]
        The catalogue, the Python programme, the cat programme, and
        the echo programme.
    """
    _, python_prog = python_catalogue()
    cat_prog = cat_program()
    catalogue = combine_programs_into_catalogue(
        python_prog,
        cat_prog,
        ECHO,
        project_name="stream-parity-tests",
    )
    return catalogue, python_prog, cat_prog, ECHO


def run_parity_pipeline(
    pipeline: Pipeline,
    allowlist: frozenset[Program],
) -> PipelineResult:
    """Execute a pipeline within a scoped allowlist.

    Parameters
    ----------
    pipeline : Pipeline
        The pipeline to execute.
    allowlist : frozenset[Program]
        Programmes to permit during execution.

    Returns
    -------
    PipelineResult
        The result of synchronous pipeline execution.
    """
    with scoped(ScopeConfig(allowlist=allowlist)):
        return pipeline.run_sync()


def utf8_stress_payload(n_chars: int = 32768) -> str:
    """Generate a deterministic multi-byte UTF-8 stress payload.

    Cycles through 1-byte ASCII, 2-byte Latin, 3-byte symbols, and
    4-byte emoji characters in round-robin order. Uses a fixed seed
    for reproducibility. The resulting string encodes to more than
    80 KB of UTF-8, ensuring chunk boundary splits in both the Python
    path (4096-byte reads) and the Rust path (65536-byte reads).

    Parameters
    ----------
    n_chars : int
        Number of characters to generate. Defaults to ``32768``.

    Returns
    -------
    str
        A string containing a mix of 1, 2, 3, and 4-byte UTF-8
        characters.
    """
    rng = random.Random(_SEED)  # noqa: S311 — not security-sensitive; deterministic test seed
    chars: list[str] = []
    for i in range(n_chars):
        pool = _POOLS[i % len(_POOLS)]
        chars.append(rng.choice(pool))
    return "".join(chars)


def chunk_sizes_from_cut_points(
    payload_size: int,
    cut_points: tuple[int, ...],
) -> tuple[int, ...]:
    """Convert sorted cut points into sequential chunk sizes.

    Parameters
    ----------
    payload_size : int
        Total payload length in bytes.
    cut_points : tuple[int, ...]
        Strictly increasing byte offsets that split the payload.

    Returns
    -------
    tuple[int, ...]
        A tuple of positive chunk sizes that sums to ``payload_size``.
    """
    if payload_size < 0:
        msg = f"payload_size must be non-negative, got {payload_size}"
        raise ValueError(msg)

    if not cut_points:
        return (payload_size,) if payload_size > 0 else ()

    previous = 0
    sizes: list[int] = []
    for cut in cut_points:
        if cut <= previous or cut >= payload_size:
            msg = (
                "cut points must be strictly increasing and inside the payload, "
                f"got {cut_points!r} for payload_size={payload_size}"
            )
            raise ValueError(msg)
        sizes.append(cut - previous)
        previous = cut

    sizes.append(payload_size - previous)
    return tuple(sizes)


def deterministic_property_case(
    *,
    seed: int,
    payload_size: int,
    max_cuts: int = 8,
) -> tuple[bytes, tuple[int, ...]]:
    """Generate a deterministic random payload with chunk sizes.

    Parameters
    ----------
    seed : int
        Random seed for reproducible payload and cut-point generation.
    payload_size : int
        Payload length in bytes.
    max_cuts : int
        Maximum number of cut points used to split the payload.

    Returns
    -------
    tuple[bytes, tuple[int, ...]]
        The payload bytes and derived chunk sizes.
    """
    if payload_size < 0:
        msg = f"payload_size must be non-negative, got {payload_size}"
        raise ValueError(msg)
    if max_cuts < 0:
        msg = f"max_cuts must be non-negative, got {max_cuts}"
        raise ValueError(msg)

    rng = random.Random(seed)  # noqa: S311 — deterministic test data only
    payload = rng.randbytes(payload_size)

    if payload_size <= 1 or max_cuts == 0:
        return payload, (payload_size,) if payload_size == 1 else ()

    upper_bound = min(max_cuts, payload_size - 1)
    cut_count = rng.randint(1, upper_bound)
    cut_points = tuple(sorted(rng.sample(range(1, payload_size), cut_count)))
    return payload, chunk_sizes_from_cut_points(payload_size, cut_points)


def chunked_writer_script() -> str:
    """Return Python code that writes payload bytes in configured chunks.

    The script expects two argv entries:

    - argv[1]: base64-encoded payload bytes;
    - argv[2]: comma-separated chunk sizes, or an empty string.
    """
    return "\n".join(
        [
            "import base64",
            "import sys",
            "payload = base64.b64decode(sys.argv[1].encode('ascii'))",
            "chunk_arg = sys.argv[2]",
            (
                "sizes = [int(part) for part in chunk_arg.split(',') if part] "
                "if chunk_arg else []"
            ),
            "offset = 0",
            "for size in sizes:",
            "    end = offset + size",
            "    sys.stdout.buffer.write(payload[offset:end])",
            "    sys.stdout.buffer.flush()",
            "    offset = end",
            "sys.stdout.buffer.write(payload[offset:])",
            "sys.stdout.buffer.flush()",
        ],
    )


def payload_to_base64(payload: bytes) -> str:
    """Encode payload bytes to an ASCII base64 string."""
    return base64.b64encode(payload).decode("ascii")


def hex_sink_script() -> str:
    """Return Python code that reads stdin bytes and prints lowercase hex."""
    return "import sys; data = sys.stdin.buffer.read(); sys.stdout.write(data.hex())"
