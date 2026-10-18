"""Real-tool regressions for the builders' end-of-options separators.

``tar_create`` and ``rsync_sync`` emit ``--`` before their positional path
operands so that a source named ``--checkpoint=1`` cannot become an option.
These tests run the installed tar and rsync, where a static argv assertion
cannot show what the tool does with the separator: GNU tar would otherwise
pair ``--checkpoint=1`` with ``--checkpoint-action=exec=...`` and run a
command with the caller's privileges.

Every test skips when its tool is missing. The checkpoint test additionally
requires GNU tar, because the option pair it defends against is GNU tar's.
"""

from __future__ import annotations

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed ``tar --version`` probe.
import tarfile
import typing as typ

import pytest

from cuprum import ExecutionContext, ScopeConfig, scoped
from cuprum.builders import (
    RsyncOptions,
    TarCreateOptions,
    rsync_sync,
    tar_create,
    tar_extract,
)
from cuprum.catalogue import RSYNC, TAR

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import CommandResult, SafeCmd

_TAR_AVAILABLE = shutil.which("tar") is not None
_RSYNC_AVAILABLE = shutil.which("rsync") is not None


def _tar_reports_gnu() -> bool:
    """Report whether ``tar --version`` on PATH identifies GNU tar."""
    if not _TAR_AVAILABLE:
        return False
    # Query the same executable with the fixed ``--version`` argv. Cuprum
    # could do this, but the gate must decide before any scope is active.
    completed = subprocess.run(
        ["tar", "--version"],  # ruff: ignore[start-process-with-partial-path] - resolved from PATH, same as shutil.which.
        capture_output=True,
        text=True,
        check=False,
    )
    return "GNU tar" in completed.stdout


_TAR_SKIP_REASON = "these tests exercise the tar on PATH, which this host lacks"
_GNU_TAR_SKIP_REASON = (
    "the checkpoint-action defence is pinned against GNU tar, whose --version "
    "the tar on PATH does not report as GNU tar"
)
_RSYNC_SKIP_REASON = "these tests exercise the rsync on PATH, which this host lacks"

requires_tar = pytest.mark.skipif(not _TAR_AVAILABLE, reason=_TAR_SKIP_REASON)
requires_gnu_tar = pytest.mark.skipif(
    not _tar_reports_gnu(),
    reason=_GNU_TAR_SKIP_REASON,
)
requires_rsync = pytest.mark.skipif(
    not _RSYNC_AVAILABLE,
    reason=_RSYNC_SKIP_REASON,
)

_CHECKPOINT_SOURCE = "--checkpoint=1"
_CHECKPOINT_ACTION = "--checkpoint-action=exec=touch pwned"


def _run(command: SafeCmd, working_dir: Path) -> CommandResult:
    """Run one curated command inside a scope that allows it."""
    with scoped(ScopeConfig(allowlist=frozenset({TAR, RSYNC}))):
        return command.run_sync(context=ExecutionContext(cwd=working_dir))


def _write(path: Path, content: str) -> None:
    """Write a small deterministic file."""
    path.write_text(content, encoding="utf-8")


@requires_tar
@requires_gnu_tar
def test_gnu_tar_does_not_run_checkpoint_actions_from_source_names(
    tmp_path: Path,
) -> None:
    """Checkpoint-shaped sources are literal members, never tar options."""
    ordinary = tmp_path / "ordinary.txt"
    _write(ordinary, "ordinary\n")
    _write(tmp_path / _CHECKPOINT_SOURCE, "checkpoint\n")
    _write(tmp_path / _CHECKPOINT_ACTION, "action\n")
    archive = tmp_path / "archive.tar"

    command = tar_create(
        "archive.tar",
        [_CHECKPOINT_SOURCE, _CHECKPOINT_ACTION, "ordinary.txt"],
        options=TarCreateOptions(allow_relative=True),
    )
    result = _run(command, tmp_path)

    assert result.ok, f"tar failed with {result.exit_code}: {result.stderr}"
    assert not (tmp_path / "pwned").exists(), (
        "a source name must never reach tar as a checkpoint action"
    )
    with tarfile.open(archive) as members:
        assert set(members.getnames()) == {
            _CHECKPOINT_SOURCE,
            _CHECKPOINT_ACTION,
            "ordinary.txt",
        }, "all three sources must be archived under their literal names"


@requires_tar
def test_tar_round_trip_accepts_leading_dash_paths(tmp_path: Path) -> None:
    """A dash-prefixed archive, source and destination still round-trip."""
    working_dir = tmp_path / "work"
    working_dir.mkdir()
    _write(working_dir / "-source.txt", "payload\n")
    (working_dir / "-out").mkdir()

    create_result = _run(
        tar_create(
            "-archive.tar",
            ["-source.txt"],
            options=TarCreateOptions(allow_relative=True),
        ),
        working_dir,
    )
    assert create_result.ok, f"tar create failed: {create_result.stderr}"

    extract_result = _run(
        tar_extract("-archive.tar", destination="-out", allow_relative=True),
        working_dir,
    )
    assert extract_result.ok, f"tar extract failed: {extract_result.stderr}"
    assert (working_dir / "-out" / "-source.txt").read_text(encoding="utf-8") == (
        "payload\n"
    ), "the dash-prefixed source must extract intact"


@requires_rsync
def test_rsync_copies_option_shaped_relative_paths(tmp_path: Path) -> None:
    """Dash-prefixed source and destination are operands, not options."""
    _write(tmp_path / "--source-file", "payload\n")

    command = rsync_sync(
        "--source-file",
        "--dest-file",
        options=RsyncOptions(archive=True, allow_relative=True),
    )
    result = _run(command, tmp_path)

    assert result.ok, f"rsync failed with {result.exit_code}: {result.stderr}"
    destination = tmp_path / "--dest-file"
    assert destination.is_file(), "rsync must create the option-shaped destination"
    assert destination.read_text(encoding="utf-8") == "payload\n", (
        "the copied payload must match the source"
    )
