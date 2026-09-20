"""Describe the Cargo compilation contracts used by boundary verification."""

from __future__ import annotations

import dataclasses
import re
import typing as typ

TARGET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


@dataclasses.dataclass(frozen=True)
class CompileTargets:
    """Select named integration-test targets for a focused Cargo check."""

    names: tuple[str, ...]

    @classmethod
    def integration_tests(cls, *names: str) -> typ.Self:
        """Create a safe immutable selection of integration-test target names."""
        if not names or any(TARGET_NAME.fullmatch(name) is None for name in names):
            msg = "compile targets must be non-empty Cargo target names"
            raise ValueError(msg)
        return cls(names)


def compile_arguments(targets: CompileTargets | None = None) -> tuple[str, ...]:
    """Return the Cargo check contract for full or named-test coverage."""
    if targets is None:
        return (
            "check",
            "--package",
            "cuprum-streams",
            "--all-targets",
            "--all-features",
        )
    test_arguments = tuple(
        argument for name in targets.names for argument in ("--test", name)
    )
    return ("check", "--package", "cuprum-streams", "--all-features", *test_arguments)
