# Scripting standards

Project scripts must prioritize clarity, reproducibility, and testability. The
baseline tooling is Python and the [`uv`](https://github.com/astral-sh/uv)
launcher so that scripts remain dependency‑self‑contained and easy to execute
in Continuous Integration (CI) or locally.

Cyclopts is the default command‑line interface (CLI) framework for new and
updated scripts. This document supersedes prior guidance that recommended Typer
as a default.

## Rationale for adopting Cyclopts

- Environment‑first configuration without glue. Cyclopts reads environment
  variables with a defined prefix (for example, `INPUT_`) and maps them to
  parameters directly. Bash argument assembly, and bespoke parsing, can be
  removed.
- Typed lists and paths from env. Parameters annotated as `list[str]` or
  `list[pathlib.Path]` are populated from whitespace‑ or delimiter‑separated
  environment values. Custom split/trim helpers are unnecessary.
- Clear precedence model. CLI flags override environment variables, which
  override code defaults. Behaviour is predictable in both CI and local runs.
- Small API surface. The API is explicit and integrates cleanly with type
  hints, aiding readability and testing.
- Backwards‑compatible migration. Option aliases and per‑parameter
  environment variable names permit preservation of existing interfaces while
  removing shell glue.

## Language and runtime

- Target Python 3.13 for all new scripts. Older versions may only be used when
  integration constraints require them, and any exception must be documented
  inline.
- Each script starts with an `uv` script block, so runtime and dependency
  expectations travel with the file. Prefer the shebang
  `#!/usr/bin/env -S uv run python` followed by the metadata block shown in the
  example below.
- External processes are invoked via `cuprum==0.1.0` to provide structured
  command execution rather than ad‑hoc shell strings.
- File‑system interactions use `pathlib.Path`. Higher‑level operations (for
  example, copying or removing trees) go through the `shutil` standard library
  module.

### Minimal script (no CLI)

```python
#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = ["cuprum==0.1.0"]
# ///

from __future__ import annotations

from pathlib import Path
from cuprum import ExecutionContext, Program, scoped
from _cuprum_helpers import build_catalogue, build_commands

TOFU = Program("tofu")
CATALOGUE = build_catalogue(TOFU)
COMMANDS = build_commands(catalogue=CATALOGUE, programs=(TOFU,))
tofu = COMMANDS[TOFU]


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    cluster_dir = project_root / "infra" / "clusters" / "dev"
    context = ExecutionContext(cwd=cluster_dir.as_posix())

    with scoped(allowlist=CATALOGUE.allowlist):
        result = tofu("plan").run_sync(context=context)
    if not result.ok:
        print(result.stderr.rstrip())
        return result.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### Cyclopts CLI pattern (environment‑first)

Employ Cyclopts when a script requires parameters, particularly under CI with
`INPUT_*` variables.

```python
#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = ["cyclopts>=2.9", "cuprum==0.1.0"]
# ///

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import cyclopts
from cyclopts import App, Parameter
from cuprum import ExecutionContext, Program, scoped
from _cuprum_helpers import build_catalogue, build_commands

TOFU = Program("tofu")
CATALOGUE = build_catalogue(TOFU)
COMMANDS = build_commands(catalogue=CATALOGUE, programs=(TOFU,))
tofu = COMMANDS[TOFU]

# Map INPUT_<PARAM> → function parameter without additional glue
app = App(config=cyclopts.config.Env("INPUT_", command=False))


@app.default
def main(
    *,
    # Required parameters
    bin_name: Annotated[str, Parameter(required=True)],
    version: Annotated[str, Parameter(required=True)],

    # Optional scalars
    package_name: str | None = None,
    target: str | None = None,
    outdir: Path | None = None,
    dry_run: bool = False,

    # Lists (whitespace/newline separated by default)
    formats: list[str] | None = None,
    man_paths: Annotated[list[Path] | None, Parameter(env_var="INPUT_MAN_PATHS")] = None,
    deb_depends: list[str] | None = None,
    rpm_depends: list[str] | None = None,
):
    name = package_name or bin_name

    project_root = Path(__file__).resolve().parents[1]
    build_dir = (outdir or (project_root / "dist")) / name

    if dry_run:
        print({
            "name": name,
            "version": version,
            "target": target,
            "formats": formats,
            "man_paths": [str(p) for p in (man_paths or [])],
            "deb_depends": deb_depends,
            "rpm_depends": rpm_depends,
            "build_dir": str(build_dir),
        })
        return

    build_dir.mkdir(parents=True, exist_ok=True)
    context = ExecutionContext(cwd=build_dir.as_posix())
    with scoped(allowlist=CATALOGUE.allowlist):
        result = tofu("plan").run_sync(context=context)
    if not result.ok:
        raise SystemExit(result.exit_code)


if __name__ == "__main__":
    app()
```

Guidance:

- Parameter names should be descriptive and stable. Where a legacy flag name
  must remain available, add an alias:

  ```python
  package_name: Annotated[str | None, Parameter(aliases=["--name"])] = None
  ```

- Where a specific delimiter is required for an environment list (for example,
  comma‑separated `formats`), specify it per parameter:

  ```python
  formats: Annotated[list[str] | None, Parameter(env_var_split=",")] = None
  ```

- Per‑parameter environment names can be pinned for backwards compatibility:

  ```python
  config_out: Annotated[Path | None, Parameter(env_var="INPUT_CONFIG_PATH")] = None
  ```

## Cuprum: command calling and pipelines

Cuprum is **not** a drop‑in replacement for Plumbum. Build commands from
allowlisted `Program` values, run via `run_sync()`/`run()`, and inspect
`CommandResult`/`PipelineResult` explicitly.

### Security model: allowlists and scoped execution

- Rationale: allowlists prevent arbitrary command execution and reduce the
  command‑injection attack surface.
- Catalogue allowlist: use `CATALOGUE.allowlist` when programs are
  pre-declared. This keeps command intent explicit and supports project-wide
  auditing.
- Manual allowlist: use `frozenset([PROGRAM, ...])` for ad‑hoc scoping or when
  mixing catalogued and uncatalogued programs.
- Enforcement: executing a non-allowlisted program raises an exception and
  fails fast.
- Migration note: this is a breaking behaviour change from Plumbum, where
  commands were implicitly available.

### Shared helper pattern

Prefer a small helper module so script examples focus on command usage instead
of repeating catalogue boilerplate.

```python
# scripts/_cuprum_helpers.py
from __future__ import annotations

from collections.abc import Iterable

from cuprum import Program, ProgramCatalogue, ProjectSettings, SafeCmd, sh


def build_catalogue(*programs: Program) -> ProgramCatalogue:
    return ProgramCatalogue(
        projects=(
            ProjectSettings(
                name="repo-scripts",
                programs=tuple(programs),
                documentation_locations=("docs/scripting-standards.md",),
                noise_rules=(),
            ),
        )
    )


def build_commands(
    *,
    catalogue: ProgramCatalogue,
    programs: Iterable[Program],
) -> dict[Program, SafeCmd]:
    return {program: sh.make(program, catalogue=catalogue) for program in programs}
```

### Program declarations and command builders

```python
from cuprum import Program
from _cuprum_helpers import build_catalogue, build_commands

GIT = Program("git")
GREP = Program("grep")

CATALOGUE = build_catalogue(GIT, GREP)
COMMANDS = build_commands(catalogue=CATALOGUE, programs=(GIT, GREP))
git = COMMANDS[GIT]
grep = COMMANDS[GREP]
```

### Command execution and failure handling

```python
from cuprum import scoped

with scoped(allowlist=CATALOGUE.allowlist):
    result = git("--no-pager", "log", "-1", "--pretty=%H").run_sync()

if not result.ok:
    raise RuntimeError(f"git failed ({result.exit_code}): {result.stderr.strip()}")

last_commit = result.stdout.strip()
```

Guidance:

- In script entry points, propagate command failures with
  `return result.exit_code` or `raise SystemExit(result.exit_code)`.
- In helper/library-style functions, raise `RuntimeError` (or a domain error)
  so callers can decide how to present or recover from failures.

### Working directory and environment management

```python
from pathlib import Path
from cuprum import ExecutionContext, Program, scoped, sh

MAKE = Program("make")
make = sh.make(MAKE)
repo_dir = Path(__file__).resolve().parents[1]

context = ExecutionContext(
    cwd=repo_dir.as_posix(),
    env={"CI": "1", "GIT_AUTHOR_NAME": "CI"},
)

with scoped(allowlist=frozenset([MAKE])):
    result = make("--version").run_sync(context=context, echo=True)
if not result.ok:
    raise RuntimeError(result.stderr.strip())
```

### Pipelines

```python
from cuprum import scoped

with scoped(allowlist=CATALOGUE.allowlist):
    pipeline_result = (git("--no-pager", "log", "--oneline") | grep("fix")).run_sync()

if pipeline_result.failure is not None:
    failed = pipeline_result.stages[pipeline_result.failure_index]
    raise RuntimeError(f"pipeline stage failed: {failed.exit_code}")

shortlog = pipeline_result.stdout
```

## Pathlib: robust path manipulation

### Project roots, joins, and ensuring directories

```python
from __future__ import annotations  # Enables postponed annotation evaluation
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST = PROJECT_ROOT / "dist"
(DIST / "artifacts").mkdir(parents=True, exist_ok=True)

# Portable joins and normalization
cfg = PROJECT_ROOT.joinpath("config", "release.toml").resolve()
```

### Reading and writing files and atomic updates

```python
from pathlib import Path
import tempfile

f = Path("./dist/version.txt")

# Text I/O
f.write_text("1.2.3\n", encoding="utf-8")
version = f.read_text(encoding="utf-8").strip()

# Atomic write pattern (tmp → replace)
with tempfile.NamedTemporaryFile("w", delete=False, dir=f.parent, encoding="utf-8") as tmp:
    tmp.write("new-contents\n")
    tmp_path = Path(tmp.name)

tmp_path.replace(f)  # atomic on POSIX
```

### Globbing, filtering, and safe deletion

```python
from pathlib import Path

# Recursive glob
md_files = sorted(Path("docs").glob("**/*.md"))

# Filter by suffix / size
small_md = [p for p in md_files if p.stat().st_size < 4096 and p.suffix == ".md"]

# Safe deletion (ignore missing)
try:
    (Path("build") / "temp.bin").unlink()
except FileNotFoundError:
    pass
```

## Cyclopts + Cuprum + Pathlib together (reference script)

```python
#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = ["cyclopts>=2.9", "cuprum==0.1.0"]
# ///

from __future__ import annotations
from pathlib import Path
from typing import Annotated

import cyclopts
from cyclopts import App, Parameter
from cuprum import ExecutionContext, Program, scoped
from _cuprum_helpers import build_catalogue, build_commands

GIT = Program("git")
CATALOGUE = build_catalogue(GIT)
COMMANDS = build_commands(catalogue=CATALOGUE, programs=(GIT,))
git = COMMANDS[GIT]

app = App(config=cyclopts.config.Env("INPUT_", command=False))


@app.default
def main(
    *,
    bin_name: Annotated[str, Parameter(required=True)],
    version: Annotated[str, Parameter(required=True)],
    formats: list[str] | None = None,
    outdir: Path | None = None,
    dry_run: bool = False,
):
    project_root = Path(__file__).resolve().parents[1]
    dist = (outdir or (project_root / "dist")) / bin_name
    dist.mkdir(parents=True, exist_ok=True)

    if not dry_run:
        context = ExecutionContext(cwd=project_root.as_posix())
        with scoped(allowlist=CATALOGUE.allowlist):
            result = git("tag", f"v{version}").run_sync(context=context, echo=True)
        if not result.ok:
            raise SystemExit(result.exit_code)

    print({
        "bin_name": bin_name,
        "version": version,
        "formats": formats or [],
        "dist": str(dist),
    })


if __name__ == "__main__":
    app()
```

## Testing expectations

- Automated coverage via `pytest` is required for every script. Fixtures from
  `pytest-mock` support Python‑level mocking; `cmd-mox` simulates external
  executables without touching the host system.
- Behavioural flows that map cleanly to scenarios should adopt Behaviour‑Driven
  Development (BDD) via `pytest-bdd` so that intent is captured in
  human‑readable Given/When/Then narratives.
- Tests reside in `scripts/tests/`, mirroring script names. For example,
  `scripts/bootstrap_doks.py` pairs with `scripts/tests/test_bootstrap_doks.py`.
- Where scripts rely on environment variables, both happy paths and failure
  modes must be asserted; tests should demonstrate graceful error handling
  rather than opaque stack traces.

### Mocking Python dependencies (pytest-mock) and environment (monkeypatch)

```python
from cyclopts.testing import invoke
from scripts.package import app


def test_reads_env_and_defaults(monkeypatch, tmp_path):
    # Arrange env for Cyclopts
    monkeypatch.setenv("INPUT_BIN_NAME", "demo")
    monkeypatch.setenv("INPUT_VERSION", "1.2.3")
    monkeypatch.setenv("INPUT_FORMATS", "deb rpm")  # whitespace or newlines

    # Exercise
    result = invoke(app, [])

    # Assert
    assert result.exit_code == 0
    assert '"version": "1.2.3"' in result.stdout


def test_patch_python_dependency(mocker):
    # Example: patch a helper function used by the script
    from scripts import helpers

    mocker.patch_object(helpers, "compute_checksum", return_value="deadbeef")
    assert helpers.compute_checksum(b"abc") == "deadbeef"
```

### Mocking external executables with cmd-mox (record → replay → verify)

Enable the plugin in `conftest.py`:

```python
pytest_plugins = ("cmd_mox.pytest_plugin",)
```

```python
from cuprum import Program, scoped, sh

GIT = Program("git")
git = sh.make(GIT)


def test_git_tag_happy_path(cmd_mox, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    # Mock external command behaviour
    cmd_mox.mock("git").with_args("tag", "v1.2.3").returns(exit_code=0)

    # Run the code under test while shims are active
    cmd_mox.replay()
    with scoped(allowlist=frozenset([GIT])):
        result = git("tag", "v1.2.3").run_sync()
    cmd_mox.verify()
    assert result.ok


def test_git_tag_failure_surface_error(cmd_mox, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    cmd_mox.mock("git").with_args("tag", "v1.2.3").returns(exit_code=1, stderr="denied")

    cmd_mox.replay()
    with scoped(allowlist=frozenset([GIT])):
        result = git("tag", "v1.2.3").run_sync()
    cmd_mox.verify()
    assert not result.ok
    assert result.exit_code == 1
    assert "denied" in result.stderr
```

### Spies and passthrough capture (turn real calls into fixtures)

```python
from cuprum import Program, scoped, sh

ECHO = Program("echo")
echo = sh.make(ECHO)


def test_spy_and_record(cmd_mox, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    # Spy records actual usage; passthrough runs the real command
    spy = cmd_mox.spy("echo").passthrough()

    cmd_mox.replay()
    with scoped(allowlist=frozenset([ECHO])):
        result = echo("hello world").run_sync()
    cmd_mox.verify()
    assert result.ok

    # Inspect what happened
    spy.assert_called()
    assert spy.call_count == 1
    args = spy.invocations[0].argv[1:]
    assert args == ["hello world"]
```

## Operational guidelines

- Scripts must be idempotent. Re‑running should converge state without
  destructive side effects. Guard conditions (for example, checking the secrets
  manager to confirm existing secrets) should precede writes or rotations.
- Pure functions that accept configuration objects are preferred over global
  state, so tests can exercise logic deterministically.
- Exit codes should follow Portable Operating System Interface (POSIX)
  conventions: `0` for success, non-zero for actionable failures.
  Human-friendly error messages should highlight remediation steps.
- Dependencies must remain minimal. Any new package should be added to the `uv`
  block and the rationale documented within the script or companion tests.

## Migration guidance (Plumbum → Cuprum)

1. Imports: replace `from plumbum import local` with cuprum primitives such as
   `Program`, `ExecutionContext`, and `scoped`.
2. Command creation: replace `local["git"]` style calls with
   `Program("git")`, register the program in a catalogue, then build commands
   via `sh.make(...)`.
3. Execution: replace implicit execution (`cmd()`) with explicit
   `cmd.run_sync()` (or `await cmd.run()`), then inspect `CommandResult`.
4. Context handling: replace `local.cwd(...)` and `local.env(...)` with
   `ExecutionContext(cwd=..., env=...)`.
5. Error handling: replace exception-driven command checks with explicit
   `result.ok`, `result.exit_code`, and `result.stderr` handling.
6. Security posture: apply `scoped(allowlist=...)` consistently; treat missing
   allowlist entries as policy failures that must be fixed rather than bypassed.

## Migration guidance (Typer → Cyclopts)

1. Dependencies: replace Typer with Cyclopts in the script’s `uv` block.
2. Entry point: replace `app = typer.Typer(…)` with `app = App(…)` and
   configure `Env("INPUT_", command=False)` where environment variables are
   authoritative in CI.
3. Parameters: replace `typer.Option(…)` with annotations and
   `Parameter(…)`. Mark required options with `required=True`. Map any
   non‑matching environment names via `env_var=…`.
4. Lists: remove custom split/trim code. Use list‑typed parameters; add
   `env_var_split=","` where a non‑whitespace delimiter is required.
5. Compatibility: retain legacy flag names using `aliases=["--old-name"]`.
6. Bash glue: delete argument arrays and conditional appends in GitHub
   Actions. Export `INPUT_*` environment variables and call `uv run` on the
   script.

## CI wiring: GitHub Actions (Cyclopts‑first)

```yaml
- name: Build
  shell: bash
  working-directory: ${{ inputs.project-dir }}
  env:
    INPUT_BIN_NAME: ${{ inputs.bin-name }}
    INPUT_VERSION: ${{ inputs.version }}
    INPUT_FORMATS: ${{ inputs.formats }}               # multiline or space‑sep
    INPUT_OUTDIR: ${{ inputs.outdir }}
  run: |
    set -euo pipefail
    uv run "${GITHUB_ACTION_PATH}/scripts/package.py"
```

## Notes and gotchas

- Newline‑separated lists are preferred for CI inputs to avoid shell quoting
  issues across platforms.
- Inspect failures via `CommandResult.ok`, `CommandResult.exit_code`, and
  `CommandResult.stderr` instead of relying on exception‑based flow control.
- Production code should present friendly error messages; tests may assert raw
  behaviours (non‑zero exits, stderr contents) via `cmd-mox`.
- On Windows, newline‑separated lists are recommended for `list[Path]` to
  sidestep `;`/`:` semantics.

## Baseline enforcement in this repository

The repository enforces script baseline contracts through two Make targets:

- `make script-baseline` validates script runtime metadata, command invocation
  posture, and script-test pairing for roadmap-delivered scripts.
- `make script-test` runs the Python script test suite under `scripts/tests/`
  including behavioural scenarios.

This document should be referenced when introducing or updating automation
scripts to maintain a consistent developer experience across the repository.
