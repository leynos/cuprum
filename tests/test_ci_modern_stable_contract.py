"""Hold the bounded modern-stable CI job to its independent route contract."""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_runners import job, placement, steps

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step


def _named_step(name: str) -> Step:
    """Return one named step from the modern-stable job."""
    matches = [
        step for step in steps("ci.yml", "modern-stable") if step.get("name") == name
    ]
    assert len(matches) == 1, f"modern-stable must declare {name!r} exactly once"
    return matches[0]


def test_modern_stable_job_is_one_bounded_linux_lane() -> None:
    """Keep the compiler experiment to one fork-reachable Linux job."""
    selected_job = job("ci.yml", "modern-stable")
    assert "strategy" not in selected_job, "modern-stable must not add a CI matrix"
    assert selected_job.get("timeout-minutes") == 90, (
        "the bounded modern-stable lane must retain its 90-minute limit"
    )
    selected_runner = placement("ci.yml", "modern-stable")
    assert selected_runner.kind == "fork", "modern stable must use the fork runner"
    assert selected_runner.owned == "ubicloud-standard-2", (
        "modern stable must use the provisioned two-vCPU runner"
    )
    assert selected_runner.fork == "ubuntu-latest", (
        "fork PRs must retain the GitHub-hosted Linux fallback"
    )

    environment = selected_job.get("env")
    assert isinstance(environment, dict), "the modern job must define its own pin"
    assert environment.get("MODERN_STABLE_TOOLCHAIN") == "1.99.1", (
        "the modern job must use the current explicit stable pin"
    )
    assert environment.get("CUPRUM_REQUIRE_MODERN_STABLE") == "1", (
        "CI must make missing modern toolchain support fail the fixture gate"
    )


def test_modern_stable_pull_requests_use_a_dedicated_changes_filter() -> None:
    """Skip the paid route for pull requests outside its validation inputs."""
    selected_job = job("ci.yml", "modern-stable")
    needs = selected_job.get("needs")
    assert needs == "changes" or (isinstance(needs, list) and "changes" in needs), (
        "modern stable must wait for its dedicated change detector"
    )
    assert selected_job.get("if") == (
        "github.event_name != 'pull_request' || needs.changes.outputs.modern == 'true'"
    ), "only pull requests without modern-stable inputs may skip the paid job"

    changes = job("ci.yml", "changes")
    outputs = changes.get("outputs")
    assert isinstance(outputs, dict), "the changes job must expose filter outputs"
    assert outputs.get("modern") == "${{ steps.filter.outputs.modern }}", (
        "modern stable must have its own filter output"
    )
    filter_step = next(
        step for step in steps("ci.yml", "changes") if step.get("id") == "filter"
    )
    filter_inputs = filter_step.get("with")
    assert isinstance(filter_inputs, dict), "the change detector needs its filters"
    filters = filter_inputs.get("filters")
    assert isinstance(filters, str), "the change detector filters must be readable"
    modern_filter = filters.split("modern:\n", maxsplit=1)[1].split(
        "rust:\n", maxsplit=1
    )[0]
    for relevant_path in (
        "cuprum/**",
        "rust/**",
        "tools/dev-fast/**",
        "conftest.py",
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        "scripts/modern_stable_warning_controls.py",
        "scripts/tests/test_modern_stable_warning_controls.py",
        "tests/helpers/cargo_warning_fixture.py",
        "tests/helpers/modern_stable_make.py",
        "tests/helpers/docs.py",
        "tests/behaviour/**",
        ".github/workflows/ci.yml",
        ".github/actions/cache-keys/action.yml",
        ".github/actions/setup-sccache/**",
    ):
        assert f"'{relevant_path}'" in modern_filter, (
            f"modern-stable filter must include {relevant_path!r}"
        )
    assert "docs/" not in modern_filter, (
        "documentation-only changes must not spend a modern-stable runner"
    )


