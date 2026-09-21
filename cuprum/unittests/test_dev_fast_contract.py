"""Hold the Linux-only Cargo acceleration route to its explicit contract."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - controlled test argv.
import tomllib
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from collections import abc as cabc


FRAGMENT_SHA256 = "8619efda5ea1c3232f413ae96ff56869ab6b2b7cd5bdef5a001ad16ebeac23a5"
FRAGMENT = "tools/dev-fast/config.toml"
RUST_MEMBERS = ("cuprum-rust", "cuprum-streams", "cuprum-native-io")
SAFE_MATURIN_FLAGS = st.sampled_from((
    "--release",
    "-r",
    "--profile",
    "release",
    "--profile=release",
    "--skip-install",
))


def _dry_run(*goals: str, variables: dict[str, str] | None = None) -> str:
    """Return evaluated Make recipes for a controlled caller environment."""
    make = shutil.which("make")
    assert make is not None, "the routing contract requires GNU Make on PATH"
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [
            make,
            "--dry-run",
            "CARGO=probe-cargo",
            *(f"{key}={value}" for key, value in (variables or {}).items()),
            *goals,
        ],
        capture_output=True,
        check=True,
        cwd=repo_root(),
        env={**os.environ, "MAKEFLAGS": ""},
        text=True,
    )
    return completed.stdout


def _routing_lines(output: str) -> list[str]:
    """Return evaluated Cargo lines, excluding spelling's unrelated config."""
    return [
        line
        for line in output.splitlines()
        if "probe-cargo" in line or "CARGO=tools/dev-fast/cargo" in line
    ]


def _assert_required_tokens(route: str, tokens: tuple[str, ...], subject: str) -> None:
    """Require every token that makes a routed command's contract binding."""
    missing = tuple(token for token in tokens if token not in route)
    assert not missing, f"{subject} omitted required routing tokens: {missing}"


def _assert_fragment_free(route: str) -> None:
    """Reject any selected Linux development acceleration from a protected route."""
    assert "--config" not in route, f"protected route selected Cargo config: {route}"
    assert "tools/dev-fast/cargo" not in route, (
        f"protected route selected bridge: {route}"
    )
    assert "nightly-2026-08-23" not in route, (
        f"protected route selected nightly: {route}"
    )
    assert "DEV_FAST_" not in route, f"protected route leaked dev-fast state: {route}"


def _assert_fragment_mutation_is_rejected(route: str) -> None:
    """Prove fragment-free assertions fail when a protected route is contaminated."""
    mutation = f"{route}\nprobe-cargo --config {FRAGMENT} check"
    with pytest.raises(AssertionError):
        _assert_fragment_free(mutation)


def _is_release_maturin_flags(flags: list[str]) -> bool:
    """Model the documented Makefile spellings that must select a release build."""
    return any(
        flag in {"--release", "-r", "--profile=release"}
        or (flag == "--profile" and flags[index + 1 : index + 2] == ["release"])
        for index, flag in enumerate(flags)
    )


def test_make_reads_the_linker_version_from_its_pin() -> None:
    """Keep prerequisite diagnostics coupled to the checked-in linker pin."""
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "DEV_FAST_MOLD_VERSION_FILE ?= tools/mold/VERSION" in makefile, (
        "the prerequisite check must read the tracked linker version file"
    )
    assert "mold 2.41.0 is required" not in makefile, (
        "Make diagnostics must not retain a stale hard-coded linker version"
    )


def test_the_prerequisite_gate_requires_both_nightly_components() -> None:
    """Clippy runs through the dev-fast nightly, so the gate must require it."""
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "DEV_FAST_LINT_COMPONENT ?= clippy" in makefile, (
        "the dev-fast lint route depends on a separately installed Clippy"
    )
    assert (
        "DEV_FAST_REQUIRED_COMPONENTS ?= $(DEV_FAST_CRANELIFT_COMPONENT) "
        "$(DEV_FAST_LINT_COMPONENT)" in makefile
    ), "the prerequisite gate must enumerate every component it requires"
    assert "$(DEV_FAST_REQUIRED_COMPONENTS)" in makefile, (
        "the prerequisite gate must check every required component"
    )


