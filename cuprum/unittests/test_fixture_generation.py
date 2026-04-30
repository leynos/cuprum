"""Tests for deterministic base64 fixture generation."""

from __future__ import annotations

import json
import math
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from benchmarks.deterministic_b64_fixture import FixtureConfig, write_fixture
from cuprum.unittests.conftest import _VOLATILE_KEYS, redact

if typ.TYPE_CHECKING:
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


def test_fixture_generation_is_repeatable(tmp_path: pth.Path) -> None:
    """The same seed and size produce identical manifest hashes."""
    first_output = tmp_path / "first.b64"
    first_manifest = tmp_path / "first.json"
    second_output = tmp_path / "second.b64"
    second_manifest = tmp_path / "second.json"

    config = FixtureConfig(seed=12345, raw_bytes=4096, wrap=76)
    first = write_fixture(config, output=first_output, manifest=first_manifest)
    second = write_fixture(config, output=second_output, manifest=second_manifest)

    assert first["sha256"] == second["sha256"], (
        f"expected repeat fixture hashes to match, got {first['sha256']} "
        f"and {second['sha256']}"
    )
    assert first_output.read_bytes() == second_output.read_bytes(), (
        f"expected fixture bytes in {first_output} and {second_output} to match"
    )
    assert json.loads(first_manifest.read_text())["sha256"] == first["sha256"], (
        f"expected manifest {first_manifest} to record hash {first['sha256']}"
    )


def test_write_fixture_manifest_snapshot(
    tmp_path: pth.Path,
    snapshot: SnapshotAssertion,
) -> None:
    """write_fixture manifest structure matches snapshot."""
    config = FixtureConfig(seed=42, raw_bytes=96, wrap=0)
    result = write_fixture(
        config,
        output=tmp_path / "fixture.b64",
        manifest=tmp_path / "manifest.json",
    )
    assert redact(result, _VOLATILE_KEYS) == snapshot


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        pytest.param(
            {"raw_bytes": -1},
            "raw-bytes must be >= 0",
            id="negative-raw-bytes",
        ),
        pytest.param(
            {"wrap": 1},
            "wrap must be 0 or 76",
            id="invalid-wrap",
        ),
    ],
)
def test_fixture_config_rejects_invalid_fields(
    kwargs: dict[str, object],
    fragment: str,
) -> None:
    """FixtureConfig raises ValueError for out-of-range fields."""
    base: dict[str, object] = {"seed": 0, "raw_bytes": 64, "wrap": 0}
    base.update(kwargs)
    seed = base["seed"]
    raw_bytes = base["raw_bytes"]
    wrap = base["wrap"]
    assert isinstance(seed, int)
    assert isinstance(raw_bytes, int)
    assert isinstance(wrap, int)
    with pytest.raises(ValueError, match=fragment):
        FixtureConfig(
            seed=int(seed),
            raw_bytes=int(raw_bytes),
            wrap=int(wrap),
        )


@given(
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    raw_bytes=st.integers(min_value=0, max_value=4096),
    wrap=st.sampled_from([0, 76]),
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_fixture_generation_is_deterministic(
    tmp_path: pth.Path,
    seed: int,
    raw_bytes: int,
    wrap: int,
) -> None:
    """write_fixture always produces the same SHA-256 for any valid FixtureConfig."""
    config = FixtureConfig(seed=seed, raw_bytes=raw_bytes, wrap=wrap)
    first = write_fixture(
        config,
        output=tmp_path / "a.b64",
        manifest=tmp_path / "a.json",
    )
    second = write_fixture(
        config,
        output=tmp_path / "b.b64",
        manifest=tmp_path / "b.json",
    )
    assert first["sha256"] == second["sha256"], (
        f"expected deterministic sha256 values to match, got {first} and {second}"
    )
    assert first["output_bytes"] == second["output_bytes"], (
        f"expected deterministic output byte counts to match, got {first} and {second}"
    )


@given(
    raw_bytes=st.integers(min_value=0, max_value=4096),
    wrap=st.sampled_from([0, 76]),
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_fixture_output_bytes_matches_manifest(
    tmp_path: pth.Path,
    raw_bytes: int,
    wrap: int,
) -> None:
    """The output_bytes field in the manifest equals the fixture file size."""
    config = FixtureConfig(seed=0, raw_bytes=raw_bytes, wrap=wrap)
    output = tmp_path / "fixture.b64"
    manifest = tmp_path / "manifest.json"
    result = write_fixture(config, output=output, manifest=manifest)
    assert result["output_bytes"] == output.stat().st_size, (
        f"expected manifest output_bytes to equal file size, got {result}"
    )


@given(
    raw_bytes=st.integers(min_value=0, max_value=4096),
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_fixture_output_size_follows_base64_expansion_ratio(
    tmp_path: pth.Path,
    raw_bytes: int,
) -> None:
    """Unwrapped fixture output size equals the base64 expansion of raw_bytes.

    The base64 alphabet encodes 3 raw bytes as 4 characters. Output is padded
    to a multiple of 4, so the expected size is ``ceil(raw_bytes / 3) * 4``.
    """
    config = FixtureConfig(seed=0, raw_bytes=raw_bytes, wrap=0)
    output = tmp_path / "fixture.b64"
    manifest = tmp_path / "manifest.json"
    result = write_fixture(config, output=output, manifest=manifest)
    expected_bytes = math.ceil(raw_bytes / 3) * 4
    assert result["output_bytes"] == expected_bytes, (
        f"expected base64 output size {expected_bytes} for raw_bytes={raw_bytes}, "
        f"got {result['output_bytes']}"
    )


@given(
    raw_bytes=st.integers(min_value=1, max_value=4096),
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_fixture_wrap76_lines_are_at_most_76_characters(
    tmp_path: pth.Path,
    raw_bytes: int,
) -> None:
    """Every line in a wrap=76 fixture has at most 76 characters (no newline).

    The MIME base64 line length is exactly 76 characters per line; only the
    final line may be shorter when the encoded payload does not divide evenly.
    """
    config = FixtureConfig(seed=0, raw_bytes=raw_bytes, wrap=76)
    output = tmp_path / "fixture.b64"
    result = write_fixture(
        config,
        output=output,
        manifest=tmp_path / "manifest.json",
    )
    lines = output.read_bytes().split(b"\n")
    non_empty_lines = [line for line in lines if line]
    for line in non_empty_lines[:-1]:
        assert len(line) == 76, (
            f"expected line length 76 for wrap=76 fixture with "
            f"raw_bytes={raw_bytes}, got {len(line)}"
        )
    if non_empty_lines:
        assert 1 <= len(non_empty_lines[-1]) <= 76, (
            f"expected last line length in [1, 76], got {len(non_empty_lines[-1])}"
        )
    output_bytes = result["output_bytes"]
    assert isinstance(output_bytes, int)
    assert output_bytes > 0
