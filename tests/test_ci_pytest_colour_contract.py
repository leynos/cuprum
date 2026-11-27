"""Hold `make test` to the colour environment it pins for every pytest session.

`make test` runs the Python suite as a sequence of pytest sessions and stops at
the first failing one. CPython 3.14 and later colour `argparse` help when the
environment asks for it, so a test that compares captured help text with plain
prose fails under `FORCE_COLOR=1`, and that one failure hides every later
session. The gate therefore removes the force variables and exports a no-colour
policy before each session.

The recipes are driven rather than read, because a prefix that no recipe calls
is a policy that never applies. A stubbed `uv` records what each session was
given and never starts pytest, so the module costs one short process per
session and needs no virtual environment. Each session's own child process is
recorded too: exporting the policy is only useful if an ordinary child, the
kind a test spawns without replacing the environment, inherits it.

One case overrides `PYTEST` on the command line. The prefix deliberately lives
outside that variable, and a caller override must not be able to drop it. See
"Colour environment for the test gate" in `docs/developers-guide.md`.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, drives the local Makefile.
import sys
import textwrap
import typing as typ

import pytest

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from pathlib import Path

#: The variables that ask an interpreter or tool for colour. The gate must
#: remove all three; `PY_COLORS` is unset rather than zeroed because pytest
#: gives it the highest precedence of any colour control.
_FORCE_VARIABLES: typ.Final = ("FORCE_COLOR", "CLICOLOR_FORCE", "PY_COLORS")

#: The values the gate must export. `NO_COLOR` also covers an interpreter
#: started with `-E`, which ignores every `PYTHON*` variable.
_PINNED_VARIABLES: typ.Final = {"NO_COLOR": "1", "PYTHON_COLORS": "0"}

#: A name no recipe knows. Its survival shows the prefix clears only the
#: colour controls rather than rebuilding the environment.
_SENTINEL_NAME: typ.Final = "CUPRUM_COLOUR_SENTINEL"
_SENTINEL_VALUE: typ.Final = "kept"

#: The bootstrap session's own module, run by `test-selection`.
_SELECTION_MODULE: typ.Final = "tests/test_ci_test_selection_contract.py"
_GATE_TIMEOUT_SECONDS: typ.Final = 120

#: Matches a Makefile variable declared with `?=`, which assigns only when the
#: name is not already defined — and a name in the environment counts as
#: defined. `make` exports its own command-line overrides under that name, so a
#: suite run as `make test PYTEST_TARGETS=…` would otherwise have the nested
#: `make` below report the caller's list as though the Makefile declared it.
_CONDITIONAL_ASSIGNMENT: typ.Final = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\?=", re.MULTILINE
)

#: `make`'s own bookkeeping, which leaks in when pytest is launched from
#: `make test`; an inherited jobserver flag makes the nested `make` warn.
_MAKE_BOOKKEEPING: typ.Final = frozenset({"MAKEFLAGS", "MAKELEVEL", "MFLAGS"})

#: The stub's recorder. It accepts only the `uv run pytest` shape the recipes
#: use, records the environment the session was given, starts a child with no
#: `env=` argument so the child report shows what an ordinary spawn inherits,
#: and never starts pytest.
_RECORDER_SOURCE: typ.Final = textwrap.dedent(
    """\
    import json
    import os
    import pathlib
    import subprocess
    import sys

    VARIABLES = (
        "FORCE_COLOR",
        "NO_COLOR",
        "PYTHON_COLORS",
        "PY_COLORS",
        "CLICOLOR_FORCE",
        "CUPRUM_COLOUR_SENTINEL",
    )

    def report():
        return {name: os.environ.get(name) for name in VARIABLES}

    def child_report():
        reporter = pathlib.Path(__file__).with_name("report_environment.py")
        completed = subprocess.run(
            [sys.executable, str(reporter)],
            capture_output=True,
            check=False,
            text=True,
        )
        completed.check_returncode()
        return json.loads(completed.stdout)

    argv = sys.argv[1:]
    if argv[:2] != ["run", "pytest"]:
        raise SystemExit(64)
    record = {
        "argv": argv[2:],
        "environment": report(),
        "child": child_report(),
    }
    with pathlib.Path(os.environ["PYTEST_COLOUR_LOG"]).open("a") as log:
        print(json.dumps(record), file=log)
    """
)

#: The child the recorder starts. It is a separate file so the recorder can
#: invoke it with no `env=` argument, which makes the report evidence about
#: inheritance rather than about a mapping the recorder rebuilt.
_REPORTER_SOURCE: typ.Final = textwrap.dedent(
    """\
    import json
    import os

    VARIABLES = (
        "FORCE_COLOR",
        "NO_COLOR",
        "PYTHON_COLORS",
        "PY_COLORS",
        "CLICOLOR_FORCE",
        "CUPRUM_COLOUR_SENTINEL",
    )

    print(json.dumps({name: os.environ.get(name) for name in VARIABLES}))
    """
)


class _UvCall(typ.TypedDict):
    """One environment report recorded by the stubbed `uv`."""

    argv: list[str]
    environment: dict[str, str | None]
    child: dict[str, str | None]


class _SelectorFiles(typ.NamedTuple):
    """The individual and grouped selector files a driven run declares."""

    individual: Path
    grouped: Path


def _make_skip_reason() -> str:
    """Return why this host cannot drive the gate's recipes, if it cannot."""
    if os.environ.get("OS") == "Windows_NT":
        return "the gate's recipes run under a POSIX shell; this is Windows_NT"
    make = shutil.which("make")
    if make is None:
        return "the gate's recipes are Make targets; this host provides no make"
    if shutil.which("sh") is None:
        return "the gate's recipes run under a POSIX shell; this host has no sh"
    version = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        [make, "--version"],
        capture_output=True,
        check=False,
        text=True,
    )
    if "GNU Make" not in version.stdout:
        return "the recipes under test use GNU Make's -o option; this make is not GNU"
    return ""


