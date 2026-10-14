"""Behavioural tests for the atomic replacement helper.

``scripts/atomic_write.py`` is the persistence primitive the duplication
allowlist writer depends on, so these tests pin its public contract directly
rather than through that caller: default parent creation, mode preservation
only when a destination exists, and the descriptor discipline around fsync.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b`` (``scripts/tests/test_atomic_write.py``),
the merged revision of PR #276, under the ISC terms in ``LICENSE``.
"""

from __future__ import annotations

import dataclasses as dc
import inspect
import os
import pathlib
import stat
import typing as typ

import pytest

from scripts import atomic_write

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_creates_parent_directories_by_default(tmp_path: Path) -> None:
    """The default options create missing destination directories."""
    destination = tmp_path / "missing" / "nested" / "config.toml"

    atomic_write.atomic_write(destination, b"payload\n")

    assert destination.read_bytes() == b"payload\n", (
        "The destination must hold the replacement content."
    )


def test_without_parent_creation_requires_the_directory(tmp_path: Path) -> None:
    """Disabling parent creation surfaces a missing destination directory."""
    destination = tmp_path / "missing" / "config.toml"

    with pytest.raises(FileNotFoundError):
        atomic_write.atomic_write(
            destination,
            b"payload\n",
            options=atomic_write.AtomicWriteOptions(create_parents=False),
        )
    assert not destination.exists(), "A failed write must not create the destination."


def test_preserves_an_existing_destination_mode(tmp_path: Path) -> None:
    """Mode preservation copies the destination's permissions onto the replacement."""
    destination = tmp_path / "config.toml"
    destination.write_bytes(b"old\n")
    destination.chmod(0o640)

    atomic_write.atomic_write(
        destination,
        b"new\n",
        options=atomic_write.AtomicWriteOptions(preserve_mode=True),
    )

    assert destination.read_bytes() == b"new\n", "The content must be replaced."
    assert destination.stat().st_mode & 0o777 == 0o640, (
        "Mode preservation must retain the destination's permission bits."
    )


def test_preserves_mode_only_when_a_destination_exists(tmp_path: Path) -> None:
    """Mode preservation must not require an initial destination."""
    destination = tmp_path / "new" / "config.toml"

    atomic_write.atomic_write(
        destination,
        b"new\n",
        options=atomic_write.AtomicWriteOptions(preserve_mode=True),
    )

    assert destination.read_bytes() == b"new\n", (
        "A missing destination must still be created when preserving modes."
    )


def test_default_options_are_shared_rather_than_rebuilt() -> None:
    """The signature default is the module singleton, not a per-call construction."""
    default = inspect.signature(atomic_write.atomic_write).parameters["options"].default
    assert default is atomic_write._DEFAULT_OPTIONS, (
        "The default options must come from the module-level singleton."
    )


@dc.dataclass(slots=True)
class _OsSpy:
    """Record what ``atomic_write`` fsyncs, delegating the rest to ``os``.

    ``atomic_write`` closes every descriptor it syncs before it returns, so
    each descriptor is inspected while it is still open and the ``stat`` result
    kept; a later ``os.fstat`` on the recorded number would raise ``EBADF``.
    """

    #: The spy stands in for the whole ``os`` module, so the directory-sync
    #: helper's ``os.O_RDONLY`` has to resolve here too.
    O_RDONLY: typ.ClassVar[int] = os.O_RDONLY

    synced: list[os.stat_result] = dc.field(default_factory=list)

    def fsync(self, descriptor: int) -> None:
        """Record the descriptor's stat, then sync it for real."""
        self.synced.append(os.fstat(descriptor))
        os.fsync(descriptor)

    def open(self, path: Path, flags: int) -> int:
        """Delegate descriptor opening."""
        return os.open(path, flags)

    def close(self, descriptor: int) -> None:
        """Delegate descriptor closing."""
        os.close(descriptor)


@dc.dataclass(slots=True)
class _FailingDirectorySync:
    """Fail the directory sync, recording which descriptors get closed.

    The first ``fsync`` reaches the temporary file and is allowed through; the
    second reaches the directory and raises. ``atomic_write`` opens the
    directory through ``os.open``, so every descriptor recorded here is the
    directory descriptor, and the ``close`` calls show whether that descriptor
    was released on the failing path.
    """

    #: The spy stands in for the whole ``os`` module, so the directory-sync
    #: helper's ``os.O_RDONLY`` has to resolve here too.
    O_RDONLY: typ.ClassVar[int] = os.O_RDONLY

    syncs: int = 0
    opened: list[int] = dc.field(default_factory=list)
    closed: list[int] = dc.field(default_factory=list)

    def fsync(self, descriptor: int) -> None:
        """Sync the temporary file, then fail on the directory."""
        self.syncs += 1
        if self.syncs > 1:
            message = "directory sync failed"
            raise OSError(message)
        os.fsync(descriptor)

    def open(self, path: Path, flags: int) -> int:
        """Delegate descriptor opening, recording the descriptor."""
        descriptor = os.open(path, flags)
        self.opened.append(descriptor)
        return descriptor

    def close(self, descriptor: int) -> None:
        """Delegate descriptor closing, recording the descriptor."""
        self.closed.append(descriptor)
        os.close(descriptor)


