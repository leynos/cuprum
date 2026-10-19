"""Unit tests for the rsync local-only operand policy.

``rsync_sync`` accepts local operands only. These tests pin the two rejection
layers separately: the generic host-native ``safe_path`` policy, and the rsync
grammar check that runs after it. Fixtures are chosen so that each assertion
fails for the layer it names, on both POSIX and Windows.
"""

from __future__ import annotations

import sys
import typing as typ

import pytest

from cuprum.builders import RsyncOptions, rsync_sync, safe_path
from cuprum.builders.rsync import _local_rsync_operand

if typ.TYPE_CHECKING:
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
    assert cmd.argv == (expected, expected), (
        "accepted local operands must be emitted in normalized form"
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
    assert cmd.argv == (expected, expected), (
        "native absolute operands must be emitted in normalized form"
    )


@pytest.mark.parametrize("operand", ["host:path", "host::module", "host::module/path"])
def test_rsync_local_operand_rejects_remote_syntax_directly(operand: str) -> None:
    """The private operand check rejects remote syntax independent of _rsync_argv."""
    with pytest.raises(ValueError, match="rsync source must be a local path"):
        _local_rsync_operand(operand, role="source", allow_relative=True)
