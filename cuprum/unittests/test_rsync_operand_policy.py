"""Tests for the rsync local-only operand policy.

``rsync_sync`` accepts local operands only. These tests pin the two rejection
layers separately: the generic host-native ``safe_path`` policy, and the rsync
grammar check that runs after it. Fixtures are chosen so that each assertion
fails for the layer it names, on both POSIX and Windows.

The parameterized examples state the named spellings from the ticket. The
property tests beside them generalize the same invariant over generated
``host:path`` and ``host::module/path`` operands, and include a non-vacuity
property so the rejection cannot pass for the wrong reason.
"""

from __future__ import annotations

import sys
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import sh
from cuprum.builders import RsyncOptions, rsync_sync, safe_path
from cuprum.builders.rsync import _local_rsync_operand, _rsync_argv

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="host-native absolute semantics differ on Windows"
)
# Each spelling is a remote operand for rsync even though safe_path accepts it
# once relative paths are enabled. The drive-prefixed rows are separate because
# only one layer rejects them on each host.
_REMOTE_OPERANDS = (
    "host:path",
    "host:/path",
    "host::module/path",
    "host::module",
    "user@host:path",
    "rsync://host/module/dir",
    "RSYNC://host/module/dir",
    "[::1]:path",
    "./host:path",
)
_DRIVE_OPERANDS = ("C:/path", "C:\\path")
# A local operand accepted on every host. A native absolute path is rejected on
# the host whose grammar does not treat it as absolute, and a Windows drive
# path is remote-ending syntax for rsync everywhere. The MSYS mount spelling
# keeps its colon after a separator, so rsync reads it as local, and
# ``allow_relative=True`` accepts it on POSIX and Windows alike.
_MSYS_LOCAL = "/cygdrive/c/data"
_ROLES = ("source", "destination")
_RELATIVE = RsyncOptions(allow_relative=True)


def _operands(role: str, operand: str, local: str) -> tuple[str, str]:
    """Return ``(source, destination)`` with ``operand`` in ``role``'s place."""
    return (operand, local) if role == "source" else (local, operand)


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _REMOTE_OPERANDS)
def test_rsync_rejects_remote_operands_when_relative(
    operand: str,
    role: str,
) -> None:
    """With relative paths enabled, remote syntax is rejected by name."""
    with pytest.raises(ValueError, match=rf"rsync {role} must be a local path"):
        rsync_sync(*_operands(role, operand, _MSYS_LOCAL), options=_RELATIVE)


@_POSIX_ONLY
@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _REMOTE_OPERANDS + _DRIVE_OPERANDS)
def test_rsync_rejects_remote_operands_by_default(
    operand: str,
    role: str,
    tmp_path: Path,
) -> None:
    """Default options reject every remote spelling, whatever layer reports it.

    Posix-only: the neutral operand is a native absolute path, which on Windows
    would itself carry a drive prefix and be read as remote.
    """
    source, destination = _operands(role, operand, tmp_path.as_posix())
    with pytest.raises(
        ValueError, match=rf"SafePath|rsync {role} must be a local path"
    ):
        rsync_sync(source, destination)


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _DRIVE_OPERANDS)
def test_rsync_rejects_drive_prefixed_operands_when_relative(
    operand: str,
    role: str,
) -> None:
    """A drive prefix stays a remote operand even with relative paths allowed."""
    with pytest.raises(ValueError, match=rf"rsync {role} must be a local path"):
        rsync_sync(*_operands(role, operand, _MSYS_LOCAL), options=_RELATIVE)


@pytest.mark.parametrize("operand", ["dir/file", "dir/host:path", _MSYS_LOCAL])
def test_rsync_accepts_portable_local_operands_unchanged(operand: str) -> None:
    """Accepted local operands reach the argv as safe_path normalizes them."""
    expected = str(safe_path(operand, allow_relative=True))
    cmd = rsync_sync(operand, operand, options=_RELATIVE)
    assert cmd.argv == ("--", expected, expected), (
        "accepted local operands must follow -- in normalized form"
    )


@_POSIX_ONLY
@pytest.mark.parametrize("suffix", ["", "/host:path"])
def test_rsync_accepts_native_absolute_operands_by_default(
    suffix: str,
    tmp_path: Path,
) -> None:
    """Native absolute operands, including one with a later colon, stay local."""
    operand = tmp_path.as_posix() + suffix
    expected = str(safe_path(operand))
    cmd = rsync_sync(operand, operand)
    assert cmd.argv == ("--", expected, expected), (
        "native absolute operands must follow -- in normalized form"
    )


