"""Tests for the folded-stack summarization utility."""

from __future__ import annotations

import operator
import subprocess  # ruff: ignore[suspicious-subprocess-import] - integration tests exercise fixed CLI commands.
import sys
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from benchmarks.summarize_folded import summarize_folded_file
from cuprum.unittests.conftest import _VOLATILE_KEYS, redact

if typ.TYPE_CHECKING:
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


def _summarize_folded(
    tmp_path: pth.Path,
    content: str,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    pth.Path,
]:
    """Write a folded file, run summarize_folded_file, and return parsed results."""
    folded = tmp_path / "stacks.folded"
    folded.write_text(content)
    summary_path = tmp_path / "summary.json"
    summary = summarize_folded_file(
        folded,
        output=summary_path,
        limit=5,
        example_limit=2,
    )
    top_leaf = typ.cast("list[dict[str, object]]", summary["top_leaf_frames"])
    top_inclusive = typ.cast(
        "list[dict[str, object]]",
        summary["top_inclusive_frames"],
    )
    return summary, top_leaf, top_inclusive, summary_path


@pytest.mark.parametrize(
    ("content", "description"),
    [
        pytest.param("", "empty input", id="empty-file"),
        pytest.param(
            "root;leaf\nroot;leaf not_an_int\n; 3\n",
            "malformed input",
            id="all-invalid-lines",
        ),
    ],
)
def test_folded_summary_yields_zero_totals_for_degenerate_input(
    tmp_path: pth.Path,
    content: str,
    description: str,
) -> None:
    """Degenerate folded input produces zero samples and empty rankings."""
    summary, top_leaf, top_inclusive, summary_path = _summarize_folded(
        tmp_path, content
    )
    assert summary["total_samples"] == 0, (
        f"expected total_samples to be 0 for {description}, got {summary}"
    )
    assert top_leaf == [], (
        f"expected no top leaf frames for {description}, got {top_leaf}"
    )
    assert top_inclusive == [], (
        f"expected no top inclusive frames for {description}, got {top_inclusive}"
    )
    assert summary_path.exists(), f"expected summary file to exist at {summary_path}"


def test_folded_summary_ranks_inclusive_and_leaf_frames(tmp_path: pth.Path) -> None:
    """Folded stack summaries expose ranked frame costs."""
    summary, top_leaf, top_inclusive, summary_path = _summarize_folded(
        tmp_path, "root;parent;leaf 3\nroot;other 2\nroot;parent;leaf 1\n"
    )
    assert summary["total_samples"] == 6, (
        f"expected total_samples to be 6, got {summary}"
    )
    assert top_leaf[0]["frame"] == "leaf", (
        f"expected leaf to be top leaf frame, got {top_leaf}"
    )
    assert top_leaf[0]["leaf_samples"] == 4, (
        f"expected leaf_samples for leaf to be 4, got {top_leaf[0]}"
    )
    assert top_inclusive[0]["frame"] == "root", (
        f"expected root to be top inclusive frame, got {top_inclusive}"
    )
    assert summary_path.exists(), f"expected summary file to exist at {summary_path}"


def test_summarize_folded_snapshot(
    tmp_path: pth.Path,
    snapshot: SnapshotAssertion,
) -> None:
    """summarize_folded_file output structure matches snapshot."""
    summary, _, _, _ = _summarize_folded(
        tmp_path, "root;parent;leaf 3\nroot;other 2\nroot;parent;leaf 1\n"
    )
    assert redact(summary, _VOLATILE_KEYS) == snapshot


def test_folded_summary_counts_repeated_frames_once_per_stack(
    tmp_path: pth.Path,
) -> None:
    """Inclusive folded counts deduplicate frames within one stack."""
    summary, _top_leaf, top_inclusive, _summary_path = _summarize_folded(
        tmp_path, "root;recursive;recursive;leaf 3\n"
    )

    recursive = next(entry for entry in top_inclusive if entry["frame"] == "recursive")

    assert summary["total_samples"] == 3, (
        f"expected total_samples to be 3 for recursive stack, got {summary}"
    )
    assert recursive["inclusive_samples"] == 3, (
        f"expected recursive frame to count once per stack, got {recursive}"
    )
    assert recursive["example_stacks"] == ["root;recursive;recursive;leaf"], (
        f"expected recursive example stack to be retained once, got {recursive}"
    )


@pytest.mark.parametrize(
    ("kwargs", "error_type", "fragment"),
    [
        pytest.param(
            {"limit": 0},
            ValueError,
            "limit must be a positive integer",
            id="limit-zero",
        ),
        pytest.param(
            {"example_limit": 0},
            ValueError,
            "example_limit must be a positive integer",
            id="example-limit-zero",
        ),
        pytest.param(
            {"limit": True},
            TypeError,
            "limit must be a positive integer",
            id="limit-bool",
        ),
    ],
)
def test_folded_summary_rejects_invalid_limits(
    tmp_path: pth.Path,
    kwargs: dict[str, object],
    error_type: type[Exception],
    fragment: str,
) -> None:
    """Folded summary API rejects non-positive and non-integer limits.

    A non-positive but valid integer raises ``ValueError`` while a non-integer
    such as ``bool`` raises ``TypeError``.
    """
    folded = tmp_path / "stacks.folded"
    folded.write_text("root;leaf 1\n")

    with pytest.raises(error_type, match=fragment):
        summarize_folded_file(  # pyright: ignore[reportCallIssue] - Exercise runtime validation with deliberately invalid keyword values.
            folded,
            output=tmp_path / "summary.json",
            **kwargs,  # ty: ignore[invalid-argument-type] - Exercise runtime validation with deliberately invalid keyword values.
        )