def test_fragment_is_the_approved_immutable_extension() -> None:
    """Keep the checked-in Cargo fragment byte-identical to its approved pin."""
    fragment = (repo_root() / FRAGMENT).read_bytes()
    assert hashlib.sha256(fragment).hexdigest() == FRAGMENT_SHA256, (
        "the reviewed fragment digest must change with its contents"
    )
    developers_guide = (repo_root() / "docs/developers-guide.md").read_text(
        encoding="utf-8"
    )
    assert FRAGMENT_SHA256 in developers_guide, (
        "the developer guide must publish the same reviewed fragment digest"
    )


def test_the_routed_fragment_path_cannot_be_overridden() -> None:
    """The selected fragment is Makefile-fixed, not a caller-supplied variable.

    A `?=` or plain assignment here would let an environment variable or a
    command-line variable select an unreviewed Cargo configuration while every
    routing test still passed, because those tests exercise the default
    invocation. GNU Make's `override` directive is the only definition form that
    outranks both, so it is required rather than stylistic.
    """
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "override DEV_FAST_CONFIG_RELATIVE := tools/dev-fast/config.toml" in (
        makefile
    ), "the fragment path must be a Makefile-owned constant no caller can replace"
    assert "DEV_FAST_RUST_CONFIG ?=" not in makefile, (
        "a `?=` definition still yields to an ordinary environment variable"
    )
    assert "\nDEV_FAST_CONFIG ?=" not in makefile, (
        "the fragment path must not remain caller-overridable"
    )


def test_the_adapter_environment_does_not_expose_the_configuration() -> None:
    """The Maturin adapter derives its fragment, so nothing passes it one."""
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "DEV_FAST_CONFIG=" not in makefile, (
        "the routed adapter environment must not carry a caller-settable fragment"
    )
    output = _dry_run("develop", variables={"DEV_FAST_HOST_IS_LINUX": "yes"})
    assert "DEV_FAST_CONFIG=" not in output, (
        "develop must not hand the adapter a configuration path to trust"
    )


def test_every_workspace_member_inherits_the_published_msrv() -> None:
    """Keep Clippy's MSRV-aware diagnostics aligned with Cargo metadata."""
    rust_root = repo_root() / "rust"
    workspace = tomllib.loads((rust_root / "Cargo.toml").read_text(encoding="utf-8"))
    assert workspace["workspace"]["package"]["rust-version"] == "1.85.0", (
        "the workspace must publish the agreed Rust compatibility version"
    )
    assert tuple(workspace["workspace"]["members"]) == RUST_MEMBERS, (
        "the MSRV contract must enumerate every workspace package"
    )
    for member in RUST_MEMBERS:
        manifest = tomllib.loads(
            (rust_root / member / "Cargo.toml").read_text(encoding="utf-8")
        )
        assert manifest["package"]["rust-version"] == {"workspace": True}, (
            f"{member} must inherit the root MSRV contract"
        )


def test_linux_debug_routes_select_the_fragment_and_injected_cargo() -> None:
    """Every supported debug Cargo invocation explicitly selects the fragment."""
    output = _dry_run(
        "develop",
        "test-rust",
        "rust-lint",
        "dev-build",
        "dev-test",
        variables={"DEV_FAST_HOST_IS_LINUX": "yes"},
    )
    routing = "\n".join(_routing_lines(output))
    assert "DEV_FAST_CARGO=probe-cargo" in routing, "develop must inject Cargo"
    assert "CARGO=tools/dev-fast/cargo" in routing, "develop must use the adapter"
    direct_cargo = [
        line for line in routing.splitlines() if "probe-cargo --config" in line
    ]
    assert sum(line.count("probe-cargo --config") for line in direct_cargo) == 8, (
        "each standard and explicit debug Cargo invocation must select one fragment"
    )
    assert all(
        "--config ../tools/dev-fast/config.toml" in line for line in direct_cargo
    ), "every debug Cargo command must select the approved relative fragment"
    assert "RUSTUP_TOOLCHAIN=nightly-2026-08-23" in routing, (
        "debug routes must select the pinned dev-fast nightly"
    )
    assert "-Clink-arg=-fuse-ld=mold" in routing, (
        "explicit RUSTFLAGS must retain the Linux linker selection"
    )


