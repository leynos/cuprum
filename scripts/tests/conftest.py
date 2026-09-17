"""Shared fixtures for Markdown Makefile script tests."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from scripts.markdown_format_test_support import (
    MarkdownFormatterTools,
    create_format_gate_repository,
    write_markdown_formatter_stubs,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


@dc.dataclass(frozen=True, slots=True)
class MarkdownFormatGate:
    """Hold the common temporary repository state for Markdown Make tests."""

    repository: Path
    tracked_files: tuple[Path, ...]
    tools: MarkdownFormatterTools


class MarkdownFormatGateFactory(typ.Protocol):
    """Build a Markdown Makefile fixture with optional tracked files."""

    def __call__(
        self, tracked_files: tuple[Path, ...] | None = None
    ) -> MarkdownFormatGate:
        """Return one staged Markdown fixture."""


@pytest.fixture(name="markdown_format_gate")
def markdown_format_gate_fixture(
    tmp_path: Path,
) -> MarkdownFormatGateFactory:
    """Return a factory for staged Markdown repositories and controlled tools."""
    fixture_count = 0

    def create(tracked_files: tuple[Path, ...] | None = None) -> MarkdownFormatGate:
        """Build one Git-backed fixture with the requested tracked files."""
        nonlocal fixture_count
        fixture_directory = tmp_path / str(fixture_count)
        fixture_count += 1
        fixture_directory.mkdir()
        arguments = () if tracked_files is None else (tracked_files,)
        repository, staged_files = create_format_gate_repository(
            fixture_directory, *arguments
        )
        return MarkdownFormatGate(
            repository=repository,
            tracked_files=staged_files,
            tools=write_markdown_formatter_stubs(repository),
        )

    return create
