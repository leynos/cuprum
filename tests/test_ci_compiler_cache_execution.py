"""Execute the wrapper paths a green job cannot distinguish.

A build that compiles with plain rustc and a build that runs through sccache
both pass, and only one of them uses the compiler cache. The setup-sccache
composite and the dev-fast Cargo adapter are the two processes that decide
which happens, so these tests run their real shell bodies and observe the
environment a later build would receive.

`test_ci_setup_sccache_server_start.py` asserts what the start step exports;
this module asserts what a build then does with it, and that the dev-fast
adapter forwards the wrapper rather than replacing it.
"""

from __future__ import annotations

from pathlib import Path

from tests.helpers.composite_actions import run_step, step_script
from tests.test_ci_setup_sccache_action import ACTION, _run_install

START_STEP = "Start the sccache server"
ADAPTER = Path("tools/dev-fast/cargo")
FAKE_VERSION = "0.12.0"


def _start(tmp_path: Path, *, starts: bool) -> dict[str, str]:
    """Run the start step against a fake server and return the merged env."""
    binary = tmp_path / ".local" / "bin" / "sccache"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--start-server" ]; then\n'
        f"  exit {0 if starts else 1}\n"
        "fi\n"
        'if [ "$1" = "--stop-server" ]; then\n'
        '  echo "Error: couldn\'t connect to server" >&2\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    outputs = tmp_path / "github_output"
    outputs.touch()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    result = run_step(
        step_script(ACTION, START_STEP),
        workdir=tmp_path,
        environment={
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(outputs),
        },
    )
    assert result.returncode == 0, result.stderr
    return result.exported


def _installed(tmp_path: Path) -> dict[str, str]:
    """Run the install step and return the wrapper it exported."""
    installed = _run_install(tmp_path, {}, {})
    assert installed.returncode == 0, installed.stderr
    return installed.exported


def test_a_build_after_a_successful_start_runs_through_the_wrapper(
    tmp_path: Path,
) -> None:
    """The installed path is executable and forwards the compiler arguments."""
    environment = {**_installed(tmp_path), **_start(tmp_path, starts=True)}
    wrapper = tmp_path / ".local" / "bin" / "sccache"
    compiler = tmp_path / "fake-rustc"
    ran = tmp_path / "compiler-ran"
    compiler.write_text(
        f'#!/usr/bin/env bash\nprintf ran > "{ran}"\n', encoding="utf-8"
    )
    compiler.chmod(0o755)
    arguments = tmp_path / "wrapper-arguments"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{arguments}"\n'
        f'exec "{compiler}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = run_step(
        '"$RUSTC_WRAPPER" --crate-name demo',
        workdir=tmp_path,
        environment=environment,
    )

    assert result.returncode == 0, result.stderr
    assert arguments.read_text(encoding="utf-8").splitlines() == [
        "--crate-name",
        "demo",
    ], "the wrapper must receive the compiler arguments"
    assert ran.is_file(), "the wrapper must execute the compiler it was given"


def test_a_build_after_a_fallback_compiles_with_plain_rustc(tmp_path: Path) -> None:
    """A cleared wrapper is what makes a fallback visible, and it must be empty."""
    environment = {**_installed(tmp_path), **_start(tmp_path, starts=False)}
    assert not environment.get("RUSTC_WRAPPER"), (
        f"a fallback must clear the wrapper, got {environment.get('RUSTC_WRAPPER')!r}"
    )
    result = run_step(
        'test -z "${RUSTC_WRAPPER:-}"',
        workdir=tmp_path,
        environment=environment,
    )
    assert result.returncode == 0, "the build must not receive a wrapper after fallback"


def test_a_failing_build_still_fails_after_a_fallback(tmp_path: Path) -> None:
    """The fallback must not hide a compiler failure."""
    environment = {**_installed(tmp_path), **_start(tmp_path, starts=False)}
    result = run_step("exit 7", workdir=tmp_path, environment=environment)
    assert result.returncode == 7, (
        f"a failing build must keep its exit status, got {result.returncode}"
    )