def test_job_installs_both_toolchains_without_changing_the_default() -> None:
    """Retain the 1.85 project toolchain and install modern stable separately."""
    project = _named_step("Install project Rust toolchain")
    project_inputs = project.get("with")
    assert isinstance(project_inputs, dict), "the MSRV toolchain action needs inputs"
    assert project_inputs.get("toolchain") == "1.85.0", (
        "the modern job must retain the project compatibility toolchain"
    )
    assert project_inputs.get("cache-provider") == "external", (
        "project toolchain setup must retain the established cache provider"
    )
    assert project_inputs.get("use-sccache") == "false", (
        "the project toolchain action must not replace the shared cache setup"
    )

    modern = _named_step("Install modern stable Rust toolchain")
    modern_command = modern.get("run")
    assert isinstance(modern_command, str), "the modern pin must be installed by script"
    required_fragments = (
        'rustup toolchain install "${MODERN_STABLE_TOOLCHAIN}"',
        "--profile minimal",
        "--component clippy",
    )
    assert all(fragment in modern_command for fragment in required_fragments), (
        "the modern installer must install the explicit minimal toolchain with Clippy"
    )
    assert "rustup default" not in modern_command, (
        "installing modern stable must not change the default toolchain"
    )
    assert not any(
        step.get("uses") == "./.github/actions/setup-dev-fast"
        for step in steps("ci.yml", "modern-stable")
    ), "the modern stable job must not provision dev-fast"


def test_gates_and_lockfile_check_run_sequentially_after_provisioning() -> None:
    """Use the Makefile gates after locked dependency preparation."""
    job_steps = steps("ci.yml", "modern-stable")
    names = [str(step.get("name")) for step in job_steps]
    ordered_names = (
        "Install project Rust toolchain",
        "Install modern stable Rust toolchain",
        "Build Python environment",
        "Fetch locked Rust dependencies",
        "Run modern stable validation and fixture contracts",
    )
    positions = [names.index(name) for name in ordered_names]
    assert positions == sorted(positions), (
        "toolchain and dependency provisioning must precede modern validation"
    )

    validation = _named_step("Run modern stable validation and fixture contracts")
    commands = validation.get("run")
    assert isinstance(commands, str), "modern validation must be a shell command"
    required = (
        "make modern-stable",
        "make test-modern-stable-contract",
        "git diff --exit-code -- rust/Cargo.lock",
    )
    command_positions = [commands.index(command) for command in required]
    assert command_positions == sorted(command_positions), (
        "modern gates, fixture contracts, and lockfile check must run sequentially"
    )

    environment = validation.get("env")
    assert environment == {
        "CARGO_BUILD_JOBS": "${{ env.LINUX_RUNNER_VCPUS }}",
        "TEST_JOBS": "${{ env.LINUX_RUNNER_VCPUS }}",
    }, "Cargo jobs and test threads must follow the paid runner size"


def test_modern_job_uses_locked_fetch_and_existing_sccache_policy() -> None:
    """Keep dependency resolution locked and preserve the capped cache action."""
    fetch = _named_step("Fetch locked Rust dependencies").get("run")
    assert isinstance(fetch, str), "locked dependency preparation must run a command"
    assert "cargo fetch --locked" in fetch, (
        "dependency preparation must use the committed lockfile"
    )
    assert "RUSTUP_TOOLCHAIN=1.85.0" in fetch, (
        "dependency preparation must resolve against the declared compatibility floor"
    )

    setup = _named_step("Set up sccache")
    assert setup.get("uses") == "./.github/actions/setup-sccache", (
        "the modern lane must use the shared sccache setup action"
    )
    assert setup.get("with") is None, "the established 4G default must stay in force"

    report = _named_step("Record compiler-cache effectiveness")
    assert report.get("if") == (
        "always() && steps.sccache.outputs.status != 'fallback'"
    ), "cache reporting must retain the established fallback guard"
