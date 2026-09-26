"""Tests for the code-duplication gate helper script.

The gate drives the pinned ``nose`` binary; these tests exercise the
allowlist, matching, and partitioning logic without invoking it. The
``make duplication-test`` target runs them on the repository interpreter.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_duplication_gate.py``), the merged revision of PR #276,
under the ISC terms in ``LICENSE``.
"""

from __future__ import annotations

import re
import textwrap
import typing as typ

import pytest

from scripts.tests.duplication_gate_test_support import allowlist, detector, gate

if typ.TYPE_CHECKING:
    from pathlib import Path


def _location(
    file: str = "cuprum/a.py",
    start: int = 1,
    end: int = 20,
    name: str | None = None,
) -> detector.Location:
    """Build one reported location."""
    return detector.Location(file=file, start=start, end=end, name=name)


def _finding(*locations: detector.Location, value: float = 22.1) -> detector.Finding:
    """Build a finding over the supplied locations."""
    members = locations or (_location(), _location(file="cuprum/b.py"))
    return detector.Finding(witness="copy-paste", value=value, locations=members)


class TestKeyMatching:
    """Path-glob and unit-name semantics for allow keys."""

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param("cuprum/a.py", id="exact-path"),
            pytest.param("cuprum/*.py", id="glob-path"),
            pytest.param("cuprum/**/*.py", id="recursive-glob"),
            pytest.param("cuprum/a.py::run", id="matching-name"),
            pytest.param("cuprum/*.py::run", id="glob-path-and-name"),
        ],
    )
    def test_matching_keys_cover_the_location(self, key: str) -> None:
        """Keys match on the path glob and, when given, the unit name."""
        location = _location(name="run")
        assert allowlist.key_matches(key, location), (
            f"Key {key!r} must cover {location.file}."
        )

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param("cuprum/b.py", id="other-path"),
            pytest.param("cuprum/a.py::other", id="other-name"),
            pytest.param("cuprum/nested/*.py", id="other-directory"),
        ],
    )
    def test_unrelated_keys_do_not_cover_the_location(self, key: str) -> None:
        """Keys naming another path or unit leave the location uncovered."""
        location = _location(name="run")
        assert not allowlist.key_matches(key, location), (
            f"Key {key!r} must not cover {location.file}."
        )

    def test_named_keys_never_match_fragments(self) -> None:
        """A ``::name`` key cannot silence an unnamed fragment finding."""
        assert not allowlist.key_matches("cuprum/a.py::run", _location()), (
            "Named keys must not match locations nose reported without a name."
        )


class TestAllowEntry:
    """Coverage semantics for unit and members allow entries."""

    def test_unit_entry_requires_every_location(self) -> None:
        """A unit entry silences a family only when it covers every member."""
        entry = allowlist.AllowEntry(keys=("cuprum/a.py",), reason="r")
        assert entry.matches(_finding(_location(), _location(start=40, end=60))), (
            "A single-file family must be covered by its file key."
        )
        assert not entry.matches(_finding()), (
            "A family reaching an uncovered file must keep blocking."
        )

    def test_members_entry_covers_each_listed_key(self) -> None:
        """A members entry covers families whose members it all names."""
        entry = allowlist.AllowEntry(
            keys=("cuprum/a.py", "cuprum/b.py"),
            reason="r",
        )
        assert entry.matches(_finding()), "Listed members must silence the family."
        assert not entry.matches(
            _finding(_location(), _location(file="cuprum/c.py"))
        ), "An unlisted third member must keep the family blocking."


class TestValidateKey:
    """Allow-key validation at the configuration boundary."""

    @pytest.mark.parametrize(
        "key",
        ["cuprum/a.py", "cuprum/*.py::run", "cuprum/**/models.py"],
    )
    def test_accepts_well_formed_keys(self, key: str) -> None:
        """Well-formed keys round-trip unchanged."""
        assert allowlist.validate_key(key, context="key") == key, (
            "Validation must return the key unchanged."
        )

    @pytest.mark.parametrize(
        ("key", "diagnostic"),
        [
            pytest.param("", "must be a 'path' or 'path::name' key", id="empty"),
            pytest.param("::run", "must be a 'path' or 'path::name' key", id="no-path"),
            pytest.param(
                "cuprum/a.py::",
                "must be a 'path' or 'path::name' key",
                id="empty-name",
            ),
            pytest.param(
                "/cuprum/a.py",
                "must be a repository-relative path key",
                id="absolute",
            ),
            pytest.param(
                "../secrets.py",
                "must be a repository-relative path key",
                id="parent-escape",
            ),
            pytest.param(
                "cuprum/../private.py",
                "must be a repository-relative path key",
                id="interior-parent-escape",
            ),
        ],
    )
    def test_rejects_malformed_keys(self, key: str, diagnostic: str) -> None:
        """Malformed keys raise a configuration error."""
        with pytest.raises(gate.GateConfigError, match=re.escape(diagnostic)):
            allowlist.validate_key(key, context="key")


