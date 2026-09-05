"""Tests for the lint and typecheck toolchain pins staying synchronized.

The Ruff and ty versions are pinned in three places: the Makefile defaults
(`RUFF_VERSION ?=` / `TY_VERSION ?=`), the workflow-level ``env`` block in
``.github/workflows/ci.yml`` (which overrides the Makefile defaults via
``?=`` semantics), and the ``ruff==`` / ``ty==`` dev dependencies in
``pyproject.toml`` (which pin the virtual environment's copies for editors
and ad hoc use). A mismatch between any two sites means CI and local runs
lint or typecheck with different rule sets, so drift must fail on the pull
request that introduces it.

These tests assert parity between the sites and that each pin has a
release-version shape; they deliberately do not assert a specific version,
so routine bump commits touching all sites together pass without editing
this module.

The df12-python-lints git ref is selected twice — ``DF12_PYTHON_LINTS_REF``
in the Makefile and the git URL in the pyproject dev group — and gets the
same parity treatment. Both must use the controlled ``v0.3.0`` release tag.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - reads fixed local Makefile recipes.
import tomllib
import typing as typ

import pytest
import yaml

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import pathlib as pth

#: A release version: dotted digit groups, no range operators or wildcards.
_VERSION_SHAPE_RE = re.compile(r"\d+(?:\.\d+)+")

#: (tool package name, Makefile/ci.yml env var name, human-readable subject)
#: for each lint/typecheck tool whose pin is synchronized across sites.
_TOOL_PIN_SITES = (
    ("ruff", "RUFF_VERSION", "Ruff"),
    ("ty", "TY_VERSION", "ty"),
)

_MAKEFILE_PIN_RE_TEMPLATE = r"^{name}\s*\?=\s*(\S+)\s*$"

_DF12_PYPROJECT_REF_RE = re.compile(
    r"df12-python-lints @ git\+https://github\.com/leynos/df12-python-lints"
    r"\.git@(\S+)"
)


class StepInputs(typ.TypedDict, total=False):
    """Inputs read from a CI workflow step."""

    key: str
    toolchain: str
    version: str


# `with` is a Python keyword, so declare that TypedDict key functionally.
_StepWith = typ.TypedDict("_StepWith", {"with": StepInputs}, total=False)


class Step(_StepWith, total=False):
    """Fields read from a CI workflow step."""

    name: str
    run: str
    uses: str


class Job(typ.TypedDict, total=False):
    """Fields read from the lint-test CI job."""

    env: dict[str, str]
    steps: list[Step]


class Workflow(typ.TypedDict, total=False):
    """Fields read from the CI workflow document."""

    env: dict[str, str]
    jobs: dict[str, Job]


def _ci_workflow(root: pth.Path) -> Workflow:
    """Read the CI workflow after verifying its outer mapping shape."""
    workflow = yaml.safe_load(
        (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    assert isinstance(workflow, dict), "ci.yml must parse to a mapping"
    return typ.cast("Workflow", workflow)


def _read_makefile_pin(root: pth.Path, name: str) -> str:
    """Read a `NAME ?= value` default from the repository Makefile."""
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    pattern = re.compile(
        _MAKEFILE_PIN_RE_TEMPLATE.format(name=re.escape(name)),
        re.MULTILINE,
    )
    match = pattern.search(makefile)
    assert match is not None, f"Makefile does not define a {name} ?= default"
    return match.group(1)


def _read_workflow_env(root: pth.Path, name: str) -> str:
    """Read a workflow-level env value from ci.yml."""
    workflow = _ci_workflow(root)
    env = workflow.get("env")
    assert isinstance(env, dict), "ci.yml must declare a workflow-level env block"
    value = env.get(name)
    assert isinstance(value, str), f"ci.yml env must pin {name} as a string"
    return value


def _lint_test_job(root: pth.Path) -> Job:
    """Read the lint-test job from the CI workflow."""
    workflow = _ci_workflow(root)
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "ci.yml must declare a jobs mapping"
    job = jobs.get("lint-test")
    assert isinstance(job, dict), "ci.yml must declare the lint-test job"
    return job


def _lint_test_step(job: Job, name: str) -> Step:
    """Read a named step from the lint-test job."""
    steps = job.get("steps")
    assert isinstance(steps, list), "the lint-test job must declare steps"
    for step in typ.cast("list[object]", steps):
        if not isinstance(step, dict) or step.get("name") != name:
            continue
        return typ.cast("Step", step)
    pytest.fail(f"the lint-test job must declare an {name!r} step")


def _lint_test_step_script(job: Job, name: str) -> str:
    """Read the named run script from the lint-test job."""
    script = _lint_test_step(job, name).get("run")
    assert isinstance(script, str), f"the {name!r} step must run a script"
    return script


def _dev_dependencies(root: pth.Path) -> list[str]:
    """Read the pyproject dev dependency group."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject.get("dependency-groups", {}).get("dev")
    assert isinstance(dev, list), "pyproject.toml must declare a dev group"
    return dev