def _fake_cargo(tmp_path: Path, status: int = 0) -> tuple[Path, Path]:
    """Write a fake Cargo that records its argv and exits ``status``."""
    binary = tmp_path / "fake-cargo"
    recorded = tmp_path / "cargo-environment"
    arguments = tmp_path / "cargo-arguments"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        f'env > "{recorded}"\n'
        f'printf "%s\\n" "$@" > "{arguments}"\n'
        f"exit {status}\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, recorded


def _adapter_environment(tmp_path: Path, cargo: Path) -> dict[str, str]:
    """Return the environment the dev-fast adapter needs."""
    return {
        "DEV_FAST_CARGO": str(cargo),
        "RUSTC_WRAPPER": str(tmp_path / ".local" / "bin" / "sccache"),
        "PATH": "/usr/bin:/bin",
    }


def test_the_dev_fast_adapter_preserves_the_compiler_wrapper(tmp_path: Path) -> None:
    """The accelerated route must not silently compile outside the cache."""
    cargo, recorded = _fake_cargo(tmp_path)
    result = run_step(
        f'/bin/bash "{ADAPTER.resolve()}" build',
        workdir=tmp_path,
        environment=_adapter_environment(tmp_path, cargo),
    )
    assert result.returncode == 0, result.stderr
    environment = dict(
        line.split("=", 1)
        for line in recorded.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert environment.get("RUSTC_WRAPPER") == str(
        tmp_path / ".local" / "bin" / "sccache"
    ), "the adapter must forward the wrapper to Cargo"


def test_the_wrapper_assertion_detects_an_adapter_that_strips_it(
    tmp_path: Path,
) -> None:
    """A mutated adapter must fail the assertion above, or it proves nothing."""
    cargo, recorded = _fake_cargo(tmp_path)
    mutated = tmp_path / "cargo-stripping"
    (tmp_path / "config.toml").write_text(
        ADAPTER.resolve().parent.joinpath("config.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    mutated.write_text(
        ADAPTER.read_text(encoding="utf-8").replace(
            "set -euo pipefail", "set -euo pipefail\nunset RUSTC_WRAPPER"
        ),
        encoding="utf-8",
    )
    mutated.chmod(0o755)
    result = run_step(
        f'/bin/bash "{mutated}" build',
        workdir=tmp_path,
        environment=_adapter_environment(tmp_path, cargo),
    )
    assert result.returncode == 0, result.stderr
    environment = dict(
        line.split("=", 1)
        for line in recorded.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert "RUSTC_WRAPPER" not in environment, (
        "the seeded fault must remove the wrapper, or the positive assertion "
        "would pass for an adapter that never had it"
    )


def test_the_adapter_replaces_itself_with_cargo(tmp_path: Path) -> None:
    """The selected executable's argv and exit status survive unchanged."""
    cargo, _recorded = _fake_cargo(tmp_path)
    arguments = tmp_path / "cargo-arguments"
    result = run_step(
        f'/bin/bash "{ADAPTER.resolve()}" build --release',
        workdir=tmp_path,
        environment=_adapter_environment(tmp_path, cargo),
    )
    assert result.returncode == 0, result.stderr
    observed = arguments.read_text(encoding="utf-8").splitlines()
    assert observed == [
        "--config",
        str(ADAPTER.resolve().parent / "config.toml"),
        "build",
        "--release",
    ], (
        "the adapter must pass the reviewed fragment and the caller's argv, "
        f"got {observed}"
    )


def test_the_adapter_preserves_a_failing_cargo_status(tmp_path: Path) -> None:
    """A failing build must stay failing through the adapter."""
    cargo, _recorded = _fake_cargo(tmp_path, status=7)
    result = run_step(
        f'/bin/bash "{ADAPTER.resolve()}" build',
        workdir=tmp_path,
        environment=_adapter_environment(tmp_path, cargo),
    )
    assert result.returncode == 7, (
        f"the adapter must exit with Cargo's status, got {result.returncode}"
    )
