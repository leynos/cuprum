# cuprum

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](
https://deepwiki.com/leynos/cuprum)

Typed command execution for Python, with a curated program catalogue and
predictable output, failures, and cancellation.

## What is this?

Cuprum builds argument vectors for approved executables and returns structured
results. It does not pass command strings to a shell.

The default catalogue covers common tools; an application can define its own
catalogue and project metadata. A catalogue controls builder creation, and an
optional execution scope narrows which commands may run.

Cuprum offers async execution and synchronous wrappers for scripts. Python 3.12
or newer is required.

## Quick taste

<!-- tested-example: readme-quick-start -->

```python
import asyncio
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="quick-start")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python("-c", "print('hello, cuprum!')")


async def main() -> None:
    result = await command.run()
    assert result.ok and result.stdout == "hello, cuprum!\n"


asyncio.run(main())
```

The [users' guide](docs/users-guide.md) continues with synchronous execution,
output observation, pipelines, concurrency, policy, and troubleshooting.

## Features

- **Catalogue-backed builders** – Unknown programs raise
  `UnknownProgramError` before execution.
- **Structured results** – Exit code, process ID, captured output, timing,
  resource measurements where supported, and an `ok` property.
- **Output choices** – Capture, echo, line observation, and an optional idle
  heartbeat can be configured independently.
- **Composition** – Pipelines and bounded concurrent execution.
- **Context policy** – Scoped allowlists, environment overlays, and hooks.
- **Optional acceleration** – A Rust extension is available; the pure Python
  installation has no runtime dependencies.

## Installation

Run `python -m pip install cuprum` to install it with pip.

Or with [uv](https://docs.astral.sh/uv/):

Run `uv add cuprum` to add it to a uv project.

## Status

Cuprum is in early development. The command runtime, scoped allowlists, hooks,
and optional native stream backend are implemented. Check the
[changelog](CHANGELOG.md) and
[0.2.0 migration guide](docs/v0-2-0-migration-guide.md) when upgrading; the
public API may evolve before a stable release.

## Documentation

For the full guide—including how to build your own program catalogues, write
project-specific builders, and control execution contexts—see the
[users' guide](docs/users-guide.md).

## Why "cuprum"?

The name is a tip of the hat to [Plumbum](https://plumbum.readthedocs.io/), the
library that showed us shell-like scripting in Python could actually be
pleasant. "Cuprum" is Latin for copper—another metal used in pipes—and we hope
to carry that spirit forward with a focus on type safety and explicit
allowlists.

## Licence

[ISC](LICENSE)
