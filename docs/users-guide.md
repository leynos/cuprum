# cuprum Users' Guide

## Program catalogue

Cuprum exposes a typed `Program` NewType to identify executables. The library
ships with a curated catalogue (`DEFAULT_CATALOGUE`) that defines an allowlist
of programs and project metadata. Requests for unknown executables raise
`UnknownProgramError` so accidental shell access is blocked by default.

- Import curated programs from `cuprum` (for example `ECHO`, `LS`).
- Each project in the catalogue includes `noise_rules` (output lines that
  downstream loggers can safely drop) and `documentation_locations` (links for
  operators and reviewers).

```python
from cuprum import DEFAULT_CATALOGUE, ECHO

entry = DEFAULT_CATALOGUE.lookup(ECHO)
print(entry.project.noise_rules)
```

### Adding project-specific programs

Use `ProjectSettings` and `ProgramCatalogue` to extend or replace the default
catalogue:

```python
from cuprum import Program, ProgramCatalogue, ProjectSettings

project = ProjectSettings(
    name="data-pipeline",
    programs=(Program("python"),),
    documentation_locations=("docs/pipelines.md",),
    noise_rules=(r"^progress:",),
)
catalogue = ProgramCatalogue(projects=(project,))
```

Downstream services can fetch metadata via `catalogue.visible_settings()` to
propagate noise filters and documentation links alongside the allowlist.

## Typed command core

Cuprum provides `sh.make` to build typed `SafeCmd` instances from curated
programs. Builders enforce the catalogue allowlist up front and carry project
metadata alongside argv, so downstream services can apply noise rules or link
to documentation without a second lookup.

- `sh.make` raises `UnknownProgramError` when the program is not in the current
  catalogue.
- Positional arguments are stringified with `str()`.
- Keyword arguments become `--flag=value` entries with underscores in flag
  names converted to hyphens.
- `None` is rejected; decide whether to skip or substitute a flag before
  calling the builder.

```python
from cuprum import ECHO, sh

echo = sh.make(ECHO)
cmd = echo("-n", "hello world")
print(cmd.argv_with_program)  # ('echo', '-n', 'hello world')
print(cmd.project.noise_rules)  # metadata for downstream loggers
```

### Writing project-specific builders

Wrap `sh.make` in project modules to centralize validation and expose a clear
API for callers:

```python
from pathlib import Path

from cuprum import Program, SafeCmd, sh

SAFE_CAT = Program("cat")


def _safe_path(path: Path) -> str:
    path = path.resolve()
    if not path.is_file():
        msg = f"{path} is not a readable file"
        raise ValueError(msg)
    return path.as_posix()


def cat_file(path: Path, numbered: bool = False) -> SafeCmd:
    args: list[str] = []
    if numbered:
        args.append("-n")
    args.append(_safe_path(path))
    return sh.make(SAFE_CAT)(*args)
```

Builders keep argv construction in one place, making it easier to validate
inputs, document behaviour, and reuse the same allowlisted program across a
codebase.

## Execution runtime

`SafeCmd.run` executes curated commands asynchronously with predictable capture
and echo semantics and returns a structured `CommandResult`:

- `stdout` and `stderr` are captured by default. Set `capture=False` to stream
  only; the result will carry `None` for output fields.
- `echo=True` tees stdout/stderr to the parent process while still capturing
  them when `capture=True`.
- Pass an `ExecutionContext` via the `context` parameter to override execution
  details:
  - `env` overlays key/value pairs on top of the current environment without
    mutating `os.environ`; use it to pass per-command settings.
  - `cwd` sets the working directory for the subprocess when provided.
  - `cancel_grace` controls how long Cuprum waits after `SIGTERM` (termination
    signal) before escalating to `SIGKILL` (kill signal).
  - `stdout_sink` and `stderr_sink` route echoed output to alternative text
    streams when `echo=True`.
  - `encoding` and `errors` configure how captured output is decoded; defaults
    are `"utf-8"` with `"replace"`.
- `exit_code`, `pid`, and `ok` on the `CommandResult` make it easy to branch on
  success.

```python
from cuprum import ECHO, ExecutionContext, sh


async def greet() -> None:
    cmd = sh.make(ECHO)("-n", "hello runtime")
    ctx = ExecutionContext(env={"GREETING": "1"})
    result = await cmd.run(echo=True, context=ctx)
    if not result.ok:
        raise RuntimeError(f"echo failed: {result.exit_code}")
    print(result.stdout)
```

If the awaiting task is cancelled while a command is running, Cuprum sends
`SIGTERM` to the subprocess, waits for a short grace period, and then escalates
to `SIGKILL` to ensure the child process is cleaned up.

### Synchronous execution

For scripts or contexts where async/await is not available, use `run_sync()`:

```python
from cuprum import ECHO, ExecutionContext, sh


def greet() -> None:
    cmd = sh.make(ECHO)("-n", "hello sync")
    ctx = ExecutionContext(env={"GREETING": "1"})
    result = cmd.run_sync(echo=True, context=ctx)
    if not result.ok:
        raise RuntimeError(f"echo failed: {result.exit_code}")
    print(result.stdout)
```

`run_sync()` accepts the same parameters as `run()` and returns an identical
`CommandResult`. It drives the event loop internally via `asyncio.run()`.
