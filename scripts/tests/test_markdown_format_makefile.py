"""Exercise the Markdown Makefile pipeline at its process boundary."""

from __future__ import annotations

import os
import re
import shutil
import typing as typ
from pathlib import Path

import pytest

from scripts.markdown_format_test_support import (
    REPOSITORY_ROOT,
    MarkdownTargetOptions,
    read_calls,
    run_markdown_target,
    run_process,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

    from scripts.tests.conftest import MarkdownFormatGate, MarkdownFormatGateFactory


def _selected_paths(gate: MarkdownFormatGate) -> list[str]:
    """Return the expected logical regular paths from the controlled fixture."""
    paths = [*gate.tracked_files]
    paths.extend((
        Path("untracked.md"),
        Path("untracked.markdown"),
        Path("nested/untracked.mdx"),
    ))
    return sorted(path.as_posix() for path in paths)


def _safe_path(path: Path) -> str:
    """Prefix leading dashes so a tool cannot parse a path as an option."""
    source = path.as_posix()
    return f"./{source}" if source.startswith("-") else source


def _add_symlink(repository: Path) -> Path:
    """Create and stage a Markdown symlink alias for the selection contract."""
    alias = repository / "guide-alias.md"
    try:
        alias.symlink_to("guide.md")
    except OSError as error:
        pytest.skip(f"the platform cannot create the symlink fixture: {error}")
    git = shutil.which("git")
    assert git is not None, "the Markdown format contract requires Git"
    result = run_process([git, "add", "--", alias.name], os.environ, repository)
    assert result.returncode == 0, result.stdout + result.stderr
    return alias


def _markdown_make_contract(makefile: str) -> dict[str, object]:
    """Extract the stable Markdown selection contract from Make assignments."""
    assignments = dict(
        re.findall(
            r"^(MARKDOWN_GLOBS|MDTABLEFIX_EXTENSIONS|MDLINT_FILES_FIND|"
            r"MDLINT_FIX_COMMAND|MDLINT_CHECK_COMMAND)\s*=\s*(.+)$",
            makefile,
            flags=re.MULTILINE,
        )
    )
    selector = assignments["MDLINT_FILES_FIND"]
    return {
        "native_extensions": assignments["MDTABLEFIX_EXTENSIONS"],
        "linter_extensions": tuple(
            re.findall(r"'\*\.([a-z]+)'", assignments["MARKDOWN_GLOBS"])
        ),
        "selector_flags": tuple(
            flag
            for flag in ("--cached", "--others", "--exclude-standard")
            if flag in selector
        ),
        "regular_files_only": '[ -f "$$markdown_file" ]' in selector,
        "symlinks_excluded": '[ ! -L "$$markdown_file" ]' in selector,
        "leading_dash_safe": 'printf "./%s\\0"' in selector,
        "linter_commands": {
            "fix": assignments["MDLINT_FIX_COMMAND"],
            "check": assignments["MDLINT_CHECK_COMMAND"],
        },
    }


def test_fmt_stages_native_formatter_before_linter_over_the_same_files(
    markdown_format_gate: MarkdownFormatGateFactory,
) -> None:
    """Format every selected extension before the linter receives it."""
    gate = markdown_format_gate()

    result = run_markdown_target(
        gate.repository,
        "fmt",
        gate.tools,
        MarkdownTargetOptions(
            environment={"MARKDOWN_FORMAT_EXPECT_LINTER_FIX": "true"}
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = read_calls(gate.tools.call_log)
    assert [call["tool"] for call in calls] == ["mdtablefix", "markdownlint-cli2"], (
        f"fmt must run mdtablefix before markdownlint-cli2, got {calls!r}"
    )
    expected_paths = _selected_paths(gate)
    assert calls[0]["paths"] == expected_paths, (
        f"mdtablefix must receive every native-selected path, got {calls[0]!r}"
    )
    assert calls[1]["paths"] == [_safe_path(Path(path)) for path in expected_paths], (
        f"markdownlint must receive the same option-safe path set, got {calls[1]!r}"
    )


def test_fmt_and_linter_exclude_ignored_and_symlink_paths(
    markdown_format_gate: MarkdownFormatGateFactory,
) -> None:
    """Keep wrapper-excluded paths out of both formatter stages."""
    gate = markdown_format_gate()
    alias = _add_symlink(gate.repository)

    result = run_markdown_target(
        gate.repository,
        "fmt",
        gate.tools,
        MarkdownTargetOptions(
            environment={"MARKDOWN_FORMAT_EXPECT_LINTER_FIX": "true"}
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    expected_paths = _selected_paths(gate)
    for call in read_calls(gate.tools.call_log):
        logical_paths = [str(path).removeprefix("./") for path in call["paths"]]
        assert logical_paths == expected_paths, (
            f"{call['tool']} selected unexpected paths: {call['paths']!r}"
        )
        assert alias.as_posix() not in call["paths"], (
            f"{call['tool']} must exclude the symlink alias: {call['paths']!r}"
        )
        assert "ignored.md" not in call["paths"], (
            f"{call['tool']} must exclude ignored files: {call['paths']!r}"
        )


def test_linter_selector_matches_native_mdtablefix_file_selection(
    markdown_format_gate: MarkdownFormatGateFactory,
) -> None:
    """Keep the NUL-safe linter selector aligned with mdtablefix's native list."""
    gate = markdown_format_gate((Path("tracked.md"), Path("nested/tracked.mdx")))
    _add_symlink(gate.repository)
    for source_path in _selected_paths(gate):
        (gate.repository / source_path).write_text("formatted\n", encoding="utf-8")

    lint_result = run_markdown_target(gate.repository, "markdownlint", gate.tools)
    native_formatter = shutil.which("mdtablefix")
    assert native_formatter is not None, (
        "the Markdown format contract requires mdtablefix"
    )
    native_result = run_process(
        [
            native_formatter,
            "--list-files",
            "--git",
            "--include-untracked",
            "--md-exts",
            "md,markdown,mdx",
        ],
        os.environ,
        gate.repository,
    )

    assert lint_result.returncode == 0, lint_result.stdout + lint_result.stderr
    assert native_result.returncode == 0, native_result.stdout + native_result.stderr
    linter_paths = read_calls(gate.tools.call_log)[0]["paths"]
    assert native_result.stdout.splitlines() == [
        path.removeprefix("./") for path in linter_paths
    ], f"markdownlint selector diverged from mdtablefix: {linter_paths!r}"


@pytest.mark.parametrize(
    ("failure_variable", "expected_tools"),
    [
        pytest.param(
            "MARKDOWN_FORMAT_FORMATTER_FAILURE",
            ["mdtablefix"],
            id="native-formatter",
        ),
        pytest.param(
            "MARKDOWN_FORMAT_LINTER_FAILURE",
            ["mdtablefix", "markdownlint-cli2"],
            id="markdown-linter",
        ),
    ],
)
def test_fmt_propagates_formatter_stage_failures(
    markdown_format_gate: MarkdownFormatGateFactory,
    failure_variable: str,
    expected_tools: list[str],
) -> None:
    """Preserve the formatter pipeline's failing-stage call sequence."""
    gate = markdown_format_gate()

    result = run_markdown_target(
        gate.repository,
        "fmt",
        gate.tools,
        MarkdownTargetOptions(
            environment={
                "MARKDOWN_FORMAT_EXPECT_LINTER_FIX": "true",
                failure_variable: "67",
            }
        ),
    )

    assert result.returncode == 2, result.stdout + result.stderr
    actual_tools = [call["tool"] for call in read_calls(gate.tools.call_log)]
    assert actual_tools == expected_tools, (
        f"{failure_variable} must stop fmt after {expected_tools!r}, "
        f"got {actual_tools!r}"
    )


def test_markdownlint_skips_empty_input_and_fails_closed_on_discovery_error(
    markdown_format_gate: MarkdownFormatGateFactory,
) -> None:
    """Do not invoke the linter for no files or a failed file discovery."""
    empty_gate = markdown_format_gate(())
    for path in ("untracked.md", "untracked.markdown", "nested/untracked.mdx"):
        (empty_gate.repository / path).unlink()
    empty_result = run_markdown_target(
        empty_gate.repository, "markdownlint", empty_gate.tools
    )
    assert empty_result.returncode == 0, empty_result.stdout + empty_result.stderr
    assert not empty_gate.tools.call_log.exists(), (
        "markdownlint must not run when no Markdown files are selected"
    )

    failed_gate = markdown_format_gate()
    failed_result = run_markdown_target(
        failed_gate.repository,
        "markdownlint",
        failed_gate.tools,
        MarkdownTargetOptions(discovery="false"),
    )
    assert failed_result.returncode == 2, failed_result.stdout + failed_result.stderr
    assert not failed_gate.tools.call_log.exists(), (
        "markdownlint must not run after its selector fails"
    )


def test_markdownlint_propagates_a_linter_failure(
    markdown_format_gate: MarkdownFormatGateFactory,
) -> None:
    """Return the linter error after it receives every selected path."""
    gate = markdown_format_gate()

    result = run_markdown_target(
        gate.repository,
        "markdownlint",
        gate.tools,
        MarkdownTargetOptions(environment={"MARKDOWN_FORMAT_LINTER_FAILURE": "68"}),
    )

    assert result.returncode == 2, result.stdout + result.stderr
    calls = read_calls(gate.tools.call_log)
    assert calls[0]["tool"] == "markdownlint-cli2", (
        f"the linter failure must originate from markdownlint-cli2, got {calls!r}"
    )
    assert calls[0]["paths"] == [
        _safe_path(Path(path)) for path in _selected_paths(gate)
    ], f"markdownlint must receive every selected path, got {calls[0]!r}"


def test_check_fmt_is_nonmutating_and_uses_native_selection(
    markdown_format_gate: MarkdownFormatGateFactory,
) -> None:
    """Retain native mdtablefix's check-only contract for the worktree."""
    gate = markdown_format_gate()
    source = gate.repository / "guide.md"
    source.write_bytes(b"unformatted\r\n")
    before = source.read_bytes()

    result = run_markdown_target(gate.repository, "check-fmt", gate.tools)

    assert result.returncode == 0, result.stdout + result.stderr
    assert source.read_bytes() == before, "check-fmt must not rewrite CRLF input"
    calls = read_calls(gate.tools.call_log)
    assert [call["tool"] for call in calls] == ["mdtablefix"], (
        f"check-fmt must invoke only mdtablefix, got {calls!r}"
    )
    assert "--check" in calls[0]["args"], (
        f"check-fmt must use mdtablefix's nonmutating mode, got {calls[0]!r}"
    )


def test_makefile_uses_local_tools_and_the_shared_linter_selector(
    snapshot: SnapshotAssertion,
) -> None:
    """Keep each local Markdown command on the one NUL-safe selector contract."""
    makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
    contract = _markdown_make_contract(makefile)

    assert contract["native_extensions"] == "--md-exts md,markdown,mdx", (
        "native mdtablefix must select every supported Markdown extension"
    )
    assert contract["linter_extensions"] == ("md", "markdown", "mdx"), (
        "markdownlint must select the same extensions as mdtablefix"
    )
    assert contract["selector_flags"] == (
        "--cached",
        "--others",
        "--exclude-standard",
    ), "the linter selector must include untracked but exclude ignored files"
    assert all(
        contract[key]
        for key in ("regular_files_only", "symlinks_excluded", "leading_dash_safe")
    ), "the linter selector must retain regular-file, symlink, and option safety"
    assert contract == snapshot, "the Markdown Makefile contract snapshot drifted"
