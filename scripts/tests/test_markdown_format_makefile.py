"""Exercise the Makefile-owned Markdown formatting pipeline.

The tests run ``make fmt`` against controlled executables so they prove the
repository's discovery, ordering, and failure behaviour without formatting the
checkout that runs the tests.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import typing as typ
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from scripts.markdown_format_test_support import (
    create_format_gate_repository,
    run_format_gate,
    run_process,
    stage_markdown_sources,
    write_markdown_formatter_stubs,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

FILENAME_ALPHABET = tuple("abcde12345 -_\néü")


def _read_calls(call_log: Path) -> list[dict[str, object]]:
    """Read the ordered formatter-double calls recorded by one recipe run."""
    return [
        json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()
    ]


def _formatter_path(path: Path) -> str:
    """Return the path form that cannot be mistaken for a tool option."""
    source = path.as_posix()
    return f"./{source}" if source.startswith("-") else source


def _assert_two_stage_formatter_calls(
    calls: list[dict[str, object]], expected_paths: list[str]
) -> None:
    """Assert the Make pipeline invokes both formatters over the same inputs."""
    assert [call["tool"] for call in calls] == ["mdtablefix", "markdownlint-cli2"], (
        "the Markdown pipeline must run mdtablefix before markdownlint-cli2"
    )
    assert all(call["paths"] == expected_paths for call in calls), (
        "each Markdown formatter must receive precisely the tracked source paths"
    )


def _assert_formatter_only_call(
    calls: list[dict[str, object]], expected_paths: list[str]
) -> None:
    """Assert the failed first stage did not permit the linter stage to run."""
    assert len(calls) == 1, "a first-stage failure must record only one tool call"
    assert calls[0]["tool"] == "mdtablefix", (
        "the first-stage failure must be recorded as an mdtablefix invocation"
    )
    assert calls[0]["paths"] == expected_paths, (
        "the failed formatter must receive every tracked Markdown source"
    )


def test_fmt_formats_each_tracked_markdown_extension_in_order(
    tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    """Format tracked Markdown once before lint-fixing the identical file set."""
    repository, tracked_files, _ = create_format_gate_repository(tmp_path)
    stage_markdown_sources(repository, tracked_files)
    for path in tracked_files:
        (repository / path).write_text("unformatted\n", encoding="utf-8")
    (repository / "untracked.md").write_text("unformatted\n", encoding="utf-8")
    (repository / "ignored.md").write_text("unformatted\n", encoding="utf-8")
    tools = write_markdown_formatter_stubs(repository)

    result = run_format_gate(repository, tools)

    assert result.returncode == 0, result.stdout + result.stderr
    expected_paths = sorted(_formatter_path(path) for path in tracked_files)
    calls = _read_calls(tools.call_log)
    assert calls == snapshot, (
        "the stable formatter transcript must preserve both tool names and inputs"
    )
    _assert_two_stage_formatter_calls(calls, expected_paths)
    assert all(
        (repository / path).read_text(encoding="utf-8") == "formatted\n"
        for path in tracked_files
    ), "the formatter must update every tracked Markdown source before linting"
    assert (repository / "untracked.md").read_text(
        encoding="utf-8"
    ) == "unformatted\n", (
        "untracked Markdown must remain outside the formatter input set"
    )
    assert (repository / "ignored.md").read_text(encoding="utf-8") == "unformatted\n", (
        "ignored Markdown must remain outside the formatter input set"
    )


def test_fmt_excludes_tracked_symlink_aliases(
    tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    """Leave tracked symlink aliases outside the regular-file formatter contract."""
    repository, tracked_files, _ = create_format_gate_repository(tmp_path)
    stage_markdown_sources(repository, tracked_files)
    alias = repository / "guide-alias.md"
    try:
        alias.symlink_to("guide.md")
    except OSError as error:
        pytest.skip(f"the platform cannot create the tracked symlink fixture: {error}")
    git = shutil.which("git")
    assert git is not None, "the formatter contract test requires Git"
    staged = run_process([git, "add", alias.name], os.environ, repository)
    assert staged.returncode == 0, staged.stdout + staged.stderr
    tools = write_markdown_formatter_stubs(repository)

    result = run_format_gate(repository, tools)

    assert result.returncode == 0, result.stdout + result.stderr
    expected_paths = sorted(_formatter_path(path) for path in tracked_files)
    calls = _read_calls(tools.call_log)
    assert calls == snapshot, (
        "the symlink-safe formatter transcript must preserve both tool stages"
    )
    _assert_two_stage_formatter_calls(calls, expected_paths)


@given(component=st.text(FILENAME_ALPHABET, min_size=1, max_size=20))
@example(component="-leading-option")
@example(component="space name")
@example(component="line\nbreak")
@example(component="naïve")
@settings(deadline=None, max_examples=20)
def test_fmt_round_trips_valid_tracked_filename_components(
    component: str,
) -> None:
    """Pass valid Git filename components to both stages without option parsing."""
    with tempfile.TemporaryDirectory() as temporary_directory:
        source = Path(f"{component}.md")
        repository, tracked_files, _ = create_format_gate_repository(
            Path(temporary_directory),
            (source,),
        )
        stage_markdown_sources(repository, tracked_files)
        (repository / source).write_text("unformatted\n", encoding="utf-8")
        untracked = repository / "untracked-no-format.md"
        untracked.write_text("unformatted\n", encoding="utf-8")
        tools = write_markdown_formatter_stubs(repository)

        result = run_format_gate(repository, tools)

        assert result.returncode == 0, result.stdout + result.stderr
        expected_paths = [_formatter_path(source)]
        _assert_two_stage_formatter_calls(_read_calls(tools.call_log), expected_paths)
        assert (repository / source).read_text(encoding="utf-8") == "formatted\n", (
            "the tracked generated filename must be formatted"
        )
        assert untracked.read_text(encoding="utf-8") == "unformatted\n", (
            "the untracked generated filename must not be formatted"
        )


def test_fmt_skips_markdown_tools_when_git_tracks_no_markdown(tmp_path: Path) -> None:
    """Avoid invoking either tool without a tracked source to format."""
    repository, tracked_files, _ = create_format_gate_repository(tmp_path, ())
    stage_markdown_sources(repository, tracked_files)
    tools = write_markdown_formatter_stubs(repository)

    result = run_format_gate(repository, tools)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not tools.call_log.exists(), "the Markdown tools must not run without inputs"


def test_fmt_rejects_markdown_discovery_failure_before_tools_run(
    tmp_path: Path,
) -> None:
    """Fail closed instead of allowing a failed Git walk to reach the tools."""
    repository, tracked_files, _ = create_format_gate_repository(tmp_path)
    stage_markdown_sources(repository, tracked_files)
    for path in tracked_files:
        (repository / path).write_text("unformatted\n", encoding="utf-8")
    tools = write_markdown_formatter_stubs(repository)

    result = run_format_gate(
        repository,
        tools,
        markdown_discovery="false",
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert not tools.call_log.exists(), (
        "the Markdown tools must not run after discovery fails"
    )


def test_fmt_propagates_the_first_formatter_failure(tmp_path: Path) -> None:
    """Stop before lint-fixing when the canonical formatter fails."""
    repository, tracked_files, _ = create_format_gate_repository(tmp_path)
    stage_markdown_sources(repository, tracked_files)
    for path in tracked_files:
        (repository / path).write_text("unformatted\n", encoding="utf-8")
    tools = write_markdown_formatter_stubs(repository)
    tools.formatter.write_text("#!/bin/sh\nexit 67\n", encoding="utf-8")
    tools.formatter.chmod(0o755)

    result = run_format_gate(repository, tools)

    assert result.returncode != 0, result.stdout + result.stderr
    assert not tools.call_log.exists(), (
        "the linter must not run after formatter failure"
    )
    assert all(
        (repository / path).read_text(encoding="utf-8") == "unformatted\n"
        for path in tracked_files
    ), "formatter failure must leave every tracked Markdown source unchanged"


def test_fmt_propagates_the_linter_failure_after_formatting(tmp_path: Path) -> None:
    """Report the second-stage failure after canonical formatting completes."""
    repository, tracked_files, _ = create_format_gate_repository(tmp_path)
    stage_markdown_sources(repository, tracked_files)
    tools = write_markdown_formatter_stubs(repository)
    tools.linter.write_text("#!/bin/sh\nexit 68\n", encoding="utf-8")
    tools.linter.chmod(0o755)

    result = run_format_gate(repository, tools)

    assert result.returncode != 0, result.stdout + result.stderr
    _assert_formatter_only_call(
        _read_calls(tools.call_log),
        sorted(_formatter_path(path) for path in tracked_files),
    )
    assert all(
        (repository / path).read_text(encoding="utf-8") == "formatted\n"
        for path in tracked_files
    ), "the successful first stage must format every tracked Markdown source"
