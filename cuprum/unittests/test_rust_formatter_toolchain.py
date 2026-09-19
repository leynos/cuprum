"""Contracts for the Rust formatter's maintenance toolchain."""

from __future__ import annotations

import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - expands fixed local Makefile recipes.
import sys
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
# A raw string opener such as `r"`, `r#"`, or `r##"`. The captured hash count
# must be repeated exactly to close the literal, so a quote carrying a
# different count cannot terminate it early.
_RAW_STRING_OPEN = re.compile(r'r(?P<hashes>#*)"')
# A plain string literal. Escapes are consumed so an escaped quote does not
# close the literal early.
_QUOTED_STRING = re.compile(r'"(?:\\.|[^"\\])*"', re.DOTALL)
# A character literal: one escape sequence, one non-quote character, or one
# `\u{..}` escape, closed by an immediately adjacent quote. Requiring that
# closing quote keeps a lifetime such as `'a` in `&'a str` from being read as
# the start of a literal.
_CHAR_LITERAL = re.compile(r"'(?:\\.|[^'\\])'|'\\u\{[0-9a-fA-F_]+\}'")
# Either block-comment delimiter. Scanning for both at once lets the nesting
# depth be tracked in a flat loop.
_BLOCK_COMMENT_TOKEN = re.compile(r"/\*|\*/")
# Only these characters can begin a lexeme: `/` for comments, `r` for raw
# strings, `"` for strings, and `'` for character literals.
_LEXEME_STARTS = frozenset("/r\"'")
# The Makefile gates dev-fast on `uname -s`, which reports Linux for the hosts
# `sys.platform` reports as `linux*`. Keeping the check explicit means a broken
# host probe in the Makefile fails the contract below instead of mirroring it.
_HOST_IS_LINUX = sys.platform.startswith("linux")


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


def _try_line_comment(source: str, index: int) -> int:
    """Find the end of the line comment starting at ``index``.

    Returns
    -------
        The offset just past the comment, or ``index`` when none starts here.
    """
    if not source.startswith("//", index):
        return index
    end = source.find("\n", index)
    return len(source) if end < 0 else end


def _try_block_comment(source: str, index: int) -> int:
    """Find the end of the block comment starting at ``index``.

    Block comments nest in Rust, so the terminator is the ``*/`` that closes
    the outermost comment rather than the first one encountered. An
    unterminated comment consumes the remainder of the source.

    Returns
    -------
        The offset just past the comment, or ``index`` when none starts here.
    """
    if not source.startswith("/*", index):
        return index
    depth = 0
    for token in _BLOCK_COMMENT_TOKEN.finditer(source, index):
        depth += 1 if token.group() == "/*" else -1
        if depth == 0:
            return token.end()
    return len(source)


def _try_raw_string(source: str, index: int) -> int:
    """Find the end of the raw string literal starting at ``index``.

    The closing delimiter repeats the opening hash count exactly, so a quote
    carrying a different count cannot close the literal. An unterminated
    literal consumes the remainder of the source.

    Returns
    -------
        The offset just past the literal, or ``index`` when none starts here.
    """
    opener = _RAW_STRING_OPEN.match(source, index)
    if opener is None:
        return index
    terminator = '"' + opener["hashes"]
    end = source.find(terminator, opener.end())
    return len(source) if end < 0 else end + len(terminator)


def _try_char_literal(source: str, index: int) -> int:
    """Find the end of the character literal starting at ``index``.

    Returns
    -------
        The offset just past the literal, or ``index`` when none starts here.
    """
    match = _CHAR_LITERAL.match(source, index)
    return index if match is None else match.end()


def _try_quoted_string(source: str, index: int) -> int:
    """Find the end of the plain string literal starting at ``index``.

    Returns
    -------
        The offset just past the literal, or ``index`` when none starts here.
    """
    match = _QUOTED_STRING.match(source, index)
    return index if match is None else match.end()


# Scanners for each lexeme kind, in the order they are tried. Each returns
# ``index`` when it does not apply, so the first scanner that advances wins.
_LEXEME_SCANNERS = (
    _try_line_comment,
    _try_block_comment,
    _try_raw_string,
    _try_char_literal,
    _try_quoted_string,
)


def _try_lexeme(source: str, index: int) -> int:
    """Find the end of the lexeme starting at ``index``.

    Recognizes line comments, block comments, raw string literals, character
    literals, and plain string literals.

    Returns
    -------
        The offset just past the lexeme, or ``index`` when none starts here.
    """
    if source[index] not in _LEXEME_STARTS:
        return index
    for scanner in _LEXEME_SCANNERS:
        end = scanner(source, index)
        if end != index:
            return end
    return index


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


def test_make_formatter_targets_select_the_pinned_nightly() -> None:
    """Only formatter recipes select the pinned nightly cargo route."""
    make_executable = shutil.which("make")
    assert make_executable is not None, "make must be available to expand recipes"
    root = repo_root()

    def expanded_recipes(*targets: str) -> str:
        """Return dry-run output for the fixed formatter contract targets."""
        completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
            [make_executable, "--dry-run", "CARGO=probe-cargo", *targets],
            check=True,
            shell=False,
            cwd=root,
            capture_output=True,
            encoding="utf-8",
        )
        return completed.stdout

    formatter_recipes = expanded_recipes("fmt", "check-fmt")
    formatter_lines = [
        line
        for line in formatter_recipes.splitlines()
        if line.startswith("cd rust && probe-cargo")
    ]
    expected_lines = [
        "cd rust && probe-cargo +nightly-2026-05-28 fmt --all",
        "cd rust && probe-cargo +nightly-2026-05-28 fmt --all -- --check",
    ]
    assert formatter_lines == expected_lines, (
        "formatter targets must use only the injected pinned nightly cargo route"
    )

    expected_debug_route = (
        "RUSTUP_TOOLCHAIN=nightly-2026-08-23 probe-cargo "
        "--config ../tools/dev-fast/config.toml"
    )
    dev_fast_fragments = (
        "RUSTUP_TOOLCHAIN=nightly-2026-08-23",
        "--config ../tools/dev-fast/config.toml",
    )
    for target in ("lint", "test"):
        target_recipes = expanded_recipes(target)
        cargo_lines = [
            line for line in target_recipes.splitlines() if "probe-cargo" in line
        ]
        assert cargo_lines, f"{target} must expand at least one Cargo command"
        assert all(
            "probe-cargo +nightly-2026-05-28" not in line for line in cargo_lines
        ), f"{target} must not select the formatter's nightly Cargo route"
        # The Makefile routes debug work through dev-fast only on Linux and falls
        # back to the bare injected Cargo elsewhere, so the fragment is required
        # on Linux and must be absent on every other host.
        if _HOST_IS_LINUX:
            assert any(expected_debug_route in line for line in cargo_lines), (
                f"{target} must retain the Linux dev-fast Cargo route"
            )
        else:
            assert all(
                fragment not in line
                for line in cargo_lines
                for fragment in dev_fast_fragments
            ), f"{target} must omit the Linux-only dev-fast Cargo route off Linux"