def _read_pyproject_pin(root: pth.Path, package: str) -> str:
    """Read the `package==version` pin from the pyproject dev group."""
    prefix = f"{package}=="
    pins = [
        dep.removeprefix(prefix)
        for dep in _dev_dependencies(root)
        if dep.startswith(prefix)
    ]
    assert len(pins) == 1, (
        f"expected exactly one '{prefix}' pin in the pyproject dev group, "
        f"found {pins!r}"
    )
    return pins[0]


def _read_pin_sites(root: pth.Path, tool: str, env_name: str) -> dict[str, str]:
    """Read one tool's version pin from every synchronized location."""
    return {
        "Makefile": _read_makefile_pin(root, env_name),
        "ci.yml": _read_workflow_env(root, env_name),
        "pyproject.toml": _read_pyproject_pin(root, tool),
    }


def _expanded_make_recipes(root: pth.Path, *, ruff_pin: str, ty_pin: str) -> str:
    """Return the dry-run expansion of the lint and typecheck recipes."""
    make_executable = shutil.which("make")
    assert make_executable is not None, "make must be available to expand recipes"
    # The fixed local Makefile command only expands recipes; it runs no target.
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [
            make_executable,
            "--dry-run",
            f"RUFF_VERSION={ruff_pin}",
            f"TY_VERSION={ty_pin}",
            "lint",
            "typecheck",
        ],
        check=True,
        shell=False,
        cwd=root,
        capture_output=True,
        encoding="utf-8",
    )
    return completed.stdout


def _assert_pins_agree(pins: dict[str, str], subject: str) -> None:
    """Assert every pin site carries the same value."""
    assert len(set(pins.values())) == 1, (
        f"Expected one {subject} pin across all sites, found {pins!r}"
    )


@pytest.mark.parametrize(
    ("tool", "env_name", "subject"),
    _TOOL_PIN_SITES,
    ids=[subject for _, _, subject in _TOOL_PIN_SITES],
)
def test_tool_pins_are_synchronized(tool: str, env_name: str, subject: str) -> None:
    """A tool's pin is identical in the Makefile, ci.yml, and pyproject."""
    _assert_pins_agree(_read_pin_sites(repo_root(), tool, env_name), subject)


def test_ruff_and_ty_pins_are_release_versions() -> None:
    """Each pin is an exact release version, not a range or wildcard."""
    root = repo_root()
    for tool, env_name, _subject in _TOOL_PIN_SITES:
        for site, value in _read_pin_sites(root, tool, env_name).items():
            assert _VERSION_SHAPE_RE.fullmatch(value), (
                f"{site} pins {tool} as {value!r}, which is not an exact "
                "dotted release version"
            )


