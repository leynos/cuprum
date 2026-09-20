"""Keep the committed Rust lint baseline complete and executable."""

from __future__ import annotations

import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - this test expands a fixed local Make recipe.
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIPPY_LINTS = {
    "allow_attributes",
    "allow_attributes_without_reason",
    "blanket_clippy_restriction_lints",
    "cognitive_complexity",
    "disallowed_methods",
    "needless_pass_by_value",
    "implicit_hasher",
    "missing_assert_message",
    "dbg_macro",
    "print_stdout",
    "print_stderr",
    "unwrap_used",
    "expect_used",
    "indexing_slicing",
    "string_slice",
    "integer_division",
    "integer_division_remainder_used",
    "panic_in_result_fn",
    "unreachable",
    "host_endian_bytes",
    "little_endian_bytes",
    "big_endian_bytes",
    "let_underscore_must_use",
    "or_fun_call",
    "option_if_let_else",
    "self_named_module_files",
    "shadow_reuse",
    "shadow_same",
    "shadow_unrelated",
    "str_to_string",
    "string_lit_as_bytes",
    "try_err",
    "unneeded_field_pattern",
    "use_self",
    "float_arithmetic",
    "cast_possible_truncation",
    "cast_possible_wrap",
    "cast_precision_loss",
    "lossy_float_literal",
    "missing_const_for_fn",
    "must_use_candidate",
    "unused_async",
    "missing_panics_doc",
    "error_impl_error",
    "result_large_err",
}
RUST_LINTS = {"missing_docs", "renamed_and_removed_lints", "unknown_lints"}
RUSTDOC_LINTS = {
    "bare_urls",
    "broken_intra_doc_links",
    "invalid_codeblock_attributes",
    "invalid_html_tags",
    "missing_crate_level_docs",
    "private_intra_doc_links",
    "unescaped_backticks",
}
DISALLOWED_METHODS = {
    "std::env::var",
    "std::env::var_os",
    "std::env::vars",
    "std::env::vars_os",
    "std::env::set_var",
    "std::env::remove_var",
    "std::env::set_current_dir",
}
EXPECTED_PEDANTIC = {"level": "warn", "priority": -1}
EXPECTED_UNEXPECTED_CFGS = {"level": "warn", "check-cfg": ["cfg(kani)"]}
EXPECTED_CLIPPY_OPTIONS = {
    "cognitive-complexity-threshold": 9,
    "too-many-arguments-threshold": 4,
    "too-many-lines-threshold": 70,
    "excessive-nesting-threshold": 4,
    "allow-expect-in-tests": True,
}


def _cargo_manifest() -> dict[str, object]:
    """Load the workspace manifest that owns the shared lint policy."""
    with (ROOT / "rust/Cargo.toml").open("rb") as manifest:
        return tomllib.load(manifest)


def _clippy_config() -> dict[str, object]:
    """Load the local Clippy configuration beside the workspace manifest."""
    with (ROOT / "rust/clippy.toml").open("rb") as config:
        return tomllib.load(config)


def _dry_run_test_rust() -> str:
    """Expand the Linux doctest recipe without running Cargo or prerequisites."""
    make = shutil.which("make")
    assert make is not None, "the Make contract requires GNU Make on PATH"
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [
            make,
            "--dry-run",
            "test-rust",
            "CARGO=probe-cargo",
            "DEV_FAST_HOST_IS_LINUX=yes",
            "BUILD_JOBS=--jobs 7",
        ],
        capture_output=True,
        check=True,
        cwd=ROOT,
        text=True,
    )
    return completed.stdout


def test_workspace_declares_every_required_rust_lint_at_the_expected_level() -> None:
    """The baseline must not lose a denied lint during future table edits."""
    workspace = _cargo_manifest()["workspace"]
    assert isinstance(workspace, dict), "the workspace table must be a TOML mapping"
    lints = workspace["lints"]
    assert isinstance(lints, dict), "the workspace must declare a lint table"
    clippy = lints["clippy"]
    rust = lints["rust"]
    rustdoc = lints["rustdoc"]
    assert isinstance(clippy, dict), "the workspace must declare Clippy lints"
    assert isinstance(rust, dict), "the workspace must declare Rust lints"
    assert isinstance(rustdoc, dict), "the workspace must declare Rustdoc lints"
    assert clippy["pedantic"] == EXPECTED_PEDANTIC, (
        "pedantic must remain the warn-level base"
    )
    assert {name: clippy[name] for name in CLIPPY_LINTS} == dict.fromkeys(
        CLIPPY_LINTS, "deny"
    ), "every required Clippy lint must stay denied"
    assert {name: rust[name] for name in RUST_LINTS} == dict.fromkeys(
        RUST_LINTS, "deny"
    ), "every required Rust lint must stay denied"
    assert {name: rustdoc[name] for name in RUSTDOC_LINTS} == dict.fromkeys(
        RUSTDOC_LINTS, "deny"
    ), "every required Rustdoc lint must stay denied"
    assert "unsafe_code" not in rust, (
        "the audited syscall and FFI boundaries preclude a workspace-wide unsafe ban"
    )
    assert rust["unexpected_cfgs"] == EXPECTED_UNEXPECTED_CFGS, (
        "the Kani conditional compilation contract must remain reachable"
    )


def test_clippy_configuration_keeps_the_approved_thresholds_and_methods() -> None:
    """Thresholds and environment injection must stay auditable local policy."""
    config = _clippy_config()
    assert {
        name: config[name]
        for name in (
            "cognitive-complexity-threshold",
            "too-many-arguments-threshold",
            "too-many-lines-threshold",
            "excessive-nesting-threshold",
            "allow-expect-in-tests",
        )
    } == EXPECTED_CLIPPY_OPTIONS, "the approved Clippy options must not drift"
    methods = config["disallowed-methods"]
    assert isinstance(methods, list), "disallowed methods must remain a TOML list"
    assert {method["path"] for method in methods if isinstance(method, dict)} == (
        DISALLOWED_METHODS
    ), "the seven environment methods must stay disallowed"
    assert all(
        isinstance(method, dict)
        and isinstance(method.get("reason"), str)
        and method["reason"]
        for method in methods
    ), "each disallowed environment method needs a local injection rationale"


def test_doctest_recipe_passes_full_rustdoc_warning_flags_and_jobs() -> None:
    """Require the separate gate to use pinned nightly rustdoc warnings."""
    output = _dry_run_test_rust()
    doctest = next(
        line
        for line in output.splitlines()
        if "test --workspace --doc --all-features --jobs 7" in line
    )
    assert (
        'RUSTDOCFLAGS="--cfg docsrs -D warnings -Zunstable-options '
        "--display-doctest-warnings --doctest-build-arg=-D "
        '--doctest-build-arg=warnings"'
    ) in doctest, "the doctest recipe must retain every rustdoc warning flag"
