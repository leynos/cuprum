"""Named examples for the typed executable-binding value types.

``cuprum.executable_binding`` separates a catalogue's logical identity (a
``Program``) from the string handed to the operating system as ``argv[0]``.
These tests pin the classification table, the advisory filesystem probe, the
structural rule that a binding names exactly one source, and the resolution
rule that turns a binding plus a working directory into an executable string.

The property-based companion module ``test_executable_binding_property_based``
covers the open-ended input domain; this module covers the boundaries that a
generator reaches only by luck, and supplies a witness for every rejection
category so a newly added category cannot slip through untested.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from cuprum.executable_binding import (
    ExecutableBinding,
    InvalidExecutableBindingError,
    PathBindingRejection,
    advisory_path_rejection,
    classify_executable_path,
    executable_binding,
    executable_path,
    resolve_binding,
)
from cuprum.program import Program

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

PROGRAM = Program("tool")
"""Logical catalogue identity reused by most examples."""


def _write_executable(path: Path, *, mode: int = 0o755) -> Path:
    """Create *path* as a regular file with the given permission bits."""
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(mode)
    return path


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_classify_accepts_an_absolute_path() -> None:
    """A traversal-free absolute path classifies as valid."""
    assert classify_executable_path("/opt/tools/bin/tool", allow_relative=False) is None


def test_classify_accepts_a_windows_absolute_path() -> None:
    """A drive-letter path counts as absolute even on POSIX hosts."""
    assert classify_executable_path(r"C:\tools\tool.exe", allow_relative=False) is None


def test_classify_rejects_the_empty_string() -> None:
    """An empty string is reported as empty rather than as relative."""
    assert (
        classify_executable_path("", allow_relative=True) is PathBindingRejection.EMPTY
    )


def test_classify_rejects_a_nul_byte() -> None:
    """A NUL byte is reported before any path-shape consideration."""
    raw = "/opt/to\x00ol"
    assert (
        classify_executable_path(raw, allow_relative=False) is PathBindingRejection.NUL
    )


def test_classify_rejects_a_parent_segment() -> None:
    """A ``..`` segment is rejected even for an otherwise absolute path."""
    raw = "/opt/tools/../bin/tool"
    assert (
        classify_executable_path(raw, allow_relative=False)
        is PathBindingRejection.PARENT_SEGMENT
    )


def test_classify_rejects_a_relative_path_by_default() -> None:
    """Relative paths need an explicit opt-in."""
    assert (
        classify_executable_path("bin/tool", allow_relative=False)
        is PathBindingRejection.NOT_ABSOLUTE
    )


def test_classify_accepts_a_relative_path_when_permitted() -> None:
    """The opt-in relaxes only the absolute-path requirement."""
    assert classify_executable_path("bin/tool", allow_relative=True) is None


@pytest.mark.parametrize(
    "raw",
    ["/opt/tools/../bin/tool", "../tool", ".."],
)
def test_parent_segment_check_precedes_the_absolute_check(raw: str) -> None:
    """A relative path carrying ``..`` is reported as a traversal, not as relative."""
    assert (
        classify_executable_path(raw, allow_relative=False)
        is PathBindingRejection.PARENT_SEGMENT
    )


@pytest.mark.parametrize(
    "raw",
    ["/opt/to\x00ol/../bin", "\x00"],
)
def test_nul_check_precedes_the_parent_segment_check(raw: str) -> None:
    """A NUL byte outranks a traversal when both are present."""
    assert (
        classify_executable_path(raw, allow_relative=False) is PathBindingRejection.NUL
    )


@dc.dataclass(frozen=True, slots=True)
class _Witnessed:
    """A rejection category paired with an input that provokes it."""

    rejection: PathBindingRejection
    raw: str
    allow_relative: bool = False


def _witness_table(tmp_path: Path) -> tuple[_Witnessed, ...]:
    """Pair every rejection category with an input that provokes it."""
    missing = tmp_path / "absent-tool"
    plain = _write_executable(tmp_path / "plain-tool", mode=0o644)
    return (
        _Witnessed(PathBindingRejection.EMPTY, "", allow_relative=True),
        _Witnessed(PathBindingRejection.NUL, "\x00"),
        _Witnessed(PathBindingRejection.PARENT_SEGMENT, "/opt/../bin"),
        _Witnessed(PathBindingRejection.NOT_ABSOLUTE, "bin/tool"),
        _Witnessed(PathBindingRejection.NOT_FOUND, str(missing)),
        _Witnessed(PathBindingRejection.NOT_EXECUTABLE, str(plain)),
    )


def test_every_rejection_category_has_a_witness(tmp_path: Path) -> None:
    """Adding a category without a witness fails this guard."""
    covered = {witness.rejection for witness in _witness_table(tmp_path)}
    assert covered == set(PathBindingRejection)


def test_witness_table_matches_the_classifiers(tmp_path: Path) -> None:
    """Each witness provokes exactly the category it is paired with."""
    for witness in _witness_table(tmp_path):
        observed = classify_executable_path(
            witness.raw,
            allow_relative=witness.allow_relative,
        )
        if observed is None:
            observed = advisory_path_rejection(witness.raw)
        assert observed is witness.rejection, (
            f"{witness.raw!r} was expected to report {witness.rejection} "
            f"but reported {observed}"
        )


# --------------------------------------------------------------------------
# Advisory filesystem probe
# --------------------------------------------------------------------------


def test_advisory_accepts_an_executable_file(tmp_path: Path) -> None:
    """An executable regular file yields no advisory."""
    tool = _write_executable(tmp_path / "tool")
    assert advisory_path_rejection(str(tool)) is None


def test_advisory_reports_a_missing_path(tmp_path: Path) -> None:
    """An absent path is reported as not found."""
    assert (
        advisory_path_rejection(str(tmp_path / "absent"))
        is PathBindingRejection.NOT_FOUND
    )


def test_advisory_reports_a_non_executable_file(tmp_path: Path) -> None:
    """A present but non-executable file is reported as not executable."""
    plain = _write_executable(tmp_path / "plain", mode=0o644)
    assert advisory_path_rejection(str(plain)) is PathBindingRejection.NOT_EXECUTABLE


def test_advisory_reports_a_directory_as_not_executable(tmp_path: Path) -> None:
    """A directory is a present, non-executable file for this probe's purposes."""
    directory = tmp_path / "tools"
    directory.mkdir()
    assert advisory_path_rejection(str(directory)) is (
        PathBindingRejection.NOT_EXECUTABLE
    )


