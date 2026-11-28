"""Provide the controlled `uv` double the colour-gate contract drives.

`tests/helpers/pytest_colour_gate.py` drives the real `test-python` recipe and
asserts what each session was given. This module owns the other half: the
executable double the recipe resolves instead of `uv`, and the JSON-lines
transcript it writes.

The double never starts pytest. It accepts only the `uv run pytest` shape the
recipes use, records the environment of the session that invoked it, and starts
a child with no `env=` argument so the transcript also shows what an ordinary
spawn inherits. Keeping the recorded report in a separate process is what makes
it evidence about inheritance rather than about a mapping the caller rebuilt.
"""

from __future__ import annotations

import json
import shlex
import sys
import textwrap
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

#: The stub's recorder. It accepts only the `uv run pytest` shape the recipes
#: use, records the environment the session was given, starts a child with no
#: `env=` argument so the child report shows what an ordinary spawn inherits,
#: and never starts pytest.
RECORDER_SOURCE: typ.Final = textwrap.dedent(
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
REPORTER_SOURCE: typ.Final = textwrap.dedent(
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


class UvCall(typ.TypedDict):
    """One environment report recorded by the stubbed `uv`."""

    argv: list[str]
    environment: dict[str, str | None]
    child: dict[str, str | None]


def write_stub(bin_directory: Path) -> None:
    """Write the stub `uv`, its recorder, and the child reporter.

    The `uv` stub is a POSIX shell wrapper so the recipes' own `PATH` prefix
    resolves it, and it hands straight to the absolute interpreter running the
    contract, independent of whichever `uv` and interpreter `PATH` would select.
    """
    recorder = bin_directory / "record_uv_call.py"
    recorder.write_text(RECORDER_SOURCE, encoding="utf-8")
    (bin_directory / "report_environment.py").write_text(
        REPORTER_SOURCE, encoding="utf-8"
    )
    stub = bin_directory / "uv"
    stub.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def read_calls(log: Path) -> list[UvCall]:
    """Read the stub's JSON-lines transcript in the order sessions ran."""
    return [
        typ.cast("UvCall", json.loads(line))
        for line in log.read_text(encoding="utf-8").splitlines()
    ]