def test_rust_doctests_are_a_separate_debug_routed_gate() -> None:
    """Keep doctests covered when nextest owns ordinary Rust tests."""
    output = _dry_run("test-rust", variables={"DEV_FAST_HOST_IS_LINUX": "yes"})
    doctest = next(
        line
        for line in _routing_lines(output)
        if "test --workspace --doc --all-features" in line
    )
    _assert_required_tokens(
        doctest,
        (
            "probe-cargo --config ../tools/dev-fast/config.toml",
            'RUSTFLAGS="-D warnings',
            'RUSTDOCFLAGS="--cfg docsrs -D warnings -Zunstable-options',
            "--display-doctest-warnings",
            "--doctest-build-arg=-D --doctest-build-arg=warnings",
            "-Clink-arg=-fuse-ld=mold",
        ),
        "the Linux doctest route",
    )


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param("--release --skip-install", id="release"),
        pytest.param("-r --skip-install", id="short_release"),
        pytest.param("--profile release --skip-install", id="profile_release"),
    ],
)
def test_release_maturin_routes_do_not_select_dev_fast(flags: str) -> None:
    """Release spellings cannot accidentally route Maturin through Cranelift."""
    output = _dry_run("develop", variables={"MATURIN_DEVELOP_FLAGS": flags})
    assert "tools/dev-fast/cargo" not in output, "release Maturin must skip bridge"
    assert "DEV_FAST_CARGO=" not in output, "release Maturin must skip adapter env"
    assert "RUSTUP_TOOLCHAIN=nightly-2026-08-23" not in output, (
        "release Maturin must not select the dev-fast nightly"
    )


@settings(max_examples=24, deadline=None)
@given(st.lists(SAFE_MATURIN_FLAGS, max_size=7))
def test_maturin_release_classification_is_order_independent(flags: list[str]) -> None:
    """Generated safe flag sequences preserve the documented release boundary."""
    output = _dry_run("develop", variables={"MATURIN_DEVELOP_FLAGS": " ".join(flags)})
    is_release = _is_release_maturin_flags(flags)
    assert ("tools/dev-fast/cargo" not in output) is is_release, (
        f"flags {flags!r} must {'skip' if is_release else 'select'} the dev-fast bridge"
    )


def test_non_linux_alternative_stays_on_the_stable_cargo_route() -> None:
    """The explicit non-Linux alternative cannot acquire the Linux fragment."""
    output = _dry_run(
        "develop", "test-rust", "rust-lint", variables={"DEV_FAST_HOST_IS_LINUX": ""}
    )
    assert "tools/dev-fast/cargo" not in output, "non-Linux must skip bridge"
    assert "--config ../tools/dev-fast/config.toml" not in output, (
        "non-Linux must not select the fragment"
    )
    assert "nightly-2026-08-23" not in output, "non-Linux must use its stable route"


def test_whitaker_and_windows_lint_remain_fragment_free() -> None:
    """Verification and cross-platform routes never inherit Linux acceleration."""
    output = _dry_run("rust-lint", "lint-windows")
    whitaker = next(line for line in output.splitlines() if "whitaker --all" in line)
    windows = next(
        line
        for line in output.splitlines()
        if "--target x86_64-pc-windows-msvc" in line
    )
    assert "--config" not in whitaker, "Whitaker must not select a Cargo fragment"
    assert "RUSTUP_TOOLCHAIN=nightly-2026-08-23" not in whitaker, (
        "Whitaker must keep its verifier toolchain"
    )
    assert "--config" not in windows, "Windows lint must not select Linux fragment"
    assert "RUSTUP_TOOLCHAIN=nightly-2026-08-23" not in windows, (
        "Windows lint must retain its platform route"
    )