def test_advisory_accepts_a_symlink_to_an_executable(tmp_path: Path) -> None:
    """A symlink pointing at an executable resolves to it rather than being skipped."""
    tool = _write_executable(tmp_path / "tool")
    link = tmp_path / "link"
    link.symlink_to(tool)
    assert advisory_path_rejection(str(link)) is None


def test_advisory_reports_a_dangling_symlink(tmp_path: Path) -> None:
    """A symlink whose target is gone is reported as not found."""
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "absent")
    assert advisory_path_rejection(str(link)) is PathBindingRejection.NOT_FOUND


def test_advisory_skips_a_bare_name() -> None:
    """A bare name is left to the platform's ``PATH`` search, so it is unchecked."""
    assert advisory_path_rejection("tool") is None
    assert advisory_path_rejection("definitely-absent-tool") is None


def test_advisory_checks_a_relative_path_with_a_separator(tmp_path: Path) -> None:
    """A relative path naming a directory component is checked."""
    assert advisory_path_rejection("./absent-tool") is PathBindingRejection.NOT_FOUND


# --------------------------------------------------------------------------
# Binding construction
# --------------------------------------------------------------------------


def test_binding_rejects_setting_neither_source() -> None:
    """A binding must name a path or a resolver."""
    with pytest.raises(ValueError, match="exactly one") as exc_info:
        ExecutableBinding()
    assert "path" in str(exc_info.value) or "resolver" in str(exc_info.value)


def test_binding_rejects_setting_both_sources() -> None:
    """A binding must not name a path and a resolver at once."""
    with pytest.raises(ValueError, match="exactly one"):
        ExecutableBinding(path=executable_path("/bin/sh"), resolver=lambda: "/bin/sh")


def test_executable_binding_accepts_a_path() -> None:
    """A static path becomes a path-backed binding."""
    binding = executable_binding(PROGRAM, "/opt/tools/bin/tool")
    assert binding.path == "/opt/tools/bin/tool"
    assert binding.resolver is None


def test_executable_binding_accepts_a_resolver() -> None:
    """A callable becomes a resolver-backed binding."""
    binding = executable_binding(PROGRAM, lambda: "/opt/tools/bin/tool")
    assert binding.resolver is not None
    assert binding.path is None


def test_executable_binding_normalizes_a_validated_path() -> None:
    """A validated path is normalized through ``PurePath`` like ``SafePath``."""
    binding = executable_binding(PROGRAM, "/opt//tools/./bin/tool")
    assert binding.path == "/opt/tools/bin/tool"


@pytest.mark.parametrize(
    ("raw", "rejection"),
    [
        ("", PathBindingRejection.EMPTY),
        ("/opt/to\x00ol", PathBindingRejection.NUL),
        ("/opt/../tool", PathBindingRejection.PARENT_SEGMENT),
        ("bin/tool", PathBindingRejection.NOT_ABSOLUTE),
    ],
)
def test_executable_binding_names_the_program_and_reason(
    raw: str,
    rejection: PathBindingRejection,
) -> None:
    """A rejected binding names the program, the offending path, and the reason."""
    with pytest.raises(InvalidExecutableBindingError) as exc_info:
        executable_binding(PROGRAM, raw)
    error = exc_info.value
    assert error.program == PROGRAM
    assert error.path == raw
    assert error.reason is rejection
    assert str(rejection.value) in str(error)
    assert str(PROGRAM) in str(error)


def test_executable_binding_honours_the_relative_opt_in(tmp_path: Path) -> None:
    """``allow_relative`` reaches the classifier through the binding constructor."""
    binding = executable_binding(PROGRAM, "bin/tool", allow_relative=True)
    assert binding.path == "bin/tool"


