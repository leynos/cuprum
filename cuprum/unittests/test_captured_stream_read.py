"""Read-back behaviour for file-backed capture records."""

from __future__ import annotations

import pathlib

import pytest

from cuprum import CapturedStream, CapturePolicyKind


def test_read_text_propagates_a_missing_spill_file(tmp_path: pathlib.Path) -> None:
    """A removed kept spill remains an ordinary filesystem read failure."""
    record = CapturedStream(
        stream="stdout",
        policy=CapturePolicyKind.FILE_BACKED,
        total_bytes=1,
        storage="file",
        path=tmp_path / "removed-spill.out",
        owned=False,
    )

    with pytest.raises(FileNotFoundError):
        record.read_text()


def test_read_text_propagates_an_unreadable_spill_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permission failure remains the filesystem's original error."""
    record = CapturedStream(
        stream="stdout",
        policy=CapturePolicyKind.FILE_BACKED,
        total_bytes=1,
        storage="file",
        path=tmp_path / "unreadable-spill.out",
        owned=False,
    )
    failure = PermissionError("access denied")

    def deny_read_text(
        _path: pathlib.Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        """Stand in for a filesystem that refuses this file."""
        del encoding, errors
        raise failure

    monkeypatch.setattr(pathlib.Path, "read_text", deny_read_text)

    with pytest.raises(PermissionError) as caught:
        record.read_text()

    assert caught.value is failure, "the read API must preserve the original error"
