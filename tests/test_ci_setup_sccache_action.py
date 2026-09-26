"""Execute the setup-sccache action's backend selection under controlled fixtures.

sccache binds its store once, when its server starts, and then reports a
plausible hit rate whatever it bound, so both ways of getting the selection
wrong are silent in a green run. Reading the action's text cannot show which
variables the step really exports or whether a refusal really stops the step:
an ``exit 1`` elsewhere in the script satisfies a search for one. These tests
run the install step's own ``run`` body under ``bash`` and assert the exit
status and the ``GITHUB_ENV`` it leaves behind.

Nothing is downloaded. A fake ``sccache`` sits where the step looks for a
restored binary, and its digest is passed as the pin, so the step takes its
reuse path; ``curl`` is replaced by a command that fails, so a regression that
reached for the network would fail here rather than fetch a release.
"""

from __future__ import annotations

import hashlib
import os
import re
import typing as typ

import pytest

from tests.helpers.composite_actions import (
    StepResult,
    action_document,
    run_step,
    step_script,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

ACTION = ".github/actions/setup-sccache"
INSTALL_STEP = "Install sccache from the pinned release"
#: The action's own defaults for the version and the directory ceiling, which
#: the rendered ``env`` block supplies when a test passes no such input.
VERSION = "0.12.0"
CACHE_SIZE = "4G"
#: The only shape a value in the install step's ``env`` block may take.
_INPUT_REFERENCE = re.compile(r"\$\{\{\s*inputs\.(?P<name>[\w-]+)\s*\}\}")
PROXY_CREDENTIALS = {
    "ACTIONS_CACHE_URL": "https://cache-proxy.invalid/",
    "ACTIONS_RUNTIME_TOKEN": "runtime-token",
}
#: The variables that bind the directory backend. Either one present in a job
#: that also enables the Actions backend leaves sccache choosing between them.
DIRECTORY_VARIABLES = frozenset({"SCCACHE_DIR", "SCCACHE_CACHE_SIZE"})


def _write_program(path: Path, body: str) -> None:
    """Write one executable shell command for the step to find."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _step_environment(inputs: dict[str, str]) -> dict[str, str]:
    """Render the install step's ``env`` block the way the runner would.

    The step reads its settings from ``env``, and each value must be exactly
    ``${{ inputs.<name> }}``. Rendering the declared block, rather than
    injecting the variables the script reads, is what tests the wiring: a
    hard-coded or missing mapping would ignore a caller's ``backend: gha``
    and leave sccache on a directory that dies with the runner.

    Returns
    -------
    dict[str, str]
        Each declared variable resolved from ``inputs`` or the input's default.
    """
    document = action_document(ACTION)
    declared_inputs = typ.cast("dict[str, dict[str, object]]", document["inputs"])
    steps = typ.cast("dict[str, list[dict[str, object]]]", document["runs"])["steps"]
    (step,) = [step for step in steps if step.get("name") == INSTALL_STEP]
    rendered: dict[str, str] = {}
    for variable, value in typ.cast("dict[str, str]", step["env"]).items():
        reference = _INPUT_REFERENCE.fullmatch(value)
        assert reference is not None, (
            f"{variable} must be read from an input, got {value!r}"
        )
        name = reference.group("name")
        default = declared_inputs[name].get("default")
        rendered[variable] = inputs.get(name, str(default))
    return rendered


def _run_install(
    tmp_path: Path, inputs: dict[str, str], credentials: dict[str, str]
) -> StepResult:
    """Run the install step with the given action inputs and runner credentials.

    The fake binary's digest is computed here and passed as the
    ``binary-sha256`` input, which is what makes the step reuse it instead of
    downloading a release.

    Returns
    -------
    StepResult
        The step's exit status, output, and ``GITHUB_ENV`` exports.
    """
    binary = tmp_path / ".local" / "bin" / "sccache"
    _write_program(binary, f"echo 'sccache {VERSION}'")
    commands = tmp_path / "commands"
    _write_program(commands / "curl", "echo 'curl must not run' >&2\nexit 97")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    pinned = {
        **inputs,
        "binary-sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    return run_step(
        step_script(ACTION, INSTALL_STEP),
        workdir=tmp_path,
        environment={
            "PATH": f"{commands}:{os.environ['PATH']}",
            "RUNNER_OS": "Linux",
            "RUNNER_ARCH": "X64",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_PATH": str(tmp_path / "github_path"),
            **_step_environment(pinned),
            **credentials,
        },
    )


def test_the_local_backend_exports_only_the_directory(tmp_path: Path) -> None:
    """``local`` needs no credentials and binds the capped directory alone.

    No ``backend`` input is passed, so this is also the default: a caller that
    names no store gets the directory, never the Actions service.
    """
    result = _run_install(tmp_path, {}, {})

    assert result.returncode == 0, result.stderr
    assert result.exported.get("SCCACHE_DIR") == str(tmp_path / ".cache" / "sccache"), (
        f"the directory backend must bind the home cache, got {result.exported}"
    )
    assert result.exported.get("SCCACHE_CACHE_SIZE") == CACHE_SIZE, (
        f"the directory backend must cap the directory, got {result.exported}"
    )
    assert "SCCACHE_GHA_ENABLED" not in result.exported, (
        "the directory backend must not also enable the Actions backend"
    )
    assert result.exported.get("RUSTC_WRAPPER") == str(
        tmp_path / ".local" / "bin" / "sccache"
    ), "the step must install the wrapper it verified"


def test_the_actions_backend_exports_only_the_actions_switch(tmp_path: Path) -> None:
    """``gha`` with the proxy credentials enables that backend and no directory."""
    result = _run_install(tmp_path, {"backend": "gha"}, PROXY_CREDENTIALS)

    assert result.returncode == 0, result.stderr
    assert result.exported.get("SCCACHE_GHA_ENABLED") == "true", (
        f"the Actions backend must be enabled, got {result.exported}"
    )
    assert not DIRECTORY_VARIABLES & result.exported.keys(), (
        f"the Actions backend must not also bind a directory, got "
        f"{sorted(DIRECTORY_VARIABLES & result.exported.keys())}"
    )
    assert result.exported.get("RUSTC_WRAPPER") == str(
        tmp_path / ".local" / "bin" / "sccache"
    ), "the step must install the wrapper it verified"


@pytest.mark.parametrize("missing", sorted(PROXY_CREDENTIALS))
@pytest.mark.parametrize("absence", ["empty", "unset"])
def test_the_actions_backend_fails_without_either_credential(
    tmp_path: Path, missing: str, absence: str
) -> None:
    """A missing proxy credential fails the step and binds no backend at all.

    The alternative has no symptom: a green job that compiled everything and
    cached it to a directory that dies with the runner. Empty and unset are
    both cases because the runner can hand a step either.
    """
    credentials = dict(PROXY_CREDENTIALS)
    if absence == "empty":
        credentials[missing] = ""
    else:
        del credentials[missing]

    result = _run_install(tmp_path, {"backend": "gha"}, credentials)

    assert result.returncode != 0, f"the step passed without {missing}"
    assert missing in result.stderr, result.stderr
    assert "SCCACHE_GHA_ENABLED" not in result.exported, (
        "a refused Actions backend must not be enabled"
    )
    assert not DIRECTORY_VARIABLES & result.exported.keys(), (
        "a refused Actions backend must not fall back to a directory"
    )


def test_an_unknown_backend_fails_before_anything_is_exported(tmp_path: Path) -> None:
    """A misspelt backend is refused rather than read as either store."""
    result = _run_install(tmp_path, {"backend": "s3"}, PROXY_CREDENTIALS)

    assert result.returncode != 0, "the step accepted an unknown backend"
    assert "backend must be 'local' or 'gha'" in result.stderr, result.stderr
    assert not result.exported, (
        f"a refused backend must export nothing, got {sorted(result.exported)}"
    )