def test_portable_lint_orchestration_preserves_leaf_order() -> None:
    """The aggregate lint gates avoid GNU Make 4.4-only prerequisite markers."""
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    output = _dry_run("lint")

    # Strip comments before looking for `.WAIT`: the Makefile describes the
    # marker in prose precisely to record why it is not used, and a raw text
    # search would match that explanation instead of any real prerequisite.
    directives = "\n".join(
        line for line in makefile.splitlines() if not line.lstrip().startswith("#")
    )
    assert ".WAIT" not in directives, "hosted Make must not require GNU Make 4.4"
    # `.NOTPARALLEL` with prerequisites is itself GNU Make 4.4-only, so the
    # ordering cannot rest on it alone. Make 4.3 ignores the prerequisites and
    # serializes the whole run instead, which preserves the order either way.
    assert ".NOTPARALLEL: lint rust-lint" in directives, (
        "both lint aggregates must be serialized, present on GNU Make 4.3 and 4.4"
    )
    assert "RECURSIVE_MAKE" not in makefile, (
        "lint leaves must run in one Make process so caller `-f` files are "
        "honoured without forwarding a `MAKEFILE_LIST` that cannot distinguish "
        "`-f` inputs from included files"
    )
    python_lint = output.index("python-lint")
    clippy = output.index("probe-cargo --config ../tools/dev-fast/config.toml clippy")
    whitaker = output.index("whitaker --all --")
    spelling = output.index("typos-config-builder gate --repository . --scope all")
    workflow_lint = output.index("yamllint --strict --config-file")

    assert python_lint < clippy < whitaker < spelling < workflow_lint, (
        "lint must serialize Python, Rust leaves, and GitHub Actions validation"
    )


def test_msrv_verification_keeps_the_stable_route_fragment_free() -> None:
    """The accelerated lint compiler cannot replace published-MSRV verification."""
    output = _dry_run("msrv-check")
    route = next(line for line in output.splitlines() if "probe-cargo check" in line)
    assert "RUSTUP_TOOLCHAIN=1.85.0" in route, "MSRV check must pin Rust 1.85.0"
    assert "--config" not in route, "MSRV check must not select the fragment"
    assert "nightly-2026-08-23" not in route, "MSRV check must not use nightly"


@pytest.mark.parametrize(
    "route",
    [
        pytest.param(
            lambda: "\n".join([
                _dry_run("build-release"),
                (repo_root() / ".github/actions/build-wheels/action.yml").read_text(
                    encoding="utf-8"
                ),
            ]),
            id="release",
        ),
        pytest.param(
            lambda: (repo_root() / ".github/workflows/coverage-main.yml").read_text(
                encoding="utf-8"
            ),
            id="coverage",
        ),
        pytest.param(
            lambda: _dry_run("boundary-kani", "boundary-miri", "boundary-test"),
            id="verification",
        ),
    ],
)
def test_protected_routes_reject_dev_fast_contamination(
    route: cabc.Callable[[], str],
) -> None:
    """Keep release, coverage, and verification invocations on their own toolchains."""
    protected_route = route()
    _assert_fragment_free(protected_route)
    _assert_fragment_mutation_is_rejected(protected_route)


def test_ci_provisions_only_the_pinned_linux_prerequisites() -> None:
    """CI installs only the approved binary prerequisites for Linux debug work."""
    action = (repo_root() / ".github/actions/setup-dev-fast/action.yml").read_text(
        encoding="utf-8"
    )
    workflow = (repo_root() / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "nightly-2026-08-23" in action, "CI must install the pinned nightly"
    assert "rustc-codegen-cranelift" in action, "CI must install Cranelift"
    assert 'rustup component add "${component}" clippy' in action, (
        "CI must provision the lint component with the backend"
    )
    assert 'archive="mold-${version}-${architecture}-linux.tar.gz"' in action, (
        "CI must derive the architecture-specific archive"
    )
    assert "sha256sum --check --status" in action, "CI must verify downloaded mold"
    assert "cargo install" not in action, "CI must not add a source-build fallback"
    assert workflow.count("uses: ./.github/actions/setup-dev-fast") == 2, (
        "only Linux debug jobs may provision dev-fast"
    )
    assert "- name: Verify Rust MSRV\n        run: make msrv-check" in workflow, (
        "CI must retain independent MSRV verification"
    )
