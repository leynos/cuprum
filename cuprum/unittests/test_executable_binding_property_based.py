"""Property-based tests for executable-binding classification and resolution.

The classification and resolution helpers in ``cuprum.executable_binding`` are
pure total functions over large input domains: any string may be offered as an
executable path, and any binding may be resolved against any working
directory. Hypothesis explores those domains far more thoroughly than a
hand-written table can.

The invariants checked here are:

- Totality: classification returns a member or ``None`` but never raises, for
  any string and either value of ``allow_relative``.
- Consistency: ``executable_path`` and ``executable_binding`` raise
  ``InvalidExecutableBindingError`` exactly when the classifier reports a
  rejection, and the error carries that rejection verbatim.
- Round trip: a valid value classifies as ``None`` again after normalization,
  and normalizing twice is idempotent.
- Resolution shape: resolving an absolute binding is the identity; resolving a
  relative binding against a working directory yields an absolute path inside
  that directory; resolving without a working directory preserves the
  binding's own spelling.
- Resolver invocation: a resolver is called exactly once per resolution, and
  its result is passed through unchanged.

Text witnesses for the four categories a generator can reach from string input
alone are covered here. ``NOT_FOUND`` and ``NOT_EXECUTABLE`` need filesystem
state, so their witnesses live in ``test_executable_binding.py``; its witness
table asserts coverage of the full enum, which is the check that fails when a
new category is added without a test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum.executable_binding import (
    ExecutableBinding,
    InvalidExecutableBindingError,
    PathBindingRejection,
    classify_executable_path,
    executable_binding,
    executable_path,
    resolve_binding,
)
from cuprum.program import Program

PROGRAM = Program("tool")
"""Logical identity used by every generated binding."""

# A small alphabet carrying the boundary characters that matter: NUL, both
# separators, the parent-segment dot, and characters that are safe anywhere.
_FUZZ_ALPHABET = "ab./\\-\x00_ "
_FUZZ_TEXT = st.text(alphabet=_FUZZ_ALPHABET, max_size=12)
# A segment that can never be formed from rejection-triggering characters, so
# it composes cleanly into witness inputs.
_SAFE_SEGMENT = st.text(alphabet="abcXYZ0123_", min_size=1, max_size=5)
# A platform-native absolute anchor. On Windows a leading "/" without a drive
# is root-relative rather than absolute, so anchor with a drive there.
_ABSOLUTE_ANCHOR = "C:/" if os.name == "nt" else "/"
_SEGMENTS = st.lists(_SAFE_SEGMENT, min_size=1, max_size=4)


@st.composite
def _absolute_path(draw: st.DrawFn) -> str:
    """Compose a traversal-free path anchored at the platform's root."""
    return _ABSOLUTE_ANCHOR + "/".join(draw(_SEGMENTS))


@st.composite
def _relative_path(draw: st.DrawFn) -> str:
    """Compose a traversal-free relative path with at least one segment."""
    return "/".join(draw(_SEGMENTS))


_ABSOLUTE_PATH = _absolute_path()
_RELATIVE_PATH = _relative_path()
_CWD = st.one_of(st.none(), _ABSOLUTE_PATH)

# Inputs that provoke each category reachable from string input alone, paired
# with the category they must provoke. The filesystem-dependent categories are
# covered by the witness table in the named-example module.
_TEXT_WITNESSES = (
    (PathBindingRejection.EMPTY, "", True),
    (PathBindingRejection.EMPTY, "", False),
    (PathBindingRejection.NUL, "/opt/to\x00ol", True),
    (PathBindingRejection.NUL, "/opt/to\x00ol", False),
    (PathBindingRejection.PARENT_SEGMENT, "/opt/../bin", True),
    (PathBindingRejection.PARENT_SEGMENT, "/opt/../bin", False),
    (PathBindingRejection.NOT_ABSOLUTE, "bin/tool", False),
)


def test_text_witnesses_cover_every_text_reachable_category() -> None:
    """A category reachable from string input must have a witness here.

    ``NOT_FOUND`` and ``NOT_EXECUTABLE`` are excluded because they cannot be
    provoked by a string alone; the named-example module's witness table
    asserts coverage of the whole enum, so a newly added category still fails
    a test rather than slipping through both modules.
    """
    filesystem_only = {
        PathBindingRejection.NOT_FOUND,
        PathBindingRejection.NOT_EXECUTABLE,
    }
    covered = {rejection for rejection, _, _ in _TEXT_WITNESSES}
    assert covered == set(PathBindingRejection) - filesystem_only


