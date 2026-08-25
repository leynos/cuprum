"""Tests for the shared dictionary model, merging, and config rendering."""

from __future__ import annotations

import ast
import dataclasses as dc
import logging
import os
import re
import shlex
import subprocess  # noqa: S404 - integration tests run the pinned spelling tool.
import sys
import tomllib
import typing as typ
import urllib.error

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types
    from pathlib import Path


def _invalid_schema(dictionary_text: cabc.Callable[..., str]) -> str:
    """Return a dictionary with an unsupported schema version."""
    return dictionary_text().replace("schema = 1", "schema = 2")


def _invalid_oxford_table(dictionary_text: cabc.Callable[..., str]) -> str:
    """Return a dictionary whose Oxford section is not a table."""
    return dictionary_text().replace('[oxford]\nstems = ["organ"]', 'oxford = "bad"')


def _invalid_stems(dictionary_text: cabc.Callable[..., str]) -> str:
    """Return a dictionary containing a non-string stem."""
    return dictionary_text().replace('stems = ["organ"]', "stems = [1]")


def _invalid_corrections(dictionary_text: cabc.Callable[..., str]) -> str:
    """Return a dictionary containing a non-string correction."""
    return dictionary_text().replace(
        "[words.corrections]", "[words.corrections]\nteh = 1"
    )


@dc.dataclass(frozen=True, slots=True)
class _InvalidDictionaryCase:
    """Describe one invalid shared-dictionary document and its rejection.

    Attributes
    ----------
    document_builder : cabc.Callable[[cabc.Callable[..., str]], str]
        Builder that derives the invalid document from the valid fixture.
    error_type : type[Exception]
        The exception ``load_dictionary`` must raise for the document.
    match : str
        Regular expression the raised message must match.
    """

    document_builder: cabc.Callable[[cabc.Callable[..., str]], str]
    error_type: type[Exception]
    match: str


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


