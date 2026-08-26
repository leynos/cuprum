"""Boundary tests for the Skylos documented-whitelist Make target."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess  # noqa: S404 - boundary tests invoke a fixed Make command.
import sys
import tomllib
import typing as typ

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from pathlib import Path

_SHELL_SENSITIVE_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789 $;*?[]!",
    min_size=1,
    max_size=24,
).filter(lambda value: bool(value.strip()) and value == value.strip())


def _make_executable() -> str:
    """Return the absolute path to the required Make executable."""
    executable = shutil.which("make")
    assert executable is not None, "Skylos whitelist boundary tests require make"
    return executable


def _write_argument_recorder(directory: Path) -> str:
    """Create a fake Skylos CLI that serializes its arguments to a file."""
    recorder = directory / "record_skylos_arguments.py"
    recorder.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "Path('skylos-arguments.json').write_text(\n"
        "    json.dumps(sys.argv[1:]), encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(recorder))}"


def _whitelist_command(
    directory: Path,
    *,
    symbol: str,
    reason: str,
    cli: str,
) -> tuple[str, ...]:
    """Build the whitelist command for an isolated project directory."""
    return (
        _make_executable(),
        "-f",
        str(repo_root() / "Makefile"),
        "skylos-allow",
        f"SYMBOL={symbol}",
        f"REASON={reason}",
        f"SKYLOS_CLI={cli}",
        f"SKYLOS_WHITELIST_LOCK={directory / '.skylos-whitelist.lock'}",
    )


def _run_whitelist(
    directory: Path,
    *,
    symbol: str,
    reason: str,
    cli: str,
) -> subprocess.CompletedProcess[str]:
    """Run the whitelist target against an isolated project directory."""
    return subprocess.run(  # noqa: S603 - fixed Makefile and test arguments.
        _whitelist_command(directory, symbol=symbol, reason=reason, cli=cli),
        capture_output=True,
        check=False,
        cwd=directory,
        text=True,
    )


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(symbol=_SHELL_SENSITIVE_TEXT, reason=_SHELL_SENSITIVE_TEXT)
def test_whitelist_preserves_shell_sensitive_arguments(
    tmp_path: Path, symbol: str, reason: str
) -> None:
    """The Make boundary must preserve each valid whitelist argument exactly."""
    completed = _run_whitelist(
        tmp_path,
        symbol=symbol,
        reason=reason,
        cli=_write_argument_recorder(tmp_path),
    )

    assert completed.returncode == 0, (
        "Skylos whitelist boundary must accept non-empty shell-sensitive values"
    )
    recorded_arguments = json.loads(
        (tmp_path / "skylos-arguments.json").read_text(encoding="utf-8")
    )
    assert recorded_arguments == [
        "whitelist",
        symbol,
        "--reason",
        reason,
    ], "Skylos whitelist boundary must quote values and preserve argument order"


def test_whitelist_lock_preserves_concurrent_documented_entries(tmp_path: Path) -> None:
    """The whitelist lock must prevent concurrent updates losing documented entries."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.skylos.whitelist.documented]\n", encoding="utf-8"
    )
    writer = tmp_path / "write_whitelist_entry.py"
    writer.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "symbol = sys.argv[2]\n"
        "reason = sys.argv[4]\n"
        "path = Path('pyproject.toml')\n"
        "contents = path.read_text(encoding='utf-8')\n"
        "time.sleep(0.2)\n"
        "path.write_text(contents + f'{symbol} = {reason!r}\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    cli = f"{shlex.quote(sys.executable)} {shlex.quote(str(writer))}"

    first = subprocess.Popen(  # noqa: S603 - fixed Makefile and test arguments.
        _whitelist_command(tmp_path, symbol="first", reason="first reason", cli=cli),
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(  # noqa: S603 - fixed Makefile and test arguments.
        _whitelist_command(tmp_path, symbol="second", reason="second reason", cli=cli),
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_stdout, first_stderr = first.communicate()
    second_stdout, second_stderr = second.communicate()

    assert first.returncode == 0, (
        f"first Skylos whitelist update must succeed: {first_stdout}{first_stderr}"
    )
    assert second.returncode == 0, (
        f"second Skylos whitelist update must succeed: {second_stdout}{second_stderr}"
    )
    with (tmp_path / "pyproject.toml").open("rb") as configuration_file:
        configuration = tomllib.load(configuration_file)
    documented = configuration["tool"]["skylos"]["whitelist"]["documented"]
    assert documented == {"first": "first reason", "second": "second reason"}, (
        "Skylos whitelist lock must preserve every concurrent documented entry"
    )
