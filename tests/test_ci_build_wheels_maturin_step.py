"""Exercise the build-wheels action's named native Maturin step.

Reading the YAML cannot show whether ``TARGET`` really only appends
``--target`` when set, whether ``--out`` really receives ``WHEELHOUSE``, or
whether the warning-suppression environment reaches the build process. A fake
``maturin`` records its argv and environment while the checked-in shell body
runs, covering the process boundary as well as the array-based invocation.
"""

from __future__ import annotations

import os
import typing as typ

from tests.helpers.workflow_steps import find_step, install_tool, run_bash, step_script

if typ.TYPE_CHECKING:
    from pathlib import Path

ACTION = ".github/actions/build-wheels"
STEP = "Build wheels with maturin"


def _run_maturin(
    tmp_path: Path, *, target: str, wheelhouse: str
) -> tuple[list[str], str]:
    """Run the action step and return the fake build process's inputs."""
    tools = tmp_path / "tools"
    record = tmp_path / "maturin-argv"
    environment_record = tmp_path / "maturin-warning-setting"
    install_tool(
        tools,
        "maturin",
        (
            f"printf '%s\\n' "
            f'"${{MATURIN_NO_MISSING_BUILD_BACKEND_WARNING-}}" '
            f'> "{environment_record}"\n'
            f'for arg in "$@"; do printf \'%s\\n\' "$arg" '
            f'>> "{record}"; done\n'
        ),
    )
    build_step = find_step(action=ACTION, name=STEP)
    declared_environment = build_step.get("env")
    assert isinstance(declared_environment, dict), (
        "the Maturin build step must declare an environment mapping"
    )
    environment = {
        key: value
        for key, value in declared_environment.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    environment.update({"TARGET": target, "WHEELHOUSE": wheelhouse})
    environment["PATH"] = f"{tools}:{os.environ['PATH']}"
    result = run_bash(
        step_script(action=ACTION, name=STEP),
        cwd=tmp_path,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    argv = record.read_text(encoding="utf-8").splitlines()
    warning_setting = environment_record.read_text(encoding="utf-8").removesuffix("\n")
    return argv, warning_setting


def test_empty_target_omits_target_flag(tmp_path: Path) -> None:
    """No cross-compilation target must leave the native build untouched."""
    argv, _ = _run_maturin(tmp_path, target="", wheelhouse="wheelhouse")

    assert "--target" not in argv, f"empty TARGET must omit --target, got {argv!r}"


def test_set_target_appends_target_flag_with_its_value(tmp_path: Path) -> None:
    """A configured target must reach ``maturin`` as its own argument."""
    argv, _ = _run_maturin(
        tmp_path, target="aarch64-unknown-linux-gnu", wheelhouse="wheelhouse"
    )

    assert "--target" in argv, f"a set TARGET must append --target, got {argv!r}"
    assert argv[argv.index("--target") + 1] == "aarch64-unknown-linux-gnu", (
        f"--target must be followed by its value, got {argv!r}"
    )


def test_out_flag_receives_wheelhouse(tmp_path: Path) -> None:
    """The wheelhouse input must flow through to ``--out`` unchanged."""
    wheelhouse = str(tmp_path / "my wheelhouse")
    argv, _ = _run_maturin(tmp_path, target="", wheelhouse=wheelhouse)

    assert "--out" in argv, f"maturin must be given --out, got {argv!r}"
    assert argv[argv.index("--out") + 1] == wheelhouse, (
        f"--out must be followed by the wheelhouse path, got {argv!r}"
    )


def test_wheelhouse_value_with_spaces_stays_one_argument(tmp_path: Path) -> None:
    """The step's array-based invocation must not word-split a spaced value."""
    argv, _ = _run_maturin(
        tmp_path, target="a target with spaces", wheelhouse="wheelhouse"
    )

    assert "--target" in argv, (
        f"a spaced TARGET must still append --target, got {argv!r}"
    )
    assert argv[argv.index("--target") + 1] == "a target with spaces", (
        f"a spaced TARGET must survive as one argument, got {argv!r}"
    )


def test_warning_suppression_reaches_maturin_process(tmp_path: Path) -> None:
    """The environment declared on the action step reaches Maturin itself."""
    _, warning_setting = _run_maturin(tmp_path, target="", wheelhouse="wheelhouse")

    assert warning_setting == "1", (
        "the Maturin process must receive the warning-suppression setting"
    )
