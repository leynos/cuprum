"""Hold the Linux-only Cargo acceleration route to its explicit contract."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests fixed local build wiring.
import tomllib
import typing as typ

import pytest

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from collections import abc as cabc
    from pathlib import Path


FRAGMENT_SHA256 = "d8fcc29ce680ecf43e96f024bd8d0e1a378d5998efdafc8221902735f9ff4ec4"
FRAGMENT = "tools/dev-fast/config.toml"
RUST_MEMBERS = ("cuprum-rust", "cuprum-streams", "cuprum-native-io")


def _dry_run(*goals: str, variables: dict[str, str] | None = None) -> str:
    """Return evaluated Make recipes for a controlled caller environment."""
    make = shutil.which("make")
    assert make is not None
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
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


def _assert_fragment_free(route: str) -> None:
    """Reject any selected Linux development acceleration from a protected route."""
    assert "--config" not in route
    assert "tools/dev-fast/cargo" not in route
    assert "nightly-2026-08-23" not in route
    assert "DEV_FAST_" not in route


def _assert_fragment_mutation_is_rejected(route: str) -> None:
    """Prove fragment-free assertions fail when a protected route is contaminated."""
    mutation = f"{route}\nprobe-cargo --config {FRAGMENT} check"
    with pytest.raises(AssertionError):
        _assert_fragment_free(mutation)


def test_fragment_is_the_approved_immutable_extension() -> None:
    """Keep the checked-in Cargo fragment byte-identical to its approved pin."""
    fragment = (repo_root() / FRAGMENT).read_bytes()
    assert hashlib.sha256(fragment).hexdigest() == FRAGMENT_SHA256


def test_every_workspace_member_inherits_the_published_msrv() -> None:
    """Keep Clippy's MSRV-aware diagnostics aligned with Cargo metadata."""
    rust_root = repo_root() / "rust"
    workspace = tomllib.loads((rust_root / "Cargo.toml").read_text(encoding="utf-8"))
    assert workspace["workspace"]["package"]["rust-version"] == "1.85.0"
    assert tuple(workspace["workspace"]["members"]) == RUST_MEMBERS
    for member in RUST_MEMBERS:
        manifest = tomllib.loads(
            (rust_root / member / "Cargo.toml").read_text(encoding="utf-8")
        )
        assert manifest["package"]["rust-version"] == {"workspace": True}


def test_linux_debug_routes_select_the_fragment_and_injected_cargo() -> None:
    """Every supported debug Cargo invocation explicitly selects the fragment."""
    output = _dry_run("develop", "test-rust", "rust-lint", "dev-build", "dev-test")
    routing = "\n".join(_routing_lines(output))
    assert "DEV_FAST_CARGO=probe-cargo" in routing
    assert "CARGO=tools/dev-fast/cargo" in routing
    direct_cargo = [
        line for line in routing.splitlines() if "probe-cargo --config" in line
    ]
    assert sum(line.count("probe-cargo --config") for line in direct_cargo) == 7
    assert all(
        "--config ../tools/dev-fast/config.toml" in line for line in direct_cargo
    )
    assert "RUSTUP_TOOLCHAIN=nightly-2026-08-23" in routing


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
    assert "tools/dev-fast/cargo" not in output
    assert "DEV_FAST_CARGO=" not in output
    assert "RUSTUP_TOOLCHAIN=nightly-2026-08-23" not in output


def test_non_linux_alternative_stays_on_the_stable_cargo_route() -> None:
    """The explicit non-Linux alternative cannot acquire the Linux fragment."""
    output = _dry_run(
        "develop", "test-rust", "rust-lint", variables={"DEV_FAST_HOST_IS_LINUX": ""}
    )
    assert "tools/dev-fast/cargo" not in output
    assert "--config ../tools/dev-fast/config.toml" not in output
    assert "RUSTUP_TOOLCHAIN=nightly-2026-08-23" not in output


def test_whitaker_and_windows_lint_remain_fragment_free() -> None:
    """Verification and cross-platform routes never inherit Linux acceleration."""
    output = _dry_run("rust-lint", "lint-windows")
    whitaker = next(line for line in output.splitlines() if "whitaker --all" in line)
    windows = next(
        line
        for line in output.splitlines()
        if "--target x86_64-pc-windows-msvc" in line
    )
    assert "--config" not in whitaker
    assert "RUSTUP_TOOLCHAIN=nightly-2026-08-23" not in whitaker
    assert "--config" not in windows
    assert "RUSTUP_TOOLCHAIN=nightly-2026-08-23" not in windows


