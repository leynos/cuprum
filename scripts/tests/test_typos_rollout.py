"""Tests for the repository spelling-policy scripts."""

from __future__ import annotations

import ast
import email.message
import importlib
import json
import os
import shlex
import subprocess  # noqa: S404 - integration tests run the pinned spelling tool.
import sys
import tomllib
import typing as typ
import urllib.error
import urllib.request
from pathlib import Path

import pytest

if typ.TYPE_CHECKING:
    import types

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SCRIPT_DIRECTORY.parent
TYPOS_VERSION = "1.48.0"


def _spelling_fixture(*parts: str) -> str:
    """Assemble a deliberate negative fixture without weakening the source gate."""
    return "".join(parts)


def _run_spelling_gate(*paths: Path) -> subprocess.CompletedProcess[str]:
    """Run the pinned spelling scanner with the repository policy."""
    command = shlex.split(
        os.environ.get(
            "TYPOS_TEST_COMMAND",
            f"uv tool run typos@{TYPOS_VERSION}",
        )
    )
    return subprocess.run(  # noqa: S603 - arguments are fixed except test paths.
        [
            *command,
            "--config",
            str(REPOSITORY_ROOT / "typos.toml"),
            "--force-exclude",
            *(str(path) for path in paths),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_spelling_target_generates_config_and_scans_all_source_types(
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
            str(REPOSITORY_ROOT / "Makefile"),
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
            ".py",
            _spelling_fixture("def organi", "se_value() -> None:\n    pass\n"),
        ),
        (".rs", _spelling_fixture("fn organi", "se_value() {}\n")),
    ],
)
def test_spelling_gate_detects_plain_british_spelling(
    tmp_path: Path,
    suffix: str,
    content: str,
) -> None:
    """The scanner rejects Oxford-incompatible prose and valid identifiers."""
    fixture = tmp_path / f"invalid{suffix}"
    plain_british = "organi" + "se"
    fixture.write_text(content, encoding="utf-8")

    result = _run_spelling_gate(fixture)

    output = result.stdout + result.stderr
    assert result.returncode != 0, f"expected spelling failure, got: {output}"
    assert plain_british in output, f"expected offending spelling in: {output}"


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        (".md", "Use `--artifact-name` to organize output.\n"),
        # The `[type.markdown]` code-span and fenced-block patterns are the
        # regression cover that the inline-code exemption never had: a quoted
        # identifier keeps the spelling of the API it names. Plain Markdown
        # prose is still gated, which
        # ``test_spelling_gate_detects_plain_british_spelling`` asserts.
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
    tmp_path: Path,
    suffix: str,
    content: str,
) -> None:
    """External contracts and deliberate fixtures remain accepted."""
    fixture = tmp_path / f"exception{suffix}"
    fixture.write_text(content, encoding="utf-8")

    result = _run_spelling_gate(fixture)

    output = result.stdout + result.stderr
    assert result.returncode == 0, f"expected documented exceptions to pass: {output}"


@pytest.mark.parametrize(
    "value",
    [
        _spelling_fixture('"arti', 'facts-owned"'),
        _spelling_fixture('"/arti', 'facts-owned"'),
        _spelling_fixture('"--arti', 'fact-name-owned"'),
    ],
)
def test_spelling_gate_rejects_longer_external_contract_near_misses(
    tmp_path: Path,
    value: str,
) -> None:
    """External-contract exceptions do not hide repository-owned extensions."""
    fixture = tmp_path / "near_miss.py"
    fixture.write_text(f"value = {value}\n", encoding="utf-8")

    result = _run_spelling_gate(fixture)

    output = result.stdout + result.stderr
    assert result.returncode != 0, f"expected longer value to fail: {output}"


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        (".py", _spelling_fixture("# `organi", "se`\n")),
        (".rs", _spelling_fixture("// `organi", "se`\n")),
    ],
)
def test_source_backticks_do_not_disable_spelling(
    tmp_path: Path,
    suffix: str,
    content: str,
) -> None:
    """Markdown code-span exceptions never apply to Python or Rust source."""
    fixture = tmp_path / f"backticks{suffix}"
    fixture.write_text(content, encoding="utf-8")

    result = _run_spelling_gate(fixture)

    output = result.stdout + result.stderr
    assert result.returncode != 0, f"expected source spelling failure: {output}"