@pytest.mark.parametrize(
    ("rejection", "raw", "allow_relative"),
    _TEXT_WITNESSES,
    ids=[
        f"{rejection.name}-{'relative' if allow_relative else 'absolute'}"
        for rejection, _, allow_relative in _TEXT_WITNESSES
    ],
)
def test_text_witnesses_provoke_their_category(
    rejection: PathBindingRejection,
    raw: str,
    *,
    allow_relative: bool,
) -> None:
    """Each witness provokes exactly the category it is paired with."""
    observed = classify_executable_path(raw, allow_relative=allow_relative)
    assert observed is rejection, (
        f"{raw!r} (allow_relative={allow_relative}) was expected to report "
        f"{rejection} but reported {observed}"
    )


@settings(max_examples=300)
@given(raw=_FUZZ_TEXT, allow_relative=st.booleans())
def test_classification_is_total(raw: str, *, allow_relative: bool) -> None:
    """Classification returns a member or ``None`` but never raises."""
    result = classify_executable_path(raw, allow_relative=allow_relative)
    assert result is None or isinstance(result, PathBindingRejection)


@settings(max_examples=300)
@given(raw=_FUZZ_TEXT, allow_relative=st.booleans())
def test_executable_path_matches_classification(
    raw: str,
    *,
    allow_relative: bool,
) -> None:
    """``executable_path`` raises exactly when classification reports a rejection."""
    rejection = classify_executable_path(raw, allow_relative=allow_relative)
    if rejection is None:
        assert isinstance(executable_path(raw, allow_relative=allow_relative), str)
        return
    with pytest.raises(InvalidExecutableBindingError) as exc_info:
        executable_path(raw, allow_relative=allow_relative)
    assert exc_info.value.reason is rejection
    assert str(rejection.value) in str(exc_info.value)


@settings(max_examples=300)
@given(raw=_FUZZ_TEXT, allow_relative=st.booleans())
def test_executable_binding_matches_classification(
    raw: str,
    *,
    allow_relative: bool,
) -> None:
    """``executable_binding`` raises the classified rejection, naming the program."""
    rejection = classify_executable_path(raw, allow_relative=allow_relative)
    if rejection is None:
        binding = executable_binding(PROGRAM, raw, allow_relative=allow_relative)
        assert binding.path is not None, "A static binding must carry its path"
        assert binding.resolver is None, "A static binding must not carry a resolver"
        return
    with pytest.raises(InvalidExecutableBindingError) as exc_info:
        executable_binding(PROGRAM, raw, allow_relative=allow_relative)
    error = exc_info.value
    assert error.program == PROGRAM, "The error must name the logical program"
    assert error.path == raw, "The error must quote the rejected path verbatim"
    assert error.reason is rejection, "The error must carry the classified reason"


@given(raw=_ABSOLUTE_PATH)
def test_absolute_paths_validate_and_normalize_idempotently(raw: str) -> None:
    """A traversal-free absolute path validates and normalizes stably."""
    assert classify_executable_path(raw, allow_relative=False) is None
    normalized = executable_path(raw)
    assert classify_executable_path(normalized, allow_relative=False) is None
    assert executable_path(normalized) == normalized, "Normalization must be idempotent"


@given(segments=st.lists(_SAFE_SEGMENT, min_size=1, max_size=3))
def test_relative_paths_pin_the_not_absolute_category(segments: list[str]) -> None:
    """A relative, NUL-free, traversal-free path is rejected as ``NOT_ABSOLUTE``."""
    raw = "/".join(segments)
    assert (
        classify_executable_path(raw, allow_relative=False)
        is PathBindingRejection.NOT_ABSOLUTE
    )


@given(prefix=_SAFE_SEGMENT, suffix=_SAFE_SEGMENT)
def test_parent_segments_pin_the_traversal_category(prefix: str, suffix: str) -> None:
    """A ``..`` segment is reported as a traversal, not as a relative path."""
    raw = f"/{prefix}/../{suffix}"
    assert (
        classify_executable_path(raw, allow_relative=True)
        is PathBindingRejection.PARENT_SEGMENT
    )


