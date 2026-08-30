"""Tests for the spelling gate's scope, script baseline, and detection."""

from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess  # ruff: ignore[suspicious-subprocess-import] - integration tests run the pinned spelling tool.
import sys
import tomllib
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    from pathlib import Path


# The package baseline from ``pyproject.toml``. A helper script may raise its
# own floor with a PEP 723 script block; anything without one is held here.
_PACKAGE_BASELINE = (3, 12)
_PEP723_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)
_MINIMUM_VERSION = re.compile(r">=\s*(\d+)\.(\d+)")
_TYPOS_VERSION = "1.48.0"


def _declared_baseline(source: str) -> tuple[int, int]:
    """Return a script's own Python floor, or the package baseline."""
    for match in _PEP723_BLOCK.finditer(source):
        if match.group("type") != "script":
            continue
        content = "".join(
            line[2:] if line.startswith("# ") else line[1:]
            for line in match.group("content").splitlines(keepends=True)
        )
        requires = tomllib.loads(content).get("requires-python")
        if isinstance(requires, str) and (found := _MINIMUM_VERSION.search(requires)):
            return (int(found.group(1)), int(found.group(2)))
    return _PACKAGE_BASELINE


def _run_spelling_gate(
    repository: Path,
    *paths: Path,
) -> subprocess.CompletedProcess[str]:
    """Run the pinned spelling scanner with the repository policy."""
    command = shlex.split(
        os.environ.get(
            "TYPOS_TEST_COMMAND",
            f"uv tool run typos@{_TYPOS_VERSION}",
        )
    )
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - arguments are fixed except test paths.
        [
            *command,
            "--config",
            str(repository / "typos.toml"),
            "--force-exclude",
            *(str(path) for path in paths),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _spelling_fixture(*parts: str) -> str:
    """Assemble a deliberate spelling fixture without weakening the source gate."""
    return "".join(parts)


class TestSpellingGate:
    """Exercise the spelling gate's scope, fixtures, and script baseline."""

    def test_rollout_scripts_parse_at_their_declared_baseline(
        self,
        script_directory: Path,
    ) -> None:
        """Every rollout script parses at the Python floor it is held to.

        Scripts without a PEP 723 block must stay on the package baseline; one that
        declares its own ``requires-python`` is parsed at that version instead, so
        raising a script's floor is a deliberate, visible act rather than a silent
        consequence of the grammar the test happens to use.
        """
        rollout_scripts = tuple(script_directory.glob("*.py"))
        assert rollout_scripts, "the rollout script directory must contain Python files"
        for script in rollout_scripts:
            source = script.read_text(encoding="utf-8")
            ast.parse(
                source,
                filename=str(script),
                feature_version=_declared_baseline(source),
            )

    def test_spelling_target_checks_documentation_and_source(
        self,
        script_directory: Path,
    ) -> None:
        """The spelling gate checks Markdown, Python, and Rust files."""
        makefile = (script_directory.parent / "Makefile").read_text(encoding="utf-8")
        spelling_recipe = makefile.split("\nspelling:", maxsplit=1)[1].split(
            "\nspelling-helper-test:", maxsplit=1
        )[0]

        assert {"'*.md'", "'*.py'", "'*.rs'"} <= set(spelling_recipe.split()), (
            "spelling target must include Markdown, Python, and Rust pathspecs"
        )

    def test_spelling_target_generates_config_and_scans_all_source_types(
        self,
        script_directory: Path,
        tmp_path: Path,
    ) -> None:
        """The real spelling target generates config before scanning tracked source."""
        event_log = tmp_path / "events.log"
        generator = tmp_path / "generate.py"
        scanner = tmp_path / "scan.py"
        generator.write_text(
            "from pathlib import Path\n"
            f"Path({str(event_log)!r}).write_text('generate\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        scanner.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            f"log = Path({str(event_log)!r})\n"
            "suffixes = {'.md', '.py', '.rs'}\n"
            "paths = [arg for arg in sys.argv[1:] if Path(arg).suffix in suffixes]\n"
            "with log.open('a', encoding='utf-8') as output:\n"
            "    output.write('scan ' + ' '.join(paths) + '\\n')\n",
            encoding="utf-8",
        )
        tracked_files = ("guide.md", "module.py", "crate.rs")
        for filename in tracked_files:
            (tmp_path / filename).write_text("organize\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)  # ruff: ignore[start-process-with-partial-path] - the integration test drives the real git/make on PATH
        subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - the argument list is literal; no shell is involved
            ["git", "add", *tracked_files],  # ruff: ignore[start-process-with-partial-path] - the integration test drives the real git/make on PATH
            cwd=tmp_path,
            check=True,
        )

        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - the argument list is literal; no shell is involved
            [  # ruff: ignore[start-process-with-partial-path] - the integration test drives the real git/make on PATH
                "make",
                "-f",
                str(script_directory.parent / "Makefile"),
                "spelling",
                "SPELLING_HELPER_TARGET=",
                f"SPELLING_CONFIG_COMMAND={sys.executable} {generator}",
                f"TYPOS={sys.executable} {scanner}",
            ],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        events = event_log.read_text(encoding="utf-8").splitlines()
        assert events[0] == "generate", f"configuration must precede scan: {events}"
        assert events[1].startswith("scan "), f"scanner event is missing: {events}"
        assert set(events[1].removeprefix("scan ").split()) == set(tracked_files), (
            f"spelling target did not scan every tracked source type: {events}"
        )

    @pytest.mark.parametrize(
        ("suffix", "content"),
        [
            (".md", _spelling_fixture("organi", "se\n")),
            (
                ".md",
                _spelling_fixture("Call `organi", "se` on the upstream handle.\n"),
            ),
            (
                ".py",
                _spelling_fixture("def organi", "se_value() -> None:\n    pass\n"),
            ),
            (".rs", _spelling_fixture("fn organi", "se_value() {}\n")),
        ],
    )
    def test_spelling_gate_detects_plain_british_spelling(
        self,
        script_directory: Path,
        tmp_path: Path,
        suffix: str,
        content: str,
    ) -> None:
        """The scanner rejects Oxford-incompatible prose and valid identifiers."""
        fixture = tmp_path / f"invalid{suffix}"
        plain_british = "organi" + "se"
        fixture.write_text(content, encoding="utf-8")

        result = _run_spelling_gate(script_directory.parent, fixture)

        output = result.stdout + result.stderr
        assert result.returncode != 0, f"expected spelling failure, got: {output}"
        assert plain_british in output, f"expected offending spelling in: {output}"

    @pytest.mark.parametrize(
        ("suffix", "content"),
        [
            (".md", "Use `--artifact-name` to organize output.\n"),
            (".md", _spelling_fixture("```text\norgani", "se\n```\n")),
            (".md", "The API returns a `color` value.\n"),
            (".py", "build_native_wheel_artifact()\n"),
            (".md", "Pass `--artifact-server-path` to the external client.\n"),
            (
                ".py",
                'url = "https://api.github.test/actions/runs/1/artifacts?per_page=1"\n',
            ),
            (".rs", 'const KEY: &str = "artifacts";\n'),
            (".py", 'mappings["organise"] = "organize"\n'),
            (".py", 'correction = ("teh", "the")\n'),
            (".py", 'correction = ("teh", "ten")\n'),
            (".py", 'correction = ("ises", "izes")\n'),
            (".py", _spelling_fixture('chunk = b"o', "n", 'd\\n"\n')),
        ],
    )
    def test_spelling_gate_preserves_documented_exceptions(
        self,
        script_directory: Path,
        tmp_path: Path,
        suffix: str,
        content: str,
    ) -> None:
        """External contracts and deliberate fixtures remain accepted."""
        fixture = tmp_path / f"exception{suffix}"
        fixture.write_text(content, encoding="utf-8")

        result = _run_spelling_gate(script_directory.parent, fixture)

        output = result.stdout + result.stderr
        assert result.returncode == 0, (
            f"expected documented exceptions to pass: {output}"
        )
