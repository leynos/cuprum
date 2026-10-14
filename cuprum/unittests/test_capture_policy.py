"""Unit tests for the capture-policy vocabulary and its resolution."""

from __future__ import annotations

import dataclasses as dc
import pathlib
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import (
    BoundedCapture,
    CapturedStream,
    CapturePolicy,
    CapturePolicyKind,
    FileBackedCapture,
    FullCapture,
    RunOutputOptions,
)
from cuprum._constants import DEFAULT_CAPTURE_SPILL_THRESHOLD_BYTES
from cuprum.capture_policy import policy_kind

# A stream's declared capture setting: a policy, the shorthand's boolean, or
# ``None`` to inherit the run-wide one. Named because these cases pass values
# of exactly this union into the field and read them back, and three separate
# ``object`` annotations would describe neither end of that traffic.
type DeclaredCapture = CapturePolicy | bool | None

# A policy a caller may name, plus the shorthand spellings, plus ``None`` for
# "inherit the shorthand". The inherited value is not in the strategy: it is
# covered by the parameterized resolution matrix below, which has to reason
# about the two fields together.
_POLICIES = st.one_of(
    st.none(),
    st.booleans(),
    st.builds(FullCapture),
    st.builds(BoundedCapture, max_bytes=st.integers(min_value=1, max_value=4096)),
    st.builds(
        FileBackedCapture,
        spill_threshold_bytes=st.one_of(
            st.none(), st.integers(min_value=1, max_value=4096)
        ),
        directory=st.none(),
        keep_file=st.booleans(),
    ),
)


@settings(max_examples=200)
@given(capture=st.booleans(), stdout=_POLICIES, stderr=_POLICIES)
def test_each_stream_resolves_from_its_own_field(
    *,
    capture: bool,
    stdout: DeclaredCapture,
    stderr: DeclaredCapture,
) -> None:
    """A per-stream field governs that stream and never the other.

    The property the whole feature rests on: an override on one stream must
    not leak to its sibling, in either direction. A joint switch — resolving
    both from ``capture`` whatever the caller named — fails this on the first
    example where the two fields differ.
    """
    options = RunOutputOptions(
        capture=capture,
        capture_stdout=stdout,
        capture_stderr=stderr,
    )
    resolved_stdout, resolved_stderr = options.resolved_capture

    def expected(declared: DeclaredCapture) -> CapturePolicyKind | None:
        if declared is None:
            return policy_kind(FullCapture()) if capture else None
        if isinstance(declared, bool):
            return policy_kind(FullCapture()) if declared else None
        return policy_kind(declared)

    assert policy_kind(resolved_stdout) == expected(stdout), (
        f"stdout resolved to {resolved_stdout!r} for declared {stdout!r}"
    )
    assert policy_kind(resolved_stderr) == expected(stderr), (
        f"stderr resolved to {resolved_stderr!r} for declared {stderr!r}"
    )
    # A stream left as ``None`` inherits the shorthand, so the two fields
    # cannot both be ``None`` and still report different storages.
    if stdout is None and stderr is None:
        assert resolved_stdout == resolved_stderr, (
            "two inherited streams must resolve identically"
        )


@pytest.mark.parametrize(
    ("capture", "declared", "expected"),
    [
        pytest.param(True, None, "full", id="inherit-true"),
        pytest.param(False, None, None, id="inherit-false"),
        pytest.param(False, True, "full", id="override-on"),
        pytest.param(True, False, None, id="override-off"),
        pytest.param(True, "bounded", "bounded", id="override-bounded"),
        pytest.param(True, "file", "file_backed", id="override-file-backed"),
        pytest.param(False, "bounded", "bounded", id="override-bounded-over-off"),
    ],
)
def test_capture_resolution_matrix(
    *,
    capture: bool,
    declared: bool | str,
    expected: str | None,
) -> None:
    """Every quadrant of shorthand-versus-override resolves as documented."""
    named: dict[str, CapturePolicy] = {
        "bounded": BoundedCapture(max_bytes=16),
        "file": FileBackedCapture(),
    }
    # A case names a policy as a caller would spell one, so the name is looked
    # up rather than passed through: the assertion is what keeps an unknown
    # name from reaching the field as a string and being rejected there, where
    # it would fail for a reason the case did not mean to test.
    if isinstance(declared, str):
        assert declared in named, f"unknown policy name {declared!r}"
        policy: DeclaredCapture = named[declared]
    else:
        policy = declared
    options = RunOutputOptions(capture=capture, capture_stdout=policy)
    assert policy_kind(options.resolved_capture[0]) == expected, (
        f"capture={capture} declared={declared!r} resolved to "
        f"{options.resolved_capture[0]!r}"
    )


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param("bounded", id="string"),
        pytest.param(1, id="integer"),
        pytest.param(0, id="zero"),
        pytest.param(object(), id="object"),
    ],
)
def test_non_policy_capture_value_is_rejected(declared: object) -> None:
    """A value that is neither a policy nor a bool fails before spawning."""
    # Casts because these values are wrong on purpose: the point of the case is
    # that the field rejects at runtime what its type already forbids.
    with pytest.raises(ValueError, match="capture_stdout must be a capture policy"):
        RunOutputOptions(capture_stdout=typ.cast("DeclaredCapture", declared))

    with pytest.raises(ValueError, match="capture_stderr must be a capture policy"):
        RunOutputOptions(capture_stderr=typ.cast("DeclaredCapture", declared))


