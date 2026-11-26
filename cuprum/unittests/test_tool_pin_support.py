"""Contract tests for the shared tool-pin readers.

The readers decide what a pin test observed, so the shapes they enforce —
exactly one declaration, an exact release, one capture per site — need
exercising against synthetic files. The repository's own files are all
well formed today, which is precisely why they cannot show that a malformed
one would be refused rather than read as something plausible.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum.unittests._tool_pin_support import read_makefile_pin

if typ.TYPE_CHECKING:
    from pathlib import Path


def _repository(tmp_path: Path, body: str) -> Path:
    """Write ``body`` as a Makefile and return the scratch repository root."""
    (tmp_path / "Makefile").write_text(body, encoding="utf-8")
    return tmp_path


def test_a_plain_default_is_read(tmp_path: Path) -> None:
    """An unadorned ``NAME ?= value`` yields the value."""
    root = _repository(tmp_path, "RUFF_VERSION ?= 0.16.4\n")

    assert read_makefile_pin(root, "RUFF_VERSION") == "0.16.4"


def test_a_trailing_comment_does_not_hide_the_default(tmp_path: Path) -> None:
    """Make discards a trailing comment, so the reader must keep the value."""
    root = _repository(tmp_path, "RUFF_VERSION ?= 0.16.4  # bumped with the group\n")

    assert read_makefile_pin(root, "RUFF_VERSION") == "0.16.4", (
        "a commented default must still be read, or a duplicate could hide behind it"
    )


@pytest.mark.parametrize(
    "body",
    [
        "RUFF_VERSION ?= 9.9.9  # stale\nRUFF_VERSION ?= 0.16.4\n",
        "RUFF_VERSION ?=\nRUFF_VERSION ?= 0.16.4\n",
        "RUFF_VERSION ?= 0.1.0 unexpected\nRUFF_VERSION ?= 0.15.0\n",
        " RUFF_VERSION ?= 0.9.0\nRUFF_VERSION ?= 0.16.4\n",
    ],
    ids=["commented", "empty", "malformed", "indented"],
)
def test_a_duplicate_default_is_refused(tmp_path: Path, body: str) -> None:
    """Make keeps the first assignment whatever its value looks like."""
    root = _repository(tmp_path, body)

    with pytest.raises(AssertionError, match="exactly one RUFF_VERSION default"):
        read_makefile_pin(root, "RUFF_VERSION")


def test_an_absent_default_is_refused(tmp_path: Path) -> None:
    """A reader that found nothing must fail rather than return a guess."""
    root = _repository(tmp_path, "OTHER_VERSION ?= 1.2.3\n")

    with pytest.raises(AssertionError, match="exactly one RUFF_VERSION default"):
        read_makefile_pin(root, "RUFF_VERSION")


def test_an_empty_default_is_refused(tmp_path: Path) -> None:
    """A default with no value has nothing to mirror, so it is not a pin."""
    root = _repository(tmp_path, "RUFF_VERSION ?=\n")

    with pytest.raises(
        AssertionError, match="must give RUFF_VERSION a non-empty default"
    ):
        read_makefile_pin(root, "RUFF_VERSION")


def test_a_multi_token_default_is_refused(tmp_path: Path) -> None:
    """A default no single pin can mirror is not a pin."""
    root = _repository(tmp_path, "RUFF_VERSION ?= 0.16.4 0.16.5\n")

    with pytest.raises(AssertionError, match="single-token default"):
        read_makefile_pin(root, "RUFF_VERSION")


def test_a_tab_indented_line_is_not_a_default(tmp_path: Path) -> None:
    """A leading tab begins a recipe line, which Make never reads as a default."""
    root = _repository(
        tmp_path,
        "\tRUFF_VERSION ?= 0.9.0\nRUFF_VERSION ?= 0.16.4\n",
    )

    assert read_makefile_pin(root, "RUFF_VERSION") == "0.16.4", (
        "a tab-indented recipe line must not be read as a Make default"
    )