_SKIP_REASON = _make_skip_reason()

pytestmark = pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON or "drivable")


@functools.cache
def _caller_owned_names() -> frozenset[str]:
    """Return environment names that would redefine a Makefile variable.

    Returns
    -------
    frozenset[str]
        Names conditionally assigned in the Makefile, plus `make`'s own
        bookkeeping variables.
    """
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    overridable = frozenset(_CONDITIONAL_ASSIGNMENT.findall(makefile))
    assert overridable, (
        "no `?=` assignments were found in the Makefile, so this scrub has "
        "stopped protecting anything; check the pattern still matches"
    )
    return overridable | _MAKE_BOOKKEEPING


def _write_stub(bin_directory: Path) -> None:
    """Write the stub `uv`, its recorder, and the child reporter.

    The `uv` stub is a POSIX shell wrapper so the recipes' own `PATH` prefix
    resolves it, and it hands straight to the absolute interpreter running this
    test, independent of whichever `uv` and interpreter `PATH` would select.
    """
    recorder = bin_directory / "record_uv_call.py"
    recorder.write_text(_RECORDER_SOURCE, encoding="utf-8")
    (bin_directory / "report_environment.py").write_text(
        _REPORTER_SOURCE, encoding="utf-8"
    )
    stub = bin_directory / "uv"
    stub.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _write_selectors(tmp_path: Path) -> _SelectorFiles:
    """Create the empty selector files a driven run declares."""
    # `PYTEST_GROUPED_DESELECT` passes the grouped target unquoted, and `make`
    # splits the list on whitespace, so the assertion below states the fixture
    # assumption rather than leaving it to a confusing failure downstream.
    selectors = _SelectorFiles(
        individual=tmp_path / "individual_case.py",
        grouped=tmp_path / "grouped_case.py",
    )
    for selector in selectors:
        selector.write_text("", encoding="utf-8")
        assert not any(character.isspace() for character in str(selector)), (
            "the recipe reads selectors as bare words, so a path with "
            f"whitespace would change the command: {str(selector)!r}"
        )
    return selectors


def _read_calls(log: Path) -> list[_UvCall]:
    """Read the stub's JSON-lines transcript in the order sessions ran."""
    return [
        typ.cast("_UvCall", json.loads(line))
        for line in log.read_text(encoding="utf-8").splitlines()
    ]


