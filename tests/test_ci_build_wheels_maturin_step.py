"""Exercise the build-wheels action's "Build wheels with maturin" step.

Reading the YAML cannot show whether ``TARGET`` really only appends
``--target`` when set, whether ``--out`` really receives ``WHEELHOUSE``, or
whether a value containing a space stays one argument through the step's
array-based invocation. A fake ``maturin`` that records its argv, one per
line, answers all three directly.
"""

from __future__ import annotations

import os
import typing as typ

from tests.helpers.workflow_steps import install_tool, run_bash, step_script

if typ.TYPE_CHECKING:
    from pathlib import Path

ACTION = ".github/actions/build-wheels"
STEP = "Build wheels with maturin"


def _run_maturin(tmp_path: Path, *, target: str, wheelhouse: str) -> list[str]:
    """Run the step with a fake ``maturin`` and return its recorded argv."""
    tools = tmp_path / "tools"
    record = tmp_path / "maturin-argv"
    install_tool(
        tools,
        "maturin",
        f'for arg in "$@"; do printf \'%s\\n\' "$arg" >> "{record}"; done\n',
    )
    script = step_script(action=ACTION, name=STEP)
    result = run_bash(
        script,
        cwd=tmp_path,
        env={
            "TARGET": target,
            "WHEELHOUSE": wheelhouse,
            "PATH": f"{tools}:{os.environ['PATH']}",
        },
    )
    assert result.returncode == 0, result.stderr
    return record.read_text(encoding="utf-8").splitlines()


def test_empty_target_omits_target_flag(tmp_path: Path) -> None:
    """No cross-compilation target must leave the native build untouched."""
    argv = _run_maturin(tmp_path, target="", wheelhouse="wheelhouse")

    assert "--target" not in argv, f"empty TARGET must omit --target, got {argv!r}"


def test_set_target_appends_target_flag_with_its_value(tmp_path: Path) -> None:
    """A configured target must reach ``maturin`` as its own argument."""
    argv = _run_maturin(
        tmp_path, target="aarch64-unknown-linux-gnu", wheelhouse="wheelhouse"
    )

    assert "--target" in argv, f"a set TARGET must append --target, got {argv!r}"
    assert argv[argv.index("--target") + 1] == "aarch64-unknown-linux-gnu", (
        f"--target must be followed by its value, got {argv!r}"
    )


def test_out_flag_receives_wheelhouse(tmp_path: Path) -> None:
    """The wheelhouse input must flow through to ``--out`` unchanged."""
    wheelhouse = str(tmp_path / "my wheelhouse")
    argv = _run_maturin(tmp_path, target="", wheelhouse=wheelhouse)

    assert "--out" in argv, f"maturin must be given --out, got {argv!r}"
    assert argv[argv.index("--out") + 1] == wheelhouse, (
        f"--out must be followed by the wheelhouse path, got {argv!r}"
    )


def test_wheelhouse_value_with_spaces_stays_one_argument(tmp_path: Path) -> None:
    """The step's array-based invocation must not word-split a spaced value."""
    argv = _run_maturin(
        tmp_path, target="a target with spaces", wheelhouse="wheelhouse"
    )

    assert "--target" in argv, (
        f"a spaced TARGET must still append --target, got {argv!r}"
    )
    assert argv[argv.index("--target") + 1] == "a target with spaces", (
        f"a spaced TARGET must survive as one argument, got {argv!r}"
    )