def test_invalid_binding_error_is_a_value_error() -> None:
    """Callers that catch ``ValueError`` keep working."""
    assert issubclass(InvalidExecutableBindingError, ValueError)


def test_executable_binding_does_not_probe_the_filesystem() -> None:
    """Construction is pure: an absent absolute path is still bindable."""
    binding = executable_binding(PROGRAM, "/definitely/absent/tool")
    assert binding.path == "/definitely/absent/tool"


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_resolve_returns_a_static_path_unchanged() -> None:
    """A static path resolves to itself."""
    binding = executable_binding(PROGRAM, "/opt/tools/bin/tool")
    assert resolve_binding(binding, cwd=None) == str(binding.path)


def test_resolve_invokes_a_resolver() -> None:
    """A resolver-backed binding is evaluated for the execution."""
    binding = executable_binding(PROGRAM, lambda: "/opt/tools/bin/tool")
    assert resolve_binding(binding, cwd=None) == "/opt/tools/bin/tool"


def test_resolve_joins_a_relative_path_onto_the_working_directory() -> None:
    """A relative path is anchored at ``cwd`` and made absolute."""
    binding = executable_binding(PROGRAM, "bin/tool", allow_relative=True)
    assert resolve_binding(binding, cwd="/srv/project") == "/srv/project/bin/tool"


def test_resolve_anchors_a_bare_relative_name_in_the_same_way() -> None:
    """A bare relative name is anchored at ``cwd`` too, not left to ``PATH``."""
    binding = executable_binding(PROGRAM, "tool", allow_relative=True)
    assert resolve_binding(binding, cwd="/srv/project") == "/srv/project/tool"


def test_resolve_leaves_a_bare_name_alone_when_there_is_no_cwd() -> None:
    """Without a working directory the name reaches the platform's ``PATH`` search."""
    binding = executable_binding(PROGRAM, "tool", allow_relative=True)
    assert resolve_binding(binding, cwd=None) == "tool"


def test_resolve_leaves_a_relative_path_alone_when_there_is_no_cwd() -> None:
    """Without a working directory a relative path stays relative."""
    binding = executable_binding(PROGRAM, "bin/tool", allow_relative=True)
    assert resolve_binding(binding, cwd=None) == "bin/tool"


def test_resolve_leaves_an_absolute_path_alone_despite_a_cwd() -> None:
    """A working directory never displaces an absolute binding."""
    binding = executable_binding(PROGRAM, "/opt/tools/bin/tool")
    assert resolve_binding(binding, cwd="/srv/project") == "/opt/tools/bin/tool"


def test_resolve_ignores_cwd_for_a_resolver_result() -> None:
    """A resolver owns its own anchoring; ``cwd`` is not applied to its result."""
    binding = executable_binding(PROGRAM, lambda: "bin/tool")
    assert resolve_binding(binding, cwd="/srv/project") == "bin/tool"


def test_resolve_accepts_a_dot_prefixed_relative_path() -> None:
    """A ``.``-anchored relative path is joined and normalized, not left bare."""
    binding = executable_binding(PROGRAM, "./tool", allow_relative=True)
    assert resolve_binding(binding, cwd="/srv/project") == "/srv/project/tool"


def test_resolve_is_pure_across_repeated_calls() -> None:
    """A static binding resolves identically every time."""
    binding = executable_binding(PROGRAM, "bin/tool", allow_relative=True)
    first = resolve_binding(binding, cwd="/srv/project")
    second = resolve_binding(binding, cwd="/srv/project")
    assert first == second


def test_resolver_invocation_count_is_the_caller_s_choice() -> None:
    """``resolve_binding`` calls the resolver once per invocation, no more."""
    calls: list[int] = []

    def resolver() -> str:
        calls.append(len(calls) + 1)
        return "/opt/tools/bin/tool"

    binding = executable_binding(PROGRAM, resolver)
    assert resolve_binding(binding, cwd=None) == "/opt/tools/bin/tool"
    assert calls == [1]
    assert resolve_binding(binding, cwd=None) == "/opt/tools/bin/tool"
    assert calls == [1, 2]


def test_binding_is_immutable_and_hashable() -> None:
    """The value type is a frozen dataclass, so it can cross scope boundaries."""
    binding = executable_binding(PROGRAM, "/opt/tools/bin/tool")
    with pytest.raises(dc.FrozenInstanceError):
        binding.path = executable_path("/bin/sh")
    assert isinstance(hash(binding), int)


def test_binding_exposes_its_source_for_inspection() -> None:
    """Callers can tell a static binding from a lazy one without resolving it."""
    static = executable_binding(PROGRAM, "/opt/tools/bin/tool")
    lazy = executable_binding(PROGRAM, lambda: "/opt/tools/bin/tool")
    assert (static.path, static.resolver) != (lazy.path, lazy.resolver)


def test_resolver_type_alias_accepts_a_zero_argument_callable() -> None:
    """The resolver protocol is a zero-argument callable returning ``str``."""

    def resolver() -> str:
        return "/opt/tools/bin/tool"

    typed: cabc.Callable[[], str] = resolver
    assert executable_binding(PROGRAM, typed).resolver is resolver