@pytest.mark.parametrize(
    ("limit", "example_limit"),
    [
        pytest.param(1, 1, id="minimum"),
        pytest.param(5, 2, id="typical"),
        pytest.param(1000, 100, id="large"),
    ],
)
def test_folded_summary_accepts_valid_limits(
    tmp_path: pth.Path,
    limit: int,
    example_limit: int,
) -> None:
    """Positive integer limits are accepted and bound the generated summary."""
    folded = tmp_path / "stacks.folded"
    # Three distinct call paths share the ``alpha`` leaf, so that frame has more
    # candidate examples than the smaller ``example_limit`` values allow.
    folded.write_text(
        "root;one;alpha 3\nroot;two;alpha 2\nroot;three;alpha 1\n"
        "root;beta 2\nroot;gamma 1\n"
    )
    output = tmp_path / "summary.json"

    summary = summarize_folded_file(
        folded,
        output=output,
        limit=limit,
        example_limit=example_limit,
    )

    top_leaf = typ.cast("list[dict[str, object]]", summary["top_leaf_frames"])
    assert top_leaf, "a valid limit must still produce ranked leaf frames"
    # The fixture yields exactly three leaf frames (alpha, beta, gamma), so
    # the ranked list must hold every frame the limit permits — no more.
    assert len(top_leaf) == min(limit, 3), (
        f"the ranked list must hold exactly min(limit={limit}, 3) frames, "
        f"got {len(top_leaf)}"
    )
    for entry in top_leaf:
        examples = typ.cast("list[str]", entry["example_stacks"])
        assert len(examples) <= example_limit, (
            f"{entry['frame']!r} kept {len(examples)} examples, "
            f"exceeding the requested example_limit of {example_limit}"
        )
    assert output.exists(), "the summary must be written to the requested path"


def _run_folded_summary_cli(
    folded_path: pth.Path,
    output_path: pth.Path,
    *extra_args: str,
) -> subprocess.CompletedProcess[str]:
    """Invoke benchmarks.summarize_folded via subprocess and return the result."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [
            sys.executable,
            "-m",
            "benchmarks.summarize_folded",
            str(folded_path),
            "--output",
            str(output_path),
            *extra_args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            (
                "stacks.folded",
                "root;leaf 1\n",
                ("--limit", "0"),
                "limit must be a positive integer",
            ),
            id="invalid-limit",
        ),
        pytest.param(
            ("missing.folded", None, (), None),
            id="missing-input",
        ),
    ],
)
def test_folded_summary_cli_rejects_invalid_invocation(
    tmp_path: pth.Path,
    case: tuple[str, str | None, tuple[str, ...], str | None],
) -> None:
    """Folded summary CLI exits with code 2 for invalid or missing input."""
    filename, file_content, extra_args, stderr_fragment = case
    folded = tmp_path / filename
    if file_content is not None:
        folded.write_text(file_content)
    completed = _run_folded_summary_cli(folded, tmp_path / "summary.json", *extra_args)
    expected_fragment = stderr_fragment if stderr_fragment is not None else str(folded)
    assert completed.returncode == 2, (
        f"expected CLI exit code 2, got {completed.returncode}"
    )
    assert expected_fragment in completed.stderr, (
        f"expected {expected_fragment!r} in stderr, got {completed.stderr!r}"
    )


@given(
    stacks=st.lists(
        st.tuples(
            st.lists(
                st.text(
                    alphabet=st.characters(
                        blacklist_categories=("Cc", "Cs", "Zl", "Zp", "Zs"),
                        blacklist_characters=(";",),
                    ),
                    min_size=1,
                    max_size=20,
                ),
                min_size=1,
                max_size=5,
            ),
            st.integers(min_value=1, max_value=100),
        ),
        min_size=1,
        max_size=20,
    )
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_folded_summary_total_samples_matches_input(
    tmp_path: pth.Path,
    stacks: list[tuple[list[str], int]],
) -> None:
    """total_samples always equals the sum of per-stack counts."""
    lines = "\n".join(f"{';'.join(frames)} {count}" for frames, count in stacks)
    summary, _leaf, _inclusive, _ = _summarize_folded(tmp_path, lines)
    expected_total = sum(count for _, count in stacks)
    assert summary["total_samples"] == expected_total, (
        f"expected total_samples {expected_total}, got {summary}"
    )


@given(
    stacks=st.lists(
        st.tuples(
            st.lists(
                st.text(
                    alphabet=st.characters(
                        blacklist_categories=("Cc", "Cs", "Zl", "Zp", "Zs"),
                        blacklist_characters=(";",),
                    ),
                    min_size=1,
                    max_size=12,
                ).map(operator.itemgetter(slice(12))),
                min_size=1,
                max_size=6,
            ).map(lambda fs: fs + fs[:1]),
            st.integers(min_value=1, max_value=20),
        ),
        min_size=1,
        max_size=15,
    )
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_inclusive_counts_deduplicate_within_each_stack(
    tmp_path: pth.Path,
    stacks: list[tuple[list[str], int]],
) -> None:
    """Inclusive samples count each frame once per folded stack."""
    lines = "\n".join(f"{';'.join(frames)} {count}" for frames, count in stacks)
    summary, _leaf, inclusive, _ = _summarize_folded(tmp_path, lines)
    expected: dict[str, int] = {}
    for frames, count in stacks:
        for frame in set(frames):
            expected[frame] = expected.get(frame, 0) + count
    expected_total = sum(count for _, count in stacks)
    assert summary["total_samples"] == expected_total, (
        f"expected total_samples {expected_total}, got {summary}"
    )
    for entry in inclusive:
        frame = entry["frame"]
        assert isinstance(frame, str)
        assert entry["inclusive_samples"] == expected[frame]
