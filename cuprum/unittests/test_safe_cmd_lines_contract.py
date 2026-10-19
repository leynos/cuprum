"""Call-time validation contracts for ``SafeCmd.lines()``."""

from __future__ import annotations

import pytest

from cuprum import ScopeConfig, scoped
from cuprum.sh import RunOutputOptions, StdinInput, StdioTarget
from tests.helpers.catalogue import build_python_catalogue_env


def test_lines_rejects_contested_stdin_when_called() -> None:
    """A line stream rejects incompatible stdin sources before iteration."""
    runtime = build_python_catalogue_env()
    command = runtime.builder("-c", "import sys; sys.stdin.read()")

    with (
        scoped(ScopeConfig(allowlist=frozenset([runtime.program]))),
        pytest.raises(ValueError, match="also supplies a stdin source"),
    ):
        command.lines(
            stdin=StdinInput(text="payload"),
            output=RunOutputOptions(stdin=StdioTarget.inherit()),
        )
