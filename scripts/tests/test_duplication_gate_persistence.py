"""Persistence and contention tests for duplication-gate allow entries.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_duplication_gate_persistence.py``), the merged revision
of PR #276, under the ISC terms in ``LICENSE``.
"""

from __future__ import annotations

import contextlib
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests exercise copied gate commands.
import tomllib
from pathlib import Path

import pytest
import tomlkit.exceptions

from scripts.tests.duplication_gate_test_support import (
    allowlist,
    copied_gate_workspace,
    gate_command,
    gate_environment,
)


class TestLoadAllowEntry:
    """Reading reasoned entries without touching the file."""

    def test_loader_never_reaches_the_writer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reading the allowlist must not open, lock, or replace the file."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[[tool.duplication_gate.allow]]\n"
            'unit = "cuprum/a.py"\n'
            'reason = "reviewed"\n',
            encoding="utf-8",
        )
        before = pyproject.stat()

        def refuse(*_args: object, **_kwargs: object) -> None:
            """Fail the test if the read-only loader attempts any write."""
            msg = "the read-only loader must not write"
            raise AssertionError(msg)

        monkeypatch.setattr(allowlist, "atomic_write", refuse)
        monkeypatch.setattr(allowlist, "_locked_file", refuse)

        entries = allowlist.load_allowlist(pyproject)

        assert [entry.keys for entry in entries] == [("cuprum/a.py",)], (
            "The loader must return the parsed entries."
        )
        after = pyproject.stat()
        assert (after.st_mtime_ns, after.st_size) == (
            before.st_mtime_ns,
            before.st_size,
        ), "Loading must leave the file untouched."
        assert not list(tmp_path.glob(".pyproject.toml.*")), (
            "Loading must not leave a lock or temporary sibling behind."
        )

    def test_loader_creates_no_file_when_the_document_is_absent(
        self, tmp_path: Path
    ) -> None:
        """An absent allowlist loads as empty rather than being created."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\n', encoding="utf-8")

        assert not allowlist.load_allowlist(pyproject), (
            "A document without an allow table must load no entries."
        )
        assert list(tmp_path.iterdir()) == [pyproject], (
            "Loading must not create sibling files."
        )


def _reap(writer: subprocess.Popen[str]) -> None:
    """Kill *writer* if it is still running, then collect it."""
    if writer.poll() is None:
        writer.kill()
    writer.wait(timeout=10)


def _start_allow_command(
    stack: contextlib.ExitStack, workspace: Path, arguments: tuple[str, ...]
) -> subprocess.Popen[str]:
    """Start one ``allow`` writer for *stack* to reap once it is done.

    Returns
    -------
    subprocess.Popen[str]
        The running writer, which the caller waits for after the lock releases.
    """
    return stack.enter_context(
        subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed copied gate command.
            gate_command(workspace, "allow", *arguments),
            cwd=workspace,
            env=gate_environment(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    )


class TestAppendAllowEntry:
    """Persisting reasoned entries to ``pyproject.toml``."""

    def test_round_trips_unit_and_pair_entries(self, tmp_path: Path) -> None:
        """Appended entries load again and preserve existing TOML content."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
        allowlist.append_allow_entry(
            pyproject,
            keys=("cuprum/a.py",),
            reason="unit reason",
        )
        allowlist.append_allow_entry(
            pyproject,
            keys=("cuprum/b.py::beta", "cuprum/c.py::gamma"),
            reason="members reason",
        )
        allowlist.append_allow_entry(
            pyproject,
            keys=("cuprum/c.py::gamma", "cuprum/b.py::beta"),
            reason="updated members reason",
        )

        entries = allowlist.load_allowlist(pyproject)
        assert entries[0].keys == ("cuprum/a.py",), "Unit entry must retain its target."
        assert entries[1].keys == ("cuprum/b.py::beta", "cuprum/c.py::gamma"), (
            "Members entry must retain both targets."
        )
        assert entries[1].reason == "updated members reason", (
            "A repeated member set must update its reason."
        )
        assert len(entries) == 2, "A repeated member set must not add an entry."
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        assert data["project"]["name"] == "x", (
            "Appending must preserve existing TOML content."
        )

    def test_unparsable_document_raises_a_configuration_error(
        self, tmp_path: Path
    ) -> None:
        """An unparsable manifest is a configuration error, not a traceback."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("this is not = = valid toml\n", encoding="utf-8")

        with pytest.raises(allowlist.GateConfigError) as raised:
            allowlist.append_allow_entry(
                pyproject,
                keys=("cuprum/a.py",),
                reason="unit reason",
            )

        assert isinstance(raised.value.__cause__, tomlkit.exceptions.ParseError), (
            "The parse failure must be preserved as the cause."
        )
        assert pyproject.read_text(encoding="utf-8") == (
            "this is not = = valid toml\n"
        ), "A rejected document must be left untouched."

    def test_atomic_write_preserves_mode_and_original_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failed replacement retains contents, mode, and a usable lock."""
        pyproject = tmp_path / "pyproject.toml"
        original = '[project]\nname = "x"\nversion = "0"\n'
        pyproject.write_text(original, encoding="utf-8")
        pyproject.chmod(0o640)
        original_replace = Path.replace

        def fail_replace(_source: Path, _destination: Path) -> None:
            """Simulate a replacement that fails part way through the write."""
            msg = "replacement failed"
            raise OSError(msg)

        monkeypatch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError, match="replacement failed"):
            allowlist.append_allow_entry(
                pyproject,
                keys=("cuprum/a.py",),
                reason="unit reason",
            )
        monkeypatch.setattr(Path, "replace", original_replace)

        assert pyproject.read_text(encoding="utf-8") == original, (
            "Failed replacement must preserve the original TOML."
        )
        temporary_siblings = [
            path
            for path in tmp_path.glob(".pyproject.toml.*")
            if path.name != ".pyproject.toml.duplication-gate.lock"
        ]
        assert temporary_siblings == [], (
            "Failed replacement must remove its temporary sibling."
        )
        allowlist.append_allow_entry(
            pyproject,
            keys=("cuprum/a.py",),
            reason="unit reason",
        )
        assert pyproject.stat().st_mode & 0o777 == 0o640, (
            "Replacement must preserve the destination mode."
        )

    def test_concurrent_allow_commands_preserve_both_entries(
        self, tmp_path: Path
    ) -> None:
        """Two blocked writers retain both exceptions after the lock releases."""
        workspace = copied_gate_workspace(tmp_path)
        # Each writer must be *started* while the lock is held so that both are
        # contending when it releases, and *waited for* only afterwards: a
        # writer blocked on the lock cannot exit, so waiting inside the block
        # would deadlock the test against its own lock. The ExitStack holds the
        # writers open across the lock's release, and `_reap` collects them
        # afterwards on every path, including a failed assertion.
        stack = contextlib.ExitStack()
        with allowlist._locked_file(workspace / "pyproject.toml"):
            first = _start_allow_command(
                stack, workspace, ("--first", "cuprum/a.py", "--reason", "first")
            )
            second = _start_allow_command(
                stack, workspace, ("--first", "cuprum/b.py", "--reason", "second")
            )
            assert first.poll() is None, "First writer must wait for the lock."
            assert second.poll() is None, "Second writer must wait for the lock."
            stack.pop_all()  # Keep both writers running past the lock release.

        try:
            assert first.wait(timeout=10) == 0, "First writer must exit successfully."
            assert second.wait(timeout=10) == 0, "Second writer must exit successfully."
        finally:
            _reap(first)
            _reap(second)

        entries = tomllib.loads(
            (workspace / "pyproject.toml").read_text(encoding="utf-8")
        )["tool"]["duplication_gate"]["allow"]
        recorded_units = {entry["unit"] for entry in entries}
        assert recorded_units == {"cuprum/a.py", "cuprum/b.py"}, (
            "Concurrent commands must preserve both independently added entries."
        )