def test_rollout_scripts_support_python_313() -> None:
    """Every rollout script parses with the declared minimum Python version."""
    for script in SCRIPT_DIRECTORY.glob("*.py"):
        ast.parse(
            script.read_text(encoding="utf-8"),
            filename=str(script),
            feature_version=(3, 13),
        )


@pytest.fixture(name="rollout_modules")
def rollout_modules_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[types.ModuleType, types.ModuleType, types.ModuleType]:
    """Import the scripts through the same top-level module path used at runtime."""
    monkeypatch.syspath_prepend(str(SCRIPT_DIRECTORY))
    names = ("typos_rollout_cache", "typos_rollout", "generate_typos_config")
    importlib.invalidate_caches()
    cache, rollout, generator = (importlib.import_module(name) for name in names)
    return cache, rollout, generator


def _dictionary_text(stem: str = "organ") -> str:
    """Return a minimal valid shared-dictionary document."""
    return (
        'schema = 1\n\n[oxford]\nstems = ["'
        + stem
        + '"]\n\n[words]\naccepted = []\n\n[words.corrections]\n\n'
        + "[patterns]\nignore = []\n\n[files]\nexclude = []\n"
    )


def test_rollout_generates_oxford_corrections(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """The shared renderer accepts Oxford forms and corrects plain-British ones."""
    _, rollout, _ = rollout_modules

    mappings = rollout.generate_word_mappings(rollout.Dictionary(stems=("organ",)))

    assert mappings["organise"] == "organize"
    assert mappings["organize"] == "organize", (
        "Oxford correction mappings must preserve 'organize'"
    )


def test_local_refresh_keeps_a_newer_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """An older local authority cannot replace a newer untracked cache."""
    _, rollout, _ = rollout_modules
    source = tmp_path / "shared.toml"
    cache = tmp_path / ".typos-base.toml"
    metadata = tmp_path / ".typos-base.json"
    source.write_text(_dictionary_text(), encoding="utf-8")
    source.touch()
    rollout.refresh_base(source, cache, metadata=metadata)
    cache.write_text(_dictionary_text("newer"), encoding="utf-8")
    cache.touch()
    source_mtime = source.stat().st_mtime_ns
    cache_mtime = max(cache.stat().st_mtime_ns, source_mtime + 1)
    os.utime(cache, ns=(cache_mtime, cache_mtime))

    result = rollout.refresh_base(source, cache, metadata=metadata)

    assert result.status == "current"
    assert rollout.load_dictionary(cache).stems == ("newer",)


def test_https_failure_reuses_valid_tracked_config(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A clean network-restricted checkout retains its reviewed policy."""
    _, rollout, generator = rollout_modules
    tracked_config = tmp_path / "typos.toml"
    tracked_config.write_text('[default]\nlocale = "en-gb"\n', encoding="utf-8")

    def unavailable(*_args: object, **_kwargs: object) -> None:
        """Model an unavailable HTTPS authority."""
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(rollout, "refresh_base", unavailable)

    result = generator.main(repository=tmp_path, source="https://example.invalid/base")

    assert result.status == "tracked-config"
    assert result.cache == tracked_config


def test_dictionary_validation_rejects_invalid_documents(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """Schema, table, string-list and correction types remain validated."""
    _, rollout, _ = rollout_modules
    source = tmp_path / "base.toml"
    invalid_documents = (
        _dictionary_text().replace("schema = 1", "schema = 2"),
        _dictionary_text().replace('[oxford]\nstems = ["organ"]', 'oxford = "bad"'),
        _dictionary_text().replace('stems = ["organ"]', "stems = [1]"),
        _dictionary_text().replace(
            "[words.corrections]", "[words.corrections]\nteh = 1"
        ),
    )

    for document in invalid_documents:
        source.write_text(document, encoding="utf-8")
        with pytest.raises((TypeError, ValueError)):
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
        "rendering the same dictionary twice must produce identical output"
    )
    assert output.read_text(encoding="utf-8") == first, (
        "the installed config must match the rendered document byte for byte"
    )
    rendered_config = tomllib.loads(first)
    assert rendered_config["default"]["locale"] == "en-gb", (
        "the global locale must stay en-gb, got "
        f"{rendered_config['default']['locale']!r}"
    )
    assert rendered_config["default"]["extend-ignore-re"] == ["https?://"], (
        "only non-Markdown patterns belong in the global scope, got "
        f"{rendered_config['default']['extend-ignore-re']!r}"
    )
    assert rendered_config["type"]["markdown"]["extend-glob"] == ["*.md"], (
        "the Markdown type must be scoped to *.md, got "
        f"{rendered_config['type']['markdown']['extend-glob']!r}"
    )
    assert rendered_config["type"]["markdown"]["extend-ignore-re"] == [
        r"`[^`\n]+`",
        r"(?s)```.*?```",
    ], (
        "code-span and fenced-block patterns must be Markdown-only, got "
        f"{rendered_config['type']['markdown']['extend-ignore-re']!r}"
    )
    assert list(output.parent.glob(".typos.toml.*")) == [], (
        "the atomic write must leave no temporary files behind"
    )


def test_offline_refresh_requires_and_reuses_valid_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """Offline mode fails closed before reusing a validated cache."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "base.toml"
    metadata = tmp_path / "base.json"

    with pytest.raises(FileNotFoundError, match="no cached shared dictionary"):
        rollout.refresh_base(
            "https://example.invalid/base", cache, metadata=metadata, offline=True
        )

    cache.write_text(_dictionary_text(), encoding="utf-8")
    result = rollout.refresh_base(
        "https://example.invalid/base", cache, metadata=metadata, offline=True
    )

    assert result.status == "offline-cache"


def test_local_refresh_switches_authority_and_records_metadata(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """A different explicit authority replaces a cache regardless of mtime."""
    _, rollout, _ = rollout_modules
    first = tmp_path / "first.toml"
    second = tmp_path / "second.toml"
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"
    first.write_text(_dictionary_text("first"), encoding="utf-8")
    second.write_text(_dictionary_text("second"), encoding="utf-8")
    os.utime(first, ns=(3_000_000_000, 3_000_000_000))
    os.utime(second, ns=(1_000_000_000, 1_000_000_000))
    rollout.refresh_base(first, cache, metadata=metadata)

    result = rollout.refresh_base(second, cache, metadata=metadata)

    assert result.status == "refreshed"
    assert rollout.load_dictionary(cache).stems == ("second",)
    assert json.loads(metadata.read_text(encoding="utf-8"))["source"] == str(
        second.resolve()
    )


def test_http_refresh_uses_validators_and_preserves_newer_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Remote refresh persists validators and sends them on the next request."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"
    requests: list[urllib.request.Request] = []

    class Response:
        """Provide the HTTP response surface consumed by the helper."""

        status = 200
        headers: typ.ClassVar[dict[str, str]] = {
            "ETag": '"estate-v1"',
            "Last-Modified": "Fri, 10 Jul 2026 08:00:00 GMT",
        }

        def read(self) -> bytes:
            """Return a valid shared dictionary."""
            return _dictionary_text().encode()

        def __enter__(self) -> Response:
            """Enter the fake response context."""
            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the fake response context."""

    def open_response(request: urllib.request.Request, *, timeout: float) -> Response:
        """Capture the request passed to the network boundary."""
        assert timeout == 30.0
        requests.append(request)
        return Response()

    monkeypatch.setattr(rollout.urllib.request, "urlopen", open_response)

    first = rollout.refresh_base(
        "https://example.test/base.toml", cache, metadata=metadata
    )
    second = rollout.refresh_base(
        "https://example.test/base.toml", cache, metadata=metadata
    )

    assert first.status == "refreshed"
    assert second.status == "current"
    assert requests[1].get_header("If-none-match") == '"estate-v1"'


def test_remote_failure_reuses_only_a_valid_stale_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Network failure keeps validated data and propagates without it."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"

    def fail(*_args: object, **_kwargs: object) -> None:
        """Model an unavailable remote authority."""
        message = "offline"
        raise urllib.error.URLError(message)

    monkeypatch.setattr(rollout.urllib.request, "urlopen", fail)

    with pytest.raises(urllib.error.URLError):
        rollout.refresh_base("https://example.test/base", cache, metadata=metadata)

    cache.write_text(_dictionary_text(), encoding="utf-8")
    result = rollout.refresh_base("https://example.test/base", cache, metadata=metadata)

    assert result.status == "stale-cache"


def test_remote_refresh_rejects_insecure_and_invalid_content(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The remote boundary requires HTTPS and validates bytes before install."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "cache.toml"
    metadata = tmp_path / "cache.json"

    with pytest.raises(ValueError, match="must use HTTPS"):
        rollout.refresh_base("http://example.test/base", cache, metadata=metadata)

    class InvalidResponse:
        """Return malformed TOML from an otherwise successful response."""

        status = 200
        headers: typ.ClassVar[dict[str, str]] = {}

        def read(self) -> bytes:
            """Return malformed bytes."""
            return b"not = [valid"

        def __enter__(self) -> InvalidResponse:
            """Enter the fake response context."""
            return self

        def __exit__(self, *_args: object) -> None:
            """Leave the fake response context."""

    monkeypatch.setattr(
        rollout.urllib.request, "urlopen", lambda *_args, **_kwargs: InvalidResponse()
    )

    with pytest.raises(tomllib.TOMLDecodeError):
        rollout.refresh_base("https://example.test/base", cache, metadata=metadata)
    assert not cache.exists()


def test_metadata_reader_handles_invalid_and_non_object_json(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """Malformed or non-object freshness metadata is safely ignored."""
    _, rollout, _ = rollout_modules
    metadata = tmp_path / "cache.json"

    metadata.write_text("not-json", encoding="utf-8")
    assert rollout._read_metadata(metadata) == {}
    metadata.write_text("[]", encoding="utf-8")
    assert rollout._read_metadata(metadata) == {}


def test_http_error_translation_handles_not_modified_and_stale_cache(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    tmp_path: Path,
) -> None:
    """HTTP status handling distinguishes current, stale and absent data."""
    _, rollout, _ = rollout_modules
    cache = tmp_path / "cache.toml"
    cache.write_text(_dictionary_text(), encoding="utf-8")
    headers = email.message.Message()
    not_modified = urllib.error.HTTPError(
        "https://example.test/base", 304, "not modified", headers, None
    )
    unavailable = urllib.error.HTTPError(
        "https://example.test/base", 503, "unavailable", headers, None
    )

    assert rollout._http_error_result(cache, not_modified).status == "current"
    assert rollout._http_error_result(cache, unavailable).status == "stale-cache"
    cache.unlink()
    with pytest.raises(urllib.error.HTTPError):
        rollout._http_error_result(cache, unavailable)


def test_remote_freshness_uses_dates_and_falls_back_on_invalid_values(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """Last-Modified comparison remains conservative for malformed dates."""
    _, rollout, _ = rollout_modules

    assert rollout._remote_is_not_newer(
        {"last_modified": "Fri, 10 Jul 2026 08:00:00 GMT"},
        {"Last-Modified": "Fri, 10 Jul 2026 07:00:00 GMT"},
    )
    assert rollout._remote_is_not_newer(
        {"last_modified": "invalid"}, {"Last-Modified": "invalid"}
    )
    assert not rollout._remote_is_not_newer({}, {"Last-Modified": "invalid"})