@pytest.mark.parametrize("operand", ["host:path", "host::module", "host::module/path"])
def test_rsync_local_operand_rejects_remote_syntax_directly(operand: str) -> None:
    """The private operand check rejects remote syntax independent of _rsync_argv."""
    with pytest.raises(ValueError, match="rsync source must be a local path"):
        _local_rsync_operand(operand, role="source", allow_relative=True)


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _REMOTE_OPERANDS)
def test_rsync_rejection_precedes_command_construction(
    operand: str,
    role: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected operand never reaches ``sh.make``, in either role.

    Asserting that no command was returned is weaker than it looks: the builder
    could construct one and then raise, discarding it. Recording the factory
    call itself is what establishes the ordering the ExecPlan requires.
    """
    factory_calls: list[object] = []

    def record_factory_call(program: object) -> cabc.Callable[..., object]:
        """Record that the command factory ran, and return an unusable builder."""
        factory_calls.append(program)

        def refuse(*_argv: str) -> object:
            """Stop the builder if the factory is ever reached."""
            pytest.fail("sh.make must not run for a rejected operand")

        return refuse

    monkeypatch.setattr(sh, "make", record_factory_call)
    with pytest.raises(ValueError, match=rf"rsync {role} must be a local path"):
        rsync_sync(*_operands(role, operand, _MSYS_LOCAL), options=_RELATIVE)
    assert not factory_calls, "the command factory must not be reached"


# Path segments avoid ".", NUL, and separators so generated paths are always
# SafePath-valid and normalization is the identity.
_GENERATED_SEGMENTS = st.text(alphabet="abcxyz09-_", min_size=1, max_size=6)
_GENERATED_ABSOLUTE_PATHS = st.lists(_GENERATED_SEGMENTS, min_size=1, max_size=4).map(
    lambda parts: "/" + "/".join(parts)
)
# A host segment that cannot itself contain the separator the strategy adds.
_GENERATED_HOSTS = st.text(alphabet="abcxyz09-_", min_size=1, max_size=8)
# Remote operands are accepted by safe_path(allow_relative=True); only the
# rsync-local check can reject them, so every generated example reaches it.
_GENERATED_REMOTE_OPERANDS = st.builds(
    lambda host, separator, path: f"{host}{separator}{path}",
    host=_GENERATED_HOSTS,
    separator=st.sampled_from([":", "::"]),
    path=st.lists(_GENERATED_SEGMENTS, min_size=1, max_size=3).map("/".join),
)
# The opposite operand in each rejection property must be a valid local path
# on every host. The MSYS mount spelling keeps its colon after a separator, so
# rsync reads it as local on either host.
_LOCAL_OPERAND = _MSYS_LOCAL


def _has_remote_spelling(operand: str) -> bool:
    """Report whether ``operand`` uses rsync's remote operand grammar."""
    if operand.lower().startswith("rsync://"):
        return True
    colon = operand.find(":")
    slash = operand.find("/")
    return colon != -1 and (slash == -1 or colon < slash)


def _emitted_operand(argv: tuple[str, ...], role: str) -> str:
    """Return the source or destination operand from a built rsync argv."""
    return argv[-1] if role == "destination" else argv[-2]


@settings(max_examples=200)
@given(operand=_GENERATED_REMOTE_OPERANDS, is_source=st.booleans())
def test_rsync_rejects_remote_specification_in_either_operand(
    operand: str,
    *,
    is_source: bool,
) -> None:
    """Generated host-colon-path operands are rejected as source and destination."""
    local = _LOCAL_OPERAND
    source, destination = (operand, local) if is_source else (local, operand)
    role = "source" if is_source else "destination"
    with pytest.raises(ValueError, match=rf"rsync {role} must be a local path"):
        _rsync_argv(source, destination, RsyncOptions(allow_relative=True))


@settings(max_examples=200)
@given(candidate=_GENERATED_REMOTE_OPERANDS | _GENERATED_ABSOLUTE_PATHS)
def test_rsync_accepted_operands_are_never_remote(candidate: str) -> None:
    """Every operand the builder accepts is free of remote spelling.

    The generated space mixes operands the safe-path policy already rejects with
    valid absolute ones, so the implication is exercised on both sides rather
    than being satisfied vacuously.
    """
    local = _LOCAL_OPERAND
    for role, arguments in (
        ("source", (candidate, local)),
        ("destination", (local, candidate)),
    ):
        try:
            argv = _rsync_argv(*arguments, RsyncOptions(allow_relative=True))
        except ValueError:
            continue
        emitted = _emitted_operand(argv, role)
        assert not _has_remote_spelling(emitted), (
            f"the builder emitted a remote {role}: {emitted!r}"
        )


@settings(max_examples=100)
@given(example=_GENERATED_REMOTE_OPERANDS)
def test_remote_strategy_examples_reach_the_sink_check(example: str) -> None:
    """The remote-operand strategy is non-vacuous.

    Each generated example must pass ``safe_path(allow_relative=True)`` and
    only then be rejected by the rsync-local check; otherwise the rejection
    property above could pass for the wrong reason.
    """
    assert safe_path(example, allow_relative=True), (
        "the strategy must generate operands safe_path accepts"
    )
    assert _has_remote_spelling(example), (
        "the generated operand must actually carry remote spelling"
    )
    with pytest.raises(ValueError, match="rsync source must be a local path"):
        _rsync_argv(example, _LOCAL_OPERAND, RsyncOptions(allow_relative=True))