def test_rollout_scripts_parse_at_their_declared_baseline(
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
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)  # noqa: S607
    subprocess.run(  # noqa: S603
        ["git", "add", *tracked_files],  # noqa: S607
        cwd=tmp_path,
        check=True,
    )

    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
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
    return subprocess.run(  # noqa: S603 - arguments are fixed except test paths.
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


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        (".md", _spelling_fixture("organi", "se\n")),
        (".py", _spelling_fixture("def organi", "se_value() -> None:\n    pass\n")),
        (".rs", _spelling_fixture("fn organi", "se_value() {}\n")),
    ],
)
def test_spelling_gate_detects_plain_british_spelling(
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
        (".md", _spelling_fixture("Call `organi", "se` on the upstream handle.\n")),
        (".md", _spelling_fixture("```text\norgani", "se\n```\n")),
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
    assert result.returncode == 0, f"expected documented exceptions to pass: {output}"


def test_rollout_generates_oxford_corrections(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """The shared renderer accepts Oxford forms and corrects plain-British ones."""
    _, rollout, _ = rollout_modules

    mappings = rollout.generate_word_mappings(rollout.Dictionary(stems=("organ",)))

    assert mappings["organize"] == "organize", "an Oxford spelling must map to itself"
    assert mappings["organise"] == "organize", (
        "a plain-British spelling must map to its Oxford form"
    )


def test_local_refresh_keeps_a_newer_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
) -> None:
    """An unchanged local authority leaves a locally edited cache in place.

    Freshness is decided by the metadata sidecar (recorded source path and
    mtime), not by the cache file's own mtime, so no timestamp manipulation
    is needed to exercise the contract.
    """
    _, rollout, _ = rollout_modules
    source = tmp_path / "shared.toml"
    cache = tmp_path / ".typos-base.toml"
    metadata = tmp_path / ".typos-base.json"
    source.write_text(dictionary_text(), encoding="utf-8")
    rollout.refresh_base(source, cache, metadata=metadata)
    cache.write_text(dictionary_text("newer"), encoding="utf-8")

    result = rollout.refresh_base(source, cache, metadata=metadata)

    assert result.status == "current", (
        "metadata recording an unchanged source must keep the cache current"
    )
    assert rollout.load_dictionary(cache).stems == ("newer",), (
        "a current cache must retain its locally edited contents"
    )


def test_https_failure_reuses_valid_tracked_config(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A clean network-restricted checkout retains its reviewed policy."""
    _, rollout, generator = rollout_modules
    tracked_config = tmp_path / "typos.toml"
    tracked_config.write_text('[default]\nlocale = "en-gb"\n', encoding="utf-8")

    def unavailable(*_args: object, **_kwargs: object) -> None:
        """Model an unavailable HTTPS authority that always raises ``URLError``."""
        message = "offline"
        raise urllib.error.URLError(message)

    monkeypatch.setattr(rollout, "refresh_base", unavailable)

    with caplog.at_level(logging.WARNING):
        result = generator.main(
            repository=tmp_path, source="https://example.invalid/base"
        )

    assert result.status == "tracked-config", (
        "an unreachable HTTPS authority must fall back to the tracked config"
    )
    assert result.cache == tracked_config, (
        "the fallback must point at the tracked configuration file"
    )
    fallback_record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "typos_rollout.tracked_config_fallback"
    )
    assert fallback_record.error_type == "URLError", (
        "the fallback warning must classify the bounded refresh error type"
    )


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _InvalidDictionaryCase(
                document_builder=_invalid_schema,
                error_type=ValueError,
                match="schema",
            ),
            id="schema",
        ),
        pytest.param(
            _InvalidDictionaryCase(
                document_builder=_invalid_oxford_table,
                error_type=TypeError,
                match="oxford",
            ),
            id="oxford",
        ),
        pytest.param(
            _InvalidDictionaryCase(
                document_builder=_invalid_stems,
                error_type=TypeError,
                match="stems",
            ),
            id="stems",
        ),
        pytest.param(
            _InvalidDictionaryCase(
                document_builder=_invalid_corrections,
                error_type=TypeError,
                match="corrections",
            ),
            id="corrections",
        ),
    ],
)
def test_dictionary_validation_rejects_invalid_documents(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
    dictionary_text: cabc.Callable[..., str],
    case: _InvalidDictionaryCase,
) -> None:
    """Schema, table, string-list and correction types remain validated."""
    _, rollout, _ = rollout_modules
    source = tmp_path / "base.toml"
    source.write_text(case.document_builder(dictionary_text), encoding="utf-8")

    with pytest.raises(case.error_type, match=case.match):
        rollout.load_dictionary(source)


def test_merge_rejects_conflicting_corrections(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """A local overlay cannot silently weaken a shared correction."""
    _, rollout, _ = rollout_modules
    base = rollout.Dictionary(corrections=(("teh", "the"),))
    local = rollout.Dictionary(corrections=(("teh", "ten"),))

    with pytest.raises(ValueError, match="conflicting correction"):
        rollout.merge_dictionaries(base, local)


def test_render_and_write_are_deterministic_valid_toml(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """Rendering is stable, parseable and atomically installed."""
    _, rollout, _ = rollout_modules
    dictionary = rollout.Dictionary(
        stems=("organ",),
        accepted=("proper-name",),
        ignore_patterns=("https?://", r"`[^`\n]+`", r"(?s)```.*?```"),
        excluded_files=("target",),
    )
    output = tmp_path / "nested" / "typos.toml"

    first = rollout.render_typos_config(dictionary)
    rollout.write_config(output, dictionary)

    assert first == rollout.render_typos_config(dictionary), (
        "rendering must be deterministic across repeated calls"
    )
    assert output.read_text(encoding="utf-8") == first, (
        "the written file must match the rendered document"
    )
    rendered_config = tomllib.loads(first)
    assert rendered_config["default"]["locale"] == "en-gb", (
        "the global locale must stay en-gb"
    )
    assert rendered_config["default"]["extend-ignore-re"] == ["https?://"], (
        "only non-Markdown patterns belong in the global scope"
    )
    assert rendered_config["type"]["markdown"]["extend-glob"] == ["*.md"], (
        "the Markdown type must be scoped to *.md"
    )
    assert rendered_config["type"]["markdown"]["extend-ignore-re"] == [
        r"`[^`\n]+`",
        r"(?s)```.*?```",
    ], "code-span and fenced-block patterns must be Markdown-only"
    assert list(output.parent.glob(".typos.toml.*")) == [], (
        "the atomic write must leave no temporary files behind"
    )