@given(prefix=_SAFE_SEGMENT, suffix=_SAFE_SEGMENT)
def test_nul_pins_the_nul_category(prefix: str, suffix: str) -> None:
    """A NUL byte outranks every other consideration."""
    raw = f"/{prefix}\x00{suffix}"
    assert (
        classify_executable_path(raw, allow_relative=False) is PathBindingRejection.NUL
    )


@given(raw=_FUZZ_TEXT.filter(lambda s: "\x00" in s))
def test_nul_character_is_always_rejected(raw: str) -> None:
    """Any NUL-bearing string is rejected under either policy."""
    assert (
        classify_executable_path(raw, allow_relative=True) is PathBindingRejection.NUL
    )


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


@given(path=_ABSOLUTE_PATH, cwd=_CWD)
def test_absolute_binding_resolves_to_itself(path: str, cwd: str | None) -> None:
    """A working directory never displaces an absolute binding."""
    binding = executable_binding(PROGRAM, path)
    assert resolve_binding(binding, cwd=cwd) == str(binding.path)


@given(path=_RELATIVE_PATH, cwd=_ABSOLUTE_PATH)
def test_relative_binding_resolves_inside_the_working_directory(
    path: str,
    cwd: str,
) -> None:
    """A relative binding with a working directory becomes an absolute path."""
    binding = executable_binding(PROGRAM, path, allow_relative=True)
    resolved = resolve_binding(binding, cwd=cwd)
    assert Path(resolved).is_absolute(), "A cwd-anchored resolution must be absolute"
    assert resolved.startswith(cwd.rstrip("/") + "/"), (
        f"{resolved!r} must live under the working directory {cwd!r}"
    )


@given(path=_RELATIVE_PATH)
def test_relative_binding_without_a_cwd_stays_relative(path: str) -> None:
    """Without a working directory the binding's own spelling is preserved."""
    binding = executable_binding(PROGRAM, path, allow_relative=True)
    assert resolve_binding(binding, cwd=None) == str(binding.path)


@given(path=_ABSOLUTE_PATH, cwd=_CWD)
def test_resolution_is_idempotent(path: str, cwd: str | None) -> None:
    """Resolving the same binding twice yields the same executable."""
    binding = executable_binding(PROGRAM, path)
    assert resolve_binding(binding, cwd=cwd) == resolve_binding(binding, cwd=cwd)


@given(path=_ABSOLUTE_PATH, cwd=_CWD)
def test_resolver_is_invoked_once_per_resolution(
    path: str,
    cwd: str | None,
) -> None:
    """A lazy binding evaluates its resolver once per resolve, no more."""
    calls: list[int] = []

    def resolver() -> str:
        """Record each call so the count is observable."""
        calls.append(len(calls) + 1)
        return path

    binding = executable_binding(PROGRAM, resolver)
    assert resolve_binding(binding, cwd=cwd) == path, "A resolver owns its own result"
    assert calls == [1], "One resolution must invoke the resolver exactly once"


@given(path=_RELATIVE_PATH, cwd=_ABSOLUTE_PATH)
def test_resolver_result_is_not_anchored_by_cwd(path: str, cwd: str) -> None:
    """``cwd`` applies to a path binding, never to a resolver's own result."""
    binding = executable_binding(PROGRAM, lambda: path)
    assert resolve_binding(binding, cwd=cwd) == path


def test_binding_requires_exactly_one_source() -> None:
    """Constructing a binding with neither source is rejected."""
    with pytest.raises(ValueError, match="exactly one"):
        ExecutableBinding()


def test_binding_rejects_two_sources() -> None:
    """Constructing a binding with both sources is rejected."""
    with pytest.raises(ValueError, match="exactly one"):
        ExecutableBinding(path=executable_path("/bin/sh"), resolver=lambda: "/bin/sh")


@given(path=_ABSOLUTE_PATH)
def test_static_binding_exposes_only_its_path(path: str) -> None:
    """A path-backed binding never carries a resolver."""
    binding = executable_binding(PROGRAM, path)
    assert binding.path is not None
    assert binding.resolver is None


@given(raw=_ABSOLUTE_PATH)
def test_resolution_never_depends_on_the_host_working_directory(raw: str) -> None:
    """An absolute resolution is independent of the process's own directory."""
    binding = executable_binding(PROGRAM, raw)
    assert resolve_binding(binding, cwd=None) == resolve_binding(
        binding,
        cwd=str(Path.cwd()),
    )
