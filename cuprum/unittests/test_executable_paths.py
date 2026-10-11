"""Named examples for the executable-path validation vocabulary.

``cuprum.executable_paths`` owns the "is this string an acceptable executable
path?" question: the rejection enum, the syntactic classifier, the validating
constructor, the string coercion, and the advisory filesystem probe.
``cuprum.executable_binding`` re-exports all of it, so these tests reach the
same objects through ``cuprum.executable_paths`` to pin the re-export contract
and to keep the vocabulary testable on its own.

The property-based coverage of the classifier lives with the binding tests,
because the classifier is what the binding constructor is built from.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest

import cuprum.executable_binding as binding_module
import cuprum.executable_paths as paths_module
from cuprum.executable_paths import (
    ExecutablePath,
    InvalidExecutableBindingError,
    PathBindingRejection,
    classify_executable_path,
    coerce_path_string,
    executable_path,
)

_RE_EXPORTED = (
    "ExecutablePath",
    "InvalidExecutableBindingError",
    "PathBindingRejection",
    "advisory_path_rejection",
    "classify_executable_path",
    "executable_path",
)


@pytest.mark.parametrize("name", _RE_EXPORTED)
def test_binding_module_re_exports_the_same_object(name: str) -> None:
    """The binding module re-exports this module's names rather than copies.

    Two module-level names that merely look alike would let the vocabulary and
    the binding drift, so the identity is asserted rather than the presence.
    """
    assert getattr(binding_module, name) is getattr(paths_module, name), (
        f"{name} must be the same object in both modules"
    )


def test_binding_module_exports_only_the_documented_names() -> None:
    """Every re-exported name is declared in the binding module's ``__all__``."""
    undeclared = [name for name in _RE_EXPORTED if name not in binding_module.__all__]
    assert not undeclared, f"Names missing from __all__: {undeclared}"


def test_every_rejection_message_names_the_type() -> None:
    """Each category's value is a user-facing message about ``ExecutablePath``."""
    for member in PathBindingRejection:
        assert member.value.startswith("ExecutablePath"), (
            f"{member.name} must produce a message naming the type"
        )


def test_invalid_binding_error_is_a_value_error() -> None:
    """Callers that catch ``ValueError`` keep working."""
    assert issubclass(InvalidExecutableBindingError, ValueError)


def test_classify_reports_each_syntactic_category() -> None:
    """The three syntactic categories are distinguishable from one another."""
    assert (
        classify_executable_path("", allow_relative=False) is PathBindingRejection.EMPTY
    )
    assert (
        classify_executable_path("to\x00ol", allow_relative=False)
        is PathBindingRejection.NUL
    )
    assert (
        classify_executable_path("../tool", allow_relative=False)
        is PathBindingRejection.PARENT_SEGMENT
    )
    assert (
        classify_executable_path("tool", allow_relative=False)
        is PathBindingRejection.NOT_ABSOLUTE
    )


def test_classification_is_pure_and_repeatable() -> None:
    """Classifying the same input twice yields the same category."""
    raw = "bin/tool"
    first = classify_executable_path(raw, allow_relative=False)
    second = classify_executable_path(raw, allow_relative=False)
    assert first is second


def test_executable_path_returns_a_newtype_over_str() -> None:
    """The constructor yields the :data:`ExecutablePath` newtype."""
    result = executable_path("/opt/tools/tool")
    assert isinstance(result, str)
    assert result == ExecutablePath("/opt/tools/tool")


def test_executable_path_normalizes_redundant_separators() -> None:
    """Redundant separators and ``.`` segments are collapsed."""
    assert executable_path("/opt//tools/./tool") == "/opt/tools/tool"


def test_executable_path_normalization_is_idempotent() -> None:
    """Normalizing an already-normalized path changes nothing."""
    once = executable_path("/opt//tools/./tool")
    assert executable_path(once) == once


def test_executable_path_reports_reason_and_no_program() -> None:
    """A standalone validation reports the reason and no logical identity."""
    with pytest.raises(InvalidExecutableBindingError) as exc_info:
        executable_path("bin/tool")
    error = exc_info.value
    assert error.program is None, "No logical identity was supplied"
    assert error.path == "bin/tool", "The rejected path is quoted verbatim"
    assert error.reason is PathBindingRejection.NOT_ABSOLUTE
    assert "Path cannot be bound to" in str(error)


@pytest.mark.parametrize(
    "raw",
    ["bin/tool", "../tool", "", "to\x00ol"],
)
def test_executable_path_accepts_only_classifier_approved_values(raw: str) -> None:
    """The constructor raises exactly when the classifier reports a rejection."""
    rejection = classify_executable_path(raw, allow_relative=False)
    assert rejection is not None
    with pytest.raises(InvalidExecutableBindingError) as exc_info:
        executable_path(raw)
    assert exc_info.value.reason is rejection


def test_executable_path_accepts_a_path_object() -> None:
    """A :class:`~pathlib.Path` is accepted for symmetry with ``safe_path``."""
    assert executable_path(Path("/opt/tools/tool")) == "/opt/tools/tool"


def test_executable_path_rejects_a_non_path_like_value() -> None:
    """A value that is neither a string nor path-like raises ``TypeError``."""
    with pytest.raises(TypeError, match="ExecutablePath expects str or Path"):
        # Deliberately wrong-typed: the runtime guard is what is under test,
        # and a checker-visible call would be rejected before it ran.
        executable_path(typ.cast("typ.Any", 42))


def test_coerce_path_string_passes_strings_through_unchanged() -> None:
    """A string is returned verbatim, not normalized."""
    assert coerce_path_string("/opt//tools/./tool") == "/opt//tools/./tool"


def test_coerce_path_string_keeps_a_path_string_form() -> None:
    """A path-like object is reduced to its string form."""
    assert coerce_path_string(Path("/opt/tools/tool")) == "/opt/tools/tool"


def test_coerce_path_string_rejects_bytes() -> None:
    """Bytes are rejected rather than silently decoded."""
    with pytest.raises(TypeError, match="ExecutablePath expects str or Path"):
        # Deliberately wrong-typed; see the note on the sibling test above.
        coerce_path_string(typ.cast("typ.Any", b"/opt/tools/tool"))


def test_coerce_path_string_names_the_offending_type() -> None:
    """The error names the type the caller actually passed."""
    with pytest.raises(TypeError, match="got int"):
        # Deliberately wrong-typed; see the note on the sibling test above.
        coerce_path_string(typ.cast("typ.Any", 42))