def test_closes_the_directory_descriptor_when_the_sync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing directory sync must still release the descriptor it opened."""
    destination = tmp_path / "config.toml"
    spy = _FailingDirectorySync()
    monkeypatch.setattr(atomic_write, "os", spy)

    with pytest.raises(OSError, match="directory sync failed"):
        atomic_write.atomic_write(
            destination,
            b"payload\n",
            options=atomic_write.AtomicWriteOptions(sync_file=True),
        )

    assert len(spy.opened) == 1, "The directory descriptor must be opened once."
    assert spy.closed == spy.opened, (
        "The directory descriptor must be closed even when os.fsync raises."
    )
    assert not list(tmp_path.glob(f".{destination.name}.*")), (
        "A failed sync must not leave the temporary sibling behind."
    )


def test_syncs_the_temporary_file_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requesting a sync fsyncs the temporary file and the parent directory."""
    destination = tmp_path / "config.toml"
    spy = _OsSpy()
    monkeypatch.setattr(atomic_write, "os", spy)

    atomic_write.atomic_write(
        destination,
        b"payload\n",
        options=atomic_write.AtomicWriteOptions(sync_file=True),
    )

    assert len(spy.synced) == 2, "File and parent directory must each be fsynced."
    regular_files = [s for s in spy.synced if stat.S_ISREG(s.st_mode)]
    directories = [s for s in spy.synced if stat.S_ISDIR(s.st_mode)]
    assert len(regular_files) == 1, "Exactly one fsync must target the temporary file."
    assert len(directories) == 1, "Exactly one fsync must target the parent directory."
    assert destination.read_bytes() == b"payload\n", "The write must still complete."


@dc.dataclass(slots=True)
class _FailingFileSync:
    """Fail the first ``fsync``, which is the one on the temporary file."""

    #: The spy stands in for the whole ``os`` module, so the directory-sync
    #: helper's ``os.O_RDONLY`` has to resolve here too.
    O_RDONLY: typ.ClassVar[int] = os.O_RDONLY

    opened: list[int] = dc.field(default_factory=list)

    def fsync(self, _descriptor: int) -> None:
        """Refuse the temporary file's sync, before any replacement happens."""
        message = "temporary sync failed"
        raise OSError(message)

    def open(self, path: Path, flags: int) -> int:
        """Delegate descriptor opening, recording the descriptor."""
        descriptor = os.open(path, flags)
        self.opened.append(descriptor)
        return descriptor

    def close(self, descriptor: int) -> None:
        """Delegate descriptor closing."""
        os.close(descriptor)


class _InterruptedStream:
    """A temporary stream whose first write fails, wrapping a real file object.

    The real descriptor is opened so the path exists and the helper's own
    ``Path(stream.name)`` resolves, but every write is refused before it
    reaches the file. That is the failure the helper has to clean up after.
    """

    def __init__(self, path: Path) -> None:
        self.name = str(path)
        self._file = path.open("wb")

    def __enter__(self) -> _InterruptedStream:
        """Enter the context manager, as ``NamedTemporaryFile`` does."""
        return self

    def __exit__(self, *exc_info: object) -> bool:
        """Close the underlying file and propagate any exception."""
        self._file.close()
        return False

    def write(self, _payload: bytes) -> int:
        """Refuse the write."""
        message = "write failed"
        raise OSError(message)

    def flush(self) -> None:
        """Flush the underlying file."""


def test_a_failed_fill_leaves_no_temporary_and_no_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fill that fails mid-sync must leave the destination untouched.

    The temporary is unlinked by the code that created it, so the failure
    cannot leave a stray sibling behind, and the destination keeps its previous
    content rather than being replaced by a partially written file.
    """
    destination = tmp_path / "config.toml"
    destination.write_bytes(b"original\n")
    spy = _FailingFileSync()
    monkeypatch.setattr(atomic_write, "os", spy)

    with pytest.raises(OSError, match="temporary sync failed"):
        atomic_write.atomic_write(
            destination,
            b"replacement\n",
            options=atomic_write.AtomicWriteOptions(sync_file=True),
        )

    assert destination.read_bytes() == b"original\n", (
        "A failed fill must not reach the destination."
    )
    assert not list(tmp_path.glob(f".{destination.name}.*")), (
        "The fill's own cleanup must remove the temporary it created."
    )
    assert not spy.opened, (
        "The directory descriptor is only opened after a successful replace."
    )


def test_a_write_failure_also_removes_the_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure inside ``write`` is cleaned up by the same owner.

    The sync and the write are both inside the fill interval, so a failure in
    either must unlink the temporary before it propagates. Without that, the
    outer guard would be the only cleanup and the two paths would drift.
    """
    destination = tmp_path / "config.toml"
    created: list[Path] = []
    real_named_temporary_file = atomic_write.tempfile.NamedTemporaryFile

    def interrupted_named_temporary_file(**kwargs: object) -> _InterruptedStream:
        """Swap in a stream whose write fails, keeping the real temporary path.

        The keywords are asserted rather than declared in the signature. A real
        ``dir`` parameter would shadow the builtin that ``NamedTemporaryFile``
        itself declares, and a catch-all typed as ``object`` is what lets the
        forwarded call be cast to the real overloads rather than matching none
        of them.

        Returns
        -------
        _InterruptedStream
            A stream over the real temporary's path that refuses every write.
        """
        assert kwargs["delete"] is False, "The caller must keep the temporary."
        assert kwargs["prefix"] == f".{destination.name}.", (
            "The temporary must stay a hidden sibling of the destination."
        )
        with real_named_temporary_file(
            **typ.cast("dict[str, typ.Any]", kwargs)
        ) as real:
            temporary = pathlib.Path(real.name)
        created.append(temporary)
        return _InterruptedStream(temporary)

    monkeypatch.setattr(
        atomic_write.tempfile, "NamedTemporaryFile", interrupted_named_temporary_file
    )

    with pytest.raises(OSError, match="write failed"):
        atomic_write.atomic_write(destination, b"payload\n")

    assert created, "The helper must have created a temporary to fail on."
    assert all(not temporary.exists() for temporary in created), (
        "Every temporary the fill created must be unlinked before it re-raises."
    )
    assert not destination.exists(), "A failed fill must not create the destination."