def _drive_gate(
    tmp_path: Path, *, override_pytest: bool
) -> tuple[list[_UvCall], _SelectorFiles]:
    """Drive `test-python` with stubbed tools under a hostile colour environment.

    Parameters
    ----------
    tmp_path : Path
        The test's temporary directory; every stub and selector lives beneath
        it.
    override_pytest : bool
        Whether to replace `PYTEST` on the command line with the stub. The
        policy prefix sits outside that variable, so both cases must produce
        the same reports.

    Returns
    -------
    tuple[list[_UvCall], _SelectorFiles]
        The recorded sessions in call order, and the selector files the run
        declared.

    The prerequisite targets are excluded with `-o`, so nothing builds, syncs,
    or probes a host tool: the only executable the recipe reaches is the stub.
    """
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    _write_stub(bin_directory)
    log = tmp_path / "uv-calls.jsonl"
    log.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    selectors = _write_selectors(tmp_path)
    make = shutil.which("make")
    assert make is not None, (
        "the module's skip marker only clears when `make` exists, so its "
        "absence here means the host changed mid-run"
    )
    tool_path = f"{bin_directory}{os.pathsep}{os.environ.get('PATH', '')}"
    selections = [
        f"LOCAL_TOOL_PATH={tool_path}",
        f"PYTEST_TARGETS={selectors.individual} {selectors.grouped}",
        f"PYTEST_GROUPED_TARGETS={selectors.grouped}",
        "PYTEST_WORKERS=0",
    ]
    if override_pytest:
        selections.append(f"PYTEST={bin_directory / 'uv'} run pytest")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _caller_owned_names()
    }
    environment.update({
        "HOME": str(home),
        "PYTEST_COLOUR_LOG": str(log),
        "FORCE_COLOR": "1",
        "PYTHON_COLORS": "1",
        "PY_COLORS": "1",
        "CLICOLOR_FORCE": "1",
        "NO_COLOR": "",
        _SENTINEL_NAME: _SENTINEL_VALUE,
    })
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        [
            make,
            "-j1",
            "-o",
            "build",
            "-o",
            "uv",
            "-o",
            "pytest",
            "-o",
            "ruff",
            "-o",
            "makeutil",
            "test-python",
            *selections,
        ],
        capture_output=True,
        check=False,
        cwd=repo_root(),
        env=environment,
        text=True,
        timeout=_GATE_TIMEOUT_SECONDS,
    )
    output = f"{completed.stdout}{completed.stderr}"
    assert completed.returncode == 0, (
        "`make test-python` must run every session under the colour policy; "
        f"it exited {completed.returncode}. Output:\n{output}"
    )
    return _read_calls(log), selectors


def _assert_policy(observed: dict[str, str | None], *, subject: str) -> None:
    """Assert one recorded environment carries the gate's colour policy."""
    for variable in _FORCE_VARIABLES:
        assert observed.get(variable) is None, (
            f"{subject} must not carry {variable}; the gate removes the "
            f"force-colour variables and got {observed.get(variable)!r}. "
            f"Report: {observed!r}"
        )
    for variable, value in _PINNED_VARIABLES.items():
        assert observed.get(variable) == value, (
            f"{subject} must carry {variable}={value}, got "
            f"{observed.get(variable)!r}. Report: {observed!r}"
        )
    assert observed.get(_SENTINEL_NAME) == _SENTINEL_VALUE, (
        f"{subject} must leave unrelated variables alone, got "
        f"{observed.get(_SENTINEL_NAME)!r}. Report: {observed!r}"
    )


@pytest.mark.timeout(_GATE_TIMEOUT_SECONDS + 30)
@pytest.mark.parametrize(
    "override_pytest",
    [
        pytest.param(False, id="makefile-pytest"),
        pytest.param(True, id="pytest-override"),
    ],
)
def test_every_python_gate_session_pins_colour(
    tmp_path: Path, override_pytest: bool
) -> None:
    """Each session, and its children, see one no-colour policy.

    The expected call sequence is exact rather than a set: the bootstrap
    selector check runs first, then one individual invocation, then one grouped
    `--dist loadgroup` invocation. A prefix that reached only some of them, or
    a recipe that stopped running one, changes the sequence and fails here.
    """
    records, selectors = _drive_gate(tmp_path, override_pytest=override_pytest)
    expected = [
        [_SELECTION_MODULE],
        [
            "-v",
            "-n",
            "0",
            "--deselect",
            str(selectors.grouped),
            str(selectors.individual),
        ],
        ["-v", "-n", "0", "--dist", "loadgroup", str(selectors.grouped)],
    ]
    assert [record["argv"] for record in records] == expected, (
        "the gate must run the bootstrap, individual, and grouped sessions in "
        f"that order; recorded: {[record['argv'] for record in records]!r}"
    )
    for index, record in enumerate(records):
        _assert_policy(record["environment"], subject=f"session {index} environment")
        _assert_policy(record["child"], subject=f"session {index} child environment")
