"""Shared helpers for stream parity tests."""

from __future__ import annotations

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
