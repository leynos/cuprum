"""Contracts for the Rust formatter's maintenance toolchain."""

from __future__ import annotations

import re
import tomllib

from tests.helpers.docs import repo_root
from tests.helpers.workflow import Workflow, step_named, steps

_SHARED_ACTION_REVISION = "c5a54701c8603a0fa756a6b34c49bc2af75a6c11"
_SETUP_RUST = (
    f"leynos/shared-actions/.github/actions/setup-rust@{_SHARED_ACTION_REVISION}"
)
_FORMATTER_TOOLCHAIN = "nightly-2026-05-28"
_PROJECT_TOOLCHAIN = "1.85.0"
_FORMATTER_FIXTURE_SKIPS = {
    ("rust/cuprum-native-io/src/ownership_tests.rs", "descriptor_guard"),
    ("rust/cuprum-streams/src/io_utils/tests.rs", "pipe"),
    ("rust/cuprum-streams/src/splice/tests.rs", "pipe"),
}
_RUSTFMT_SKIP = "#[rustfmt::skip]"
_RUSTFMT_SKIP_FINDER = re.compile(re.escape(_RUSTFMT_SKIP))
_RUSTFMT_FIXTURE_SKIP = re.compile(
    rf"{re.escape(_RUSTFMT_SKIP)}\n#\[fixture\]\nfn (?P<name>\w+)\b"
)
# Matches a Rust string literal, raw string literal, or line comment. Block
# comments are handled separately below because they nest. Each match keeps its
# own length, so code-bearing text stays at its original offset.
_RUST_LEXEME = re.compile(
    r'"(?:\\.|[^"\\])*"'
    r'|r#*"(?:[^"]|"(?!#*))*"#*'
    r"|//[^\n]*",
    re.DOTALL | re.MULTILINE,
)


def test_formatter_toolchain_precedes_the_project_toolchain(
    workflow_data: Workflow,
) -> None:
    """The cold CI path installs the formatter without changing project Rust."""
    formatter_setup = step_named(
        workflow_data, "lint-test", "Install formatter Rust toolchain"
    )
    project_setup = step_named(
        workflow_data, "lint-test", "Install project Rust toolchain"
    )

    assert formatter_setup.get("uses") == _SETUP_RUST, (
        "the formatter toolchain must use the pinned shared Rust setup action"
    )
    assert project_setup.get("uses") == _SETUP_RUST, (
        "the project toolchain must use the pinned shared Rust setup action"
    )
    assert formatter_setup.get("with") == {
        "toolchain": _FORMATTER_TOOLCHAIN,
        "cache-provider": "external",
        "use-sccache": "false",
    }, "the formatter setup must provision the pinned nightly on every runner"
    assert project_setup.get("with") == {
        "toolchain": _PROJECT_TOOLCHAIN,
        "cache-provider": "external",
        "use-sccache": "false",
    }, "the project setup must restore the supported stable compiler"

    lint_steps = steps(workflow_data, "lint-test")
    assert lint_steps.index(formatter_setup) < lint_steps.index(project_setup), (
        "the stable project toolchain must replace setup-rust's formatter override"
    )


def test_project_toolchain_declares_maintenance_components() -> None:
    """The stable toolchain keeps editor and lint components available locally."""
    configuration = tomllib.loads(
        (repo_root() / "rust" / "rust-toolchain.toml").read_text(encoding="utf-8")
    )
    toolchain = configuration.get("toolchain")
    assert isinstance(toolchain, dict), "rust-toolchain.toml must declare a toolchain"
    assert toolchain == {
        "channel": _PROJECT_TOOLCHAIN,
        "profile": "minimal",
        "components": ["rustfmt", "clippy", "rust-analyzer"],
    }, "the stable pin must retain its compiler and declare each required component"


def _blank(lexeme: str) -> str:
    """Render a lexeme as blanks, preserving its newlines and offsets."""
    return "".join("\n" if char == "\n" else " " for char in lexeme)


def _try_lexeme(source: str, index: int) -> int:
    """Find the end of the lexeme starting at ``index``.

    Recognizes string literals, raw string literals, line comments, and block
    comments. Block comments nest in Rust, so the terminator is the ``*/`` that
    closes the outermost comment rather than the first one encountered.

    Returns
    -------
        The offset just past the lexeme, or ``index`` when no lexeme starts here.
    """
    if source.startswith("//", index):
        end = source.find("\n", index)
        return len(source) if end < 0 else end
    if source.startswith("/*", index):
        depth = 0
        cursor = index
        while cursor < len(source):
            if source.startswith("/*", cursor):
                depth += 1
                cursor += 2
            elif source.startswith("*/", cursor):
                depth -= 1
                cursor += 2
                if depth == 0:
                    return cursor
            else:
                cursor += 1
        return len(source)
    match = _RUST_LEXEME.match(source, index)
    return index if match is None else match.end()


def _blank_rust_lexemes(source: str) -> str:
    """Blank comments and literals, keeping code at its original offset."""
    blanked: list[str] = []
    cursor = 0
    index = 0
    while index < len(source):
        end = _try_lexeme(source, index)
        if end == index:
            index += 1
            continue
        blanked.extend((source[cursor:index], _blank(source[index:end])))
        index = end
        cursor = end
    blanked.append(source[cursor:])
    return "".join(blanked)


def _line_number(blanked: str, offset: int) -> int:
    """Return the one-based line number at a character offset."""
    return blanked.count("\n", 0, offset) + 1


def test_formatter_skips_are_limited_to_known_rstest_fixtures() -> None:
    """The formatter exception set remains auditable and deliberately small."""
    root = repo_root()
    rust_root = root / "rust"
    observed: set[tuple[str, str]] = set()

    for source_path in rust_root.glob("**/*.rs"):
        relative = source_path.relative_to(root).as_posix()
        source = _blank_rust_lexemes(source_path.read_text(encoding="utf-8"))
        for skip in _RUSTFMT_SKIP_FINDER.finditer(source):
            fixture = _RUSTFMT_FIXTURE_SKIP.match(source, skip.start())
            assert fixture is not None, (
                f"{relative}:{_line_number(source, skip.start())}: every rustfmt "
                "skip must apply directly to one rstest fixture"
            )
            observed.add((relative, fixture["name"]))

    assert observed == _FORMATTER_FIXTURE_SKIPS, (
        "add a mutation proof before extending the formatter exception set"
    )