def test_mdtablefix_uses_its_pinned_prebuilt_installer() -> None:
    """The formatter uses a pinned prebuilt installer, not a Rust fallback."""
    job = _lint_test_job(repo_root())
    toolchain_setup = _lint_test_step(job, "Install Rust toolchain")
    toolchain_configuration = toolchain_setup.get("with")
    assert isinstance(toolchain_configuration, dict), (
        "the Install Rust toolchain CI mapping must declare a with field"
    )
    assert toolchain_configuration.get("toolchain") == "1.85.0", (
        "the Install Rust toolchain CI mapping must keep with.toolchain at 1.85.0"
    )

    environment = job.get("env")
    assert isinstance(environment, dict), "the lint-test job must declare env"
    assert environment.get("MDTABLEFIX_VERSION") == "0.5.1", (
        "the lint-test CI mapping must set env.MDTABLEFIX_VERSION to 0.5.1"
    )
    assert "MDTABLEFIX_RUST_VERSION" not in environment, (
        "the lint-test CI mapping must not retain the removed formatter source "
        "toolchain pin"
    )

    cache = _lint_test_step(job, "Cache mdtablefix")
    cache_configuration = cache.get("with")
    assert isinstance(cache_configuration, dict), (
        "the Cache mdtablefix CI mapping must declare a with field"
    )
    cache_key = cache_configuration.get("key")
    assert isinstance(cache_key, str), (
        "the Cache mdtablefix CI mapping must declare with.key as a string"
    )
    for name in (
        "MDTABLEFIX_VERSION",
        "UBUNTU_RELEASE",
        "CACHE_GENERATION",
    ):
        assert f"env.{name}" in cache_key, (
            f"the Cache mdtablefix CI mapping must include {name} in with.key"
        )

    whitaker_script = _lint_test_step_script(job, "Install Whitaker")
    assert "cargo binstall -V >/dev/null 2>&1" in whitaker_script, (
        "the Install Whitaker CI step must probe cargo binstall with -V"
    )


def test_make_lint_and_typecheck_use_the_pinned_tool_commands() -> None:
    """The dry-run recipes invoke Ruff and ty through their synchronized pins."""
    root = repo_root()
    ruff_pin = _read_makefile_pin(root, "RUFF_VERSION")
    ty_pin = _read_makefile_pin(root, "TY_VERSION")
    recipes = _expanded_make_recipes(root, ruff_pin=ruff_pin, ty_pin=ty_pin)

    assert f"uv tool run --from 'ruff=={ruff_pin}' ruff check" in recipes, (
        "make lint must run Ruff through the pinned uv tool command"
    )
    assert f"uv tool run --from 'ty=={ty_pin}' ty check --python .venv" in recipes, (
        "make typecheck must run ty through its pin against the project venv"
    )


def _read_df12_refs(root: pth.Path) -> dict[str, str]:
    """Read the df12-python-lints git ref from both configured locations."""
    df12_deps = [
        match.group(1)
        for dep in _dev_dependencies(root)
        if (match := _DF12_PYPROJECT_REF_RE.search(dep)) is not None
    ]
    assert len(df12_deps) == 1, (
        f"expected exactly one df12-python-lints git dependency, found {df12_deps!r}"
    )
    return {
        "Makefile": _read_makefile_pin(root, "DF12_PYTHON_LINTS_REF"),
        "pyproject.toml": df12_deps[0],
    }


def test_df12_python_lints_refs_are_synchronized() -> None:
    """The df12-python-lints ref matches between the Makefile and pyproject."""
    _assert_pins_agree(_read_df12_refs(repo_root()), "df12-python-lints")


def test_df12_python_lints_refs_use_the_controlled_release_tag() -> None:
    """Both df12-python-lints refs select the controlled v0.3.0 tag."""
    for site, ref in _read_df12_refs(repo_root()).items():
        assert ref == "v0.3.0", (
            f"{site} selects df12-python-lints at {ref!r}, not the controlled "
            "v0.3.0 release tag"
        )