def test_msrv_verification_keeps_the_stable_route_fragment_free() -> None:
    """The accelerated lint compiler cannot replace published-MSRV verification."""
    output = _dry_run("msrv-check")
    route = next(line for line in output.splitlines() if "probe-cargo check" in line)
    assert "RUSTUP_TOOLCHAIN=1.85.0" in route
    assert "--config" not in route
    assert "nightly-2026-08-23" not in route


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


def _bridge_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Create a recording Cargo child for the adapter's process contract."""
    argv_path = tmp_path / "argv"
    child = tmp_path / "cargo-child"
    child.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$DEV_FAST_ARGV"\nexit 47\n',
        encoding="utf-8",
    )
    child.chmod(0o755)
    return {
        "DEV_FAST_ARGV": str(argv_path),
        "DEV_FAST_CARGO": str(child),
        "DEV_FAST_CONFIG": str(repo_root() / FRAGMENT),
    }, argv_path


def test_bridge_injects_one_fragment_and_preserves_child_exit(
    tmp_path: Path,
) -> None:
    """The Maturin adapter preserves Cargo's argv and status through exec."""
    environment, argv_path = _bridge_environment(tmp_path)
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [repo_root() / "tools/dev-fast/cargo", "rustc", "--lib"],
        check=False,
        cwd=repo_root(),
        env={**os.environ, **environment},
    )
    assert result.returncode == 47
    assert argv_path.read_text(encoding="utf-8").splitlines() == [
        "--config",
        environment["DEV_FAST_CONFIG"],
        "rustc",
        "--lib",
    ]


@pytest.mark.parametrize("config_flag", ["--config", "--config=other.toml"])
def test_bridge_rejects_duplicate_configuration(
    tmp_path: Path, config_flag: str
) -> None:
    """A caller cannot override or duplicate the approved configuration."""
    environment, argv_path = _bridge_environment(tmp_path)
    command = [repo_root() / "tools/dev-fast/cargo", "rustc", config_flag]
    if config_flag == "--config":
        command.append("other.toml")
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        command, check=False, cwd=repo_root(), env={**os.environ, **environment}
    )
    assert result.returncode == 2
    assert not argv_path.exists()


def _write_program(directory: Path, name: str, body: str) -> None:
    """Create one controlled executable used to mutate a prerequisite."""
    program = directory / name
    program.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    program.chmod(0o755)


@pytest.mark.parametrize(
    "programs",
    [
        pytest.param({}, id="missing_mold"),
        pytest.param(
            {"mold": "printf '%s\\n' 'mold 2.41.0'", "rustup": "exit 0"},
            id="missing_component",
        ),
    ],
)
def test_prerequisite_recipe_fails_closed_for_missing_dependencies(
    tmp_path: Path, programs: dict[str, str]
) -> None:
    """Missing linker or component is a hard failure, not an optimistic skip."""
    for name, body in programs.items():
        _write_program(tmp_path, name, body)
    make = shutil.which("make")
    assert make is not None
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [make, "dev-fast-check", "DEV_FAST_HOST_IS_LINUX=yes"],
        check=False,
        cwd=repo_root(),
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"},
        text=True,
    )
    assert result.returncode != 0


def test_ci_provisions_only_the_pinned_linux_prerequisites() -> None:
    """CI installs only the approved binary prerequisites for Linux debug work."""
    action = (repo_root() / ".github/actions/setup-dev-fast/action.yml").read_text(
        encoding="utf-8"
    )
    workflow = (repo_root() / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "nightly-2026-08-23" in action
    assert "rustc-codegen-cranelift" in action
    assert 'rustup component add "${component}" clippy' in action
    assert 'archive="mold-${version}-${architecture}-linux.tar.gz"' in action
    assert "sha256sum --check --status" in action
    assert "cargo install" not in action
    assert workflow.count("uses: ./.github/actions/setup-dev-fast") == 2
    assert "- name: Verify Rust MSRV\n        run: make msrv-check" in workflow