class TestLoadAllowlist:
    """Allowlist parsing and validation."""

    def _write(self, tmp_path: Path, body: str) -> Path:
        """Write ``body`` to ``pyproject.toml`` under ``tmp_path``."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent(body), encoding="utf-8")
        return pyproject

    def test_loads_unit_and_members_entries(self, tmp_path: Path) -> None:
        """Unit and members entries load with their reasons."""
        pyproject = self._write(
            tmp_path,
            """\
            [[tool.duplication_gate.allow]]
            unit = "cuprum/a.py"
            reason = "declarative"

            [[tool.duplication_gate.allow]]
            members = ["cuprum/b.py::beta", "cuprum/c.py::gamma"]
            reason = "parallel contracts"
            """,
        )
        entries = allowlist.load_allowlist(pyproject)
        assert entries[0].keys == ("cuprum/a.py",), "Unit entry must retain its target."
        assert entries[1].keys == ("cuprum/b.py::beta", "cuprum/c.py::gamma"), (
            "Members entry must retain every target."
        )
        assert entries[1].reason == "parallel contracts", (
            "Allow entry must retain its reason."
        )

    def test_missing_gate_table_yields_empty_allowlist(self, tmp_path: Path) -> None:
        """A pyproject without the gate table produces no entries."""
        pyproject = self._write(tmp_path, "[project]\nname = 'x'\nversion = '0'\n")
        assert allowlist.load_allowlist(pyproject) == (), (
            "Missing gate table must mean no allow entries."
        )

    @pytest.mark.parametrize(
        ("body", "diagnostic"),
        [
            (
                '[[tool.duplication_gate.allow]]\nunit = "cuprum/a.py"\n',
                "requires a non-empty reason",
            ),
            (
                '[[tool.duplication_gate.allow]]\nunit = "/a.py"\nreason = "r"\n',
                "must be a repository-relative path key",
            ),
            (
                '[[tool.duplication_gate.allow]]\nmembers = ["a.py"]\nreason = "r"\n',
                "members must be two or more 'path[::name]' strings",
            ),
            (
                '[[tool.duplication_gate.allow]]\nmembers = "a.py"\nreason = "r"\n',
                "members must be two or more 'path[::name]' strings",
            ),
            (
                '[[tool.duplication_gate.allow]]\nunit = 42\nreason = "r"\n',
                "unit must be a 'path[::name]' string",
            ),
            (
                (
                    "[[tool.duplication_gate.allow]]\n"
                    'members = ["cuprum/a.py", 7]\nreason = "r"\n'
                ),
                "members must be two or more 'path[::name]' strings",
            ),
            (
                '[[tool.duplication_gate.allow]]\nreason = "r"\n',
                "must set exactly one of 'unit' or 'members'",
            ),
            (
                (
                    '[[tool.duplication_gate.allow]]\nunit = "cuprum/a.py"\n'
                    'members = ["cuprum/a.py", "cuprum/b.py"]\nreason = "r"\n'
                ),
                "must set exactly one of 'unit' or 'members'",
            ),
        ],
        ids=[
            "no-reason",
            "absolute-unit",
            "one-member",
            "string-members",
            "non-string-unit",
            "non-string-member",
            "no-target",
            "both-kinds",
        ],
    )
    def test_rejects_malformed_entries(
        self,
        tmp_path: Path,
        body: str,
        diagnostic: str,
    ) -> None:
        """Malformed entries raise a configuration error."""
        pyproject = self._write(tmp_path, body)
        with pytest.raises(gate.GateConfigError, match=re.escape(diagnostic)):
            allowlist.load_allowlist(pyproject)


class TestEntryKeyHelpers:
    """Direct contracts for the unit and members key helpers.

    TOML cannot express a bytes value, so the bytes rejection that guards
    ``members`` is only reachable through the helper itself.
    """

    def test_unit_entry_keys_validates_one_key(self) -> None:
        """A well-formed unit becomes a single-key tuple."""
        assert allowlist._unit_entry_keys("cuprum/a.py::run", context="entry") == (
            "cuprum/a.py::run",
        ), "A valid unit must yield exactly one validated key."

    def test_members_entry_keys_preserves_order(self) -> None:
        """Member keys keep their configured order."""
        members = ["cuprum/b.py", "cuprum/a.py"]
        assert allowlist._members_entry_keys(members, context="entry") == (
            "cuprum/b.py",
            "cuprum/a.py",
        ), "Member order must survive validation."

    @pytest.mark.parametrize(
        "members",
        [
            pytest.param("cuprum/a.py", id="string"),
            pytest.param(b"cuprum/a.py", id="bytes"),
        ],
    )
    def test_members_entry_keys_rejects_string_like_values(
        self, members: object
    ) -> None:
        """String and bytes values are never treated as member sequences."""
        with pytest.raises(
            gate.GateConfigError,
            match=re.escape("entry members must be two or more 'path[::name]' strings"),
        ):
            allowlist._members_entry_keys(members, context="entry")


class TestPartitionFindings:
    """Blocking, allowed, and stale-entry partitioning."""

    def test_unmatched_findings_block(self) -> None:
        """A finding with no matching entry blocks the gate."""
        blocking, allowed, stale = gate.partition_findings([_finding()], [])
        assert len(blocking) == 1, "Unmatched finding must block."
        assert not allowed, "Unmatched finding must not be allowed."
        assert not stale, "Empty allowlist must not yield stale entries."

    def test_matched_findings_are_allowed(self) -> None:
        """Entries silence their findings and are not reported stale."""
        entry = allowlist.AllowEntry(keys=("cuprum/*.py",), reason="r")
        blocking, allowed, stale = gate.partition_findings([_finding()], [entry])
        assert not blocking, "Matching entry must prevent blocking."
        assert len(allowed) == 1, "Matching entry must allow the finding."
        assert not stale, "Used entry must not be stale."

    def test_unused_entries_are_stale(self) -> None:
        """Entries matching nothing are reported for removal."""
        entry = allowlist.AllowEntry(keys=("cuprum/gone.py",), reason="r")
        blocking, _allowed, stale = gate.partition_findings([_finding()], [entry])
        assert len(blocking) == 1, "Unmatched finding must remain blocking."
        assert stale == [entry], "Unused allow entry must be stale."
