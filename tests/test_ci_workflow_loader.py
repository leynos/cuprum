"""Tests for the workflow loader's failure boundary.

Every placement, event and cache query reaches the filesystem through
`read_source` and `read_workflow`. These drive both with real files under a
temporary directory, because this repository's own workflows are readable and
valid, so a contract parametrized over them cannot show what a read or parse
failure looks like.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.ci_workflows import read_source, read_workflow, workflow_sources

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_a_missing_workflow_fails_naming_the_file(tmp_path: Path) -> None:
    """A workflow that does not exist is a contract failure citing its name."""
    with pytest.raises(AssertionError, match=r"absent\.yml could not be read"):
        read_workflow(tmp_path / "absent.yml")


def test_an_unreadable_source_fails_naming_the_file(tmp_path: Path) -> None:
    """A path that cannot be read as text fails as a contract, not an OSError.

    A directory stands in for an unreadable file: reading it raises an
    ``OSError`` on every platform, without depending on permission bits the
    test process may be privileged enough to ignore.
    """
    unreadable = tmp_path / "unreadable.yml"
    unreadable.mkdir()
    with pytest.raises(AssertionError, match=r"unreadable\.yml could not be read"):
        read_source(unreadable)


def test_invalid_yaml_fails_naming_the_file(tmp_path: Path) -> None:
    """A parser error is translated and attributed to the file that caused it.

    The case is genuinely unparsable. A document that parses but has the
    wrong shape exercises only the mapping check, which is covered separately.
    """
    broken = tmp_path / "broken.yml"
    broken.write_text("jobs: [unclosed\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"broken\.yml is not valid YAML"):
        read_workflow(broken)


def test_a_document_that_is_not_a_mapping_fails_naming_the_file(
    tmp_path: Path,
) -> None:
    """Valid YAML of the wrong shape is refused with the file's name."""
    listed = tmp_path / "listed.yml"
    listed.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"listed\.yml must parse to a mapping"):
        read_workflow(listed)


def test_a_valid_workflow_keeps_its_boolean_trigger_key(tmp_path: Path) -> None:
    """A readable workflow parses unnarrowed, with YAML 1.1's `on` as `True`."""
    valid = tmp_path / "valid.yml"
    valid.write_text("on: push\njobs: {}\n", encoding="utf-8")
    parsed = read_workflow(valid)
    assert parsed == {True: "push", "jobs": {}}, (
        f"valid.yml should parse unnarrowed, got {parsed!r}"
    )


def test_the_source_sweep_reads_through_the_named_boundary(tmp_path: Path) -> None:
    """The sweep over every workflow fails by name, not with a bare OSError.

    `workflow_sources` feeds the text-level contracts, so it must use the same
    boundary as the parsed readers. A directory matching the glob stands in
    for a workflow that cannot be read.
    """
    (tmp_path / "ci.yml").write_text("on: push\n", encoding="utf-8")
    (tmp_path / "stuck.yml").mkdir()
    with pytest.raises(AssertionError, match=r"stuck\.yml could not be read"):
        workflow_sources(tmp_path)


@pytest.mark.parametrize("shape", ["missing", "file", "empty"])
def test_the_source_sweep_refuses_a_directory_it_cannot_sweep(
    tmp_path: Path, shape: str
) -> None:
    """A sweep that finds nothing fails instead of reporting a clean estate.

    ``Path.glob`` on a missing directory returns an empty list, and every
    "no workflow does X" contract would then pass having read nothing. A path
    that is a file, and a directory holding no workflow, are the same hazard.
    """
    target = tmp_path / "workflows"
    match shape:
        case "file":
            target.write_text("", encoding="utf-8")
        case "empty":
            target.mkdir()
        case _:
            pass
    with pytest.raises(AssertionError, match=r"workflow directory|holds no workflow"):
        workflow_sources(target)