@pytest.mark.parametrize(
    "max_bytes",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(True, id="bool"),
        pytest.param(1.5, id="float"),
        pytest.param("8", id="string"),
    ],
)
def test_bounded_capture_rejects_a_non_positive_bound(max_bytes: object) -> None:
    """A bound that retains nothing is refused, ``bool`` included."""
    with pytest.raises(ValueError, match="max_bytes must be a positive integer"):
        BoundedCapture(max_bytes=typ.cast("int", max_bytes))


@pytest.mark.parametrize(
    "threshold",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(False, id="bool"),
        pytest.param(2.5, id="float"),
    ],
)
def test_file_backed_capture_rejects_a_non_positive_threshold(
    threshold: object,
) -> None:
    """A spill threshold that is not positive is refused; ``None`` is allowed."""
    with pytest.raises(
        ValueError,
        match="spill_threshold_bytes must be a positive integer or None",
    ):
        FileBackedCapture(
            spill_threshold_bytes=typ.cast("int | None", threshold)
        )


def test_file_backed_capture_defaults_to_the_package_constant() -> None:
    """The spill threshold default is the documented constant, not a literal."""
    assert (
        FileBackedCapture().spill_threshold_bytes
        == DEFAULT_CAPTURE_SPILL_THRESHOLD_BYTES
    ), "the default must track the constant rather than duplicating its value"
    assert DEFAULT_CAPTURE_SPILL_THRESHOLD_BYTES == 1024 * 1024, (
        "the constant itself must stay a size in KiB form"
    )
    assert FileBackedCapture(spill_threshold_bytes=None).spill_threshold_bytes is None


def test_file_backed_capture_normalizes_a_string_directory() -> None:
    """A ``str`` directory becomes a ``Path``, as the docstring promises."""
    capture = FileBackedCapture(directory="spill-dir")
    assert capture.directory == pathlib.Path("spill-dir")
    assert isinstance(capture.directory, pathlib.Path), (
        "the directory must be normalized so the spill code never sees a str"
    )
    assert FileBackedCapture().directory is None


def test_policies_are_frozen_and_hashable() -> None:
    """The vocabulary is immutable, so it can be shared and compared by value."""
    policy = BoundedCapture(max_bytes=8)
    with pytest.raises(dc.FrozenInstanceError):
        policy.max_bytes = 16  # ty: ignore[invalid-assignment] - the frozen write is the assertion
    assert hash(policy) == hash(BoundedCapture(max_bytes=8))
    assert len({FullCapture(), FullCapture()}) == 1


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        pytest.param(FullCapture(), CapturePolicyKind.FULL, id="full"),
        pytest.param(
            BoundedCapture(max_bytes=4),
            CapturePolicyKind.BOUNDED,
            id="bounded",
        ),
        pytest.param(FileBackedCapture(), CapturePolicyKind.FILE_BACKED, id="file"),
    ],
)
def test_captured_stream_reports_the_applied_policy(
    *,
    policy: CapturePolicy,
    expected: CapturePolicyKind,
) -> None:
    """The metadata names a closed-vocabulary kind, not a class name."""
    record = CapturedStream(
        stream="stdout",
        policy=policy_kind(policy),
        total_bytes=16,
    )
    assert record.policy is expected
    assert record.truncated is False, "an untruncated capture is not truncated"
    assert record.storage == "memory"
    assert record.path is None
    assert record.owned is False


def test_captured_stream_reports_truncation_from_its_drop_count() -> None:
    """``truncated`` is derived, so it cannot disagree with ``dropped_bytes``."""
    record = CapturedStream(
        stream="stderr",
        policy=CapturePolicyKind.BOUNDED,
        total_bytes=16,
        dropped_bytes=8,
    )
    assert record.truncated is True


def test_read_text_refuses_a_memory_backed_capture() -> None:
    """Reading a capture with no file says so instead of returning ``""``."""
    record = CapturedStream(
        stream="stdout",
        policy=CapturePolicyKind.FULL,
        total_bytes=4,
    )
    with pytest.raises(ValueError, match="memory-backed"):
        record.read_text()


def test_read_text_reads_a_spilled_capture(tmp_path: pathlib.Path) -> None:
    """A spilled capture's bytes come back decoded from its path."""
    spill = tmp_path / "spill.out"
    spill.write_bytes("héllo".encode())
    record = CapturedStream(
        stream="stdout",
        policy=CapturePolicyKind.FILE_BACKED,
        total_bytes=len(spill.read_bytes()),
        storage="file",
        path=spill,
        owned=True,
    )
    assert record.read_text() == "héllo"
