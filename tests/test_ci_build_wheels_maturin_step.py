"""Exercise the build-wheels action's Maturin steps.

Reading the YAML cannot show whether ``TARGET`` really only appends
``--target`` when set, whether ``--out`` really receives ``WHEELHOUSE``, or
whether the warning-suppression environment reaches the build process. A fake
``maturin`` records its argv and environment while the checked-in shell body
runs, covering the process boundary as well as the array-based invocation.

The same applies to the install step ahead of it: a fake ``python`` records
each argument it was given separately, so the exact-release guard, the pinned
``pip`` requirement, and the read-back of the installed release are exercised
rather than asserted from the recipe text.
"""

from __future__ import annotations

import os
import shlex
import typing as typ

import pytest

from tests.helpers.workflow_steps import find_step, install_tool, run_bash, step_script

if typ.TYPE_CHECKING:
    import subprocess
    from pathlib import Path

ACTION = ".github/actions/build-wheels"
STEP = "Build wheels with maturin"
INSTALL_STEP = "Install maturin"


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
            f"> {shlex.quote(str(environment_record))}\n"
            f'for arg in "$@"; do printf \'%s\\n\' "$arg" '
            f">> {shlex.quote(str(record))}; done\n"
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


#: A line that can only be a boundary between calls, never an argument.
_CALL_BOUNDARY = "--call--"

#: The separate arguments the step must pass to pip and to the version probe.
_EXPECTED_INSTALL_ARGV = ("-m", "pip", "install", "--upgrade", "maturin==1.15.0")
_EXPECTED_VERSION_ARGV = ("-m", "maturin", "--version")


def _install_python_stub(tools: Path, *, record: Path, installed: str) -> None:
    """Write a ``python`` stand-in that records argv and reports ``installed``.

    Each argument is recorded on its own line and every call starts with a
    boundary line, so a single argument containing ``-m pip install`` cannot
    pass for the separate arguments the step must pass. The stand-in validates
    the argument boundaries itself; a quoting regression therefore fails the
    invocation instead of reaching a permissive ``"$*"`` match.
    """
    record_path = shlex.quote(str(record))
    install_tool(
        tools,
        "python",
        (
            f"printf '%s\\n' '{_CALL_BOUNDARY}' >> {record_path}\n"
            f'for arg in "$@"; do printf \'%s\\n\' "$arg" '
            f">> {record_path}; done\n"
            'if [ "$#" -eq 5 ] && [ "$1" = "-m" ] && [ "$2" = "pip" ] &&\n'
            '   [ "$3" = "install" ] && [ "$4" = "--upgrade" ]; then\n'
            '  case "$5" in\n'
            "    maturin==*) exit 0 ;;\n"
            "  esac\n"
            "fi\n"
            'if [ "$#" -eq 3 ] && [ "$1" = "-m" ] && [ "$2" = "maturin" ] &&\n'
            '   [ "$3" = "--version" ]; then\n'
            f"  printf 'maturin %s\\n' {shlex.quote(installed)}\n"
            "  exit 0\n"
            "fi\n"
            "printf 'unexpected python invocation: %s\\n' \"$*\" >&2\n"
            "exit 2\n"
        ),
    )


def _python_invocations(record: Path) -> list[tuple[str, ...]]:
    """Return each recorded python call's separate arguments, in call order."""
    if not record.exists():
        return []
    calls = record.read_text(encoding="utf-8").split(f"{_CALL_BOUNDARY}\n")
    return [tuple(call.splitlines()) for call in calls if call]


def _run_install(
    tmp_path: Path, *, requested: str, installed: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[tuple[str, ...]], Path]:
    """Run the install step and return its result, python calls, and record path."""
    tools = tmp_path / "tools"
    record = tmp_path / "python-argv"
    _install_python_stub(
        tools,
        record=record,
        installed=requested if installed is None else installed,
    )
    install_step = find_step(action=ACTION, name=INSTALL_STEP)
    declared_environment = install_step.get("env")
    assert isinstance(declared_environment, dict), (
        "the Maturin install step must declare an environment mapping"
    )
    environment = {
        key: value
        for key, value in declared_environment.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    environment["MATURIN_VERSION"] = requested
    environment["PATH"] = f"{tools}:{os.environ['PATH']}"
    result = run_bash(
        step_script(action=ACTION, name=INSTALL_STEP),
        cwd=tmp_path,
        env=environment,
    )
    return result, _python_invocations(record), record


def test_an_exact_release_is_installed_and_read_back(tmp_path: Path) -> None:
    """A well-formed pin installs that exact release and is then verified."""
    result, invocations, _ = _run_install(tmp_path, requested="1.15.0")

    assert result.returncode == 0, result.stderr
    assert invocations == [_EXPECTED_INSTALL_ARGV, _EXPECTED_VERSION_ARGV], (
        "the step must install the exact pin as separate arguments and read it "
        f"back, got {invocations!r}"
    )


@pytest.mark.parametrize(
    "requested",
    ["1.15", ">=1.0", "~=1.15.0", "1.15.0; rm -rf /", "1.15.0rc1"],
    ids=["two-part", "range", "compatible", "marker", "prerelease"],
)
def test_a_non_exact_release_is_refused_before_pip(
    tmp_path: Path, requested: str
) -> None:
    """Anything but ``X.Y.Z`` fails the step without reaching ``pip``."""
    result, invocations, record = _run_install(tmp_path, requested=requested)

    assert result.returncode != 0, (
        f"maturin-version {requested!r} must be refused, got success"
    )
    assert "exact X.Y.Z release" in result.stderr, (
        f"the refusal must name the required shape, got {result.stderr!r}"
    )
    assert not record.exists(), (
        f"the guard must run before pip, but python was invoked: {invocations!r}"
    )


def test_a_different_installed_release_fails_the_step(tmp_path: Path) -> None:
    """A stale preinstalled maturin must not pass as the requested pin."""
    result, _, _ = _run_install(tmp_path, requested="1.15.0", installed="1.14.0")

    assert result.returncode != 0, (
        "an installed release that differs from the pin must fail the step"
    )
    assert "maturin 1.14.0" in result.stderr, (
        f"the failure must name what pip left installed, got {result.stderr!r}"
    )
    assert "maturin 1.15.0" in result.stderr, (
        f"the failure must name the requested pin, got {result.stderr!r}"
    )


def test_both_recorders_survive_a_workspace_path_with_an_apostrophe(
    tmp_path: Path,
) -> None:
    """A quote in the temporary directory must not break the stand-ins."""
    workspace = tmp_path / "O'Brien"
    workspace.mkdir()

    argv, warning_setting = _run_maturin(workspace, target="", wheelhouse="wheelhouse")
    assert warning_setting == "1", "the build recorder must survive a quoted path"
    assert "--out" in argv, f"the build must still record argv, got {argv!r}"

    result, invocations, _ = _run_install(workspace, requested="1.15.0")
    assert result.returncode == 0, result.stderr
    assert invocations == [_EXPECTED_INSTALL_ARGV, _EXPECTED_VERSION_ARGV], (
        f"the install recorder must survive a quoted path, got {invocations!r}"
    )
