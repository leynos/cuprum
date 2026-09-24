# 🔧 cuprum

[![Ask DeepWiki][dw]][deepwiki] [![PyPI Version][pypi]][package]

[dw]: https://deepwiki.com/badge.svg
[deepwiki]: https://deepwiki.com/leynos/cuprum
[pypi]: https://img.shields.io/pypi/v/cuprum "PyPI package"
[package]: https://pypi.org/project/cuprum/

*Typed, async command execution for Python—so you can ditch the shell scripts
without losing your mind.*

Website: <https://df12.studio/cuprum>

______________________________________________________________________

## Why cuprum?

If you've ever written a Python script that calls out to external commands,
you've probably experienced the joys of `subprocess`: stringly-typed arguments,
mysterious failures, and output that vanishes into the void. Cuprum is here to
help.

- **No shell roulette**: You build argument vectors for approved executables,
  so there are no command strings to quote, escape, or get wrong.
- **You choose what runs**: A catalogue decides which programs can be built
  into commands. Use the default one, or define your own with project metadata.
  An optional execution scope narrows things further at run time.
- **Answers, not guesswork**: Every completed run returns a structured result,
  so success, failure, and output are right there to inspect.
- **Async when you want it**: Cuprum is async-first, with synchronous wrappers
  for scripts that don't need an event loop.

Whether you're building deployment helpers, CI glue, or maintenance scripts, we
want "Python instead of Bash" to feel like an upgrade rather than a chore.

______________________________________________________________________

## Quick start

### Installation

Cuprum needs Python 3.12 or newer. Install it with pip:

<!-- shell-example: readme-install-pip -->

```shell
python -m pip install cuprum
```

Or add it to a [uv](https://docs.astral.sh/uv/) project:

<!-- shell-example: readme-install-uv -->

```shell
uv add cuprum
```

### Quick taste

This example approves the Python interpreter that is running it, so it works on
any machine without assuming which other tools are installed:

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

Prefer to skip the event loop? `command.run_sync()` returns the same result.

______________________________________________________________________

## Features

- **Catalogue-backed builders** – Unknown programs raise
  `UnknownProgramError` before anything runs.
- **Structured results** – Exit code, process ID, captured output, timing,
  resource measurements where the platform supports them, and a handy `ok`
  property.
- **Output your way** – Capture, echo, per-line observation, and an optional
  heartbeat for quiet children can each be switched on independently.
- **Graceful cancellation** – Cancelled or timed-out runs terminate the child,
  wait for a configurable grace period, then escalate to `SIGKILL`.
- **Composition** – Build pipelines, or run commands concurrently with a
  bounded level of parallelism.
- **Context policy** – Scoped allowlists, environment overlays, and hooks keep
  each part of your application to the commands it should use.
- **Optional acceleration** – A Rust extension speeds up stream handling; the
  pure Python installation has no runtime dependencies at all.

______________________________________________________________________

## Status

Cuprum is young but busy. The command runtime, scoped allowlists, hooks, and
the optional native stream backend are all in place. The public API may still
evolve before a stable release, so check the changelog and migration guide when
you upgrade.

______________________________________________________________________

## Learn more

- [Users' guide](docs/users-guide.md) — synchronous execution, output
  observation, pipelines, concurrency, policy, and troubleshooting
- [Developers' guide](docs/developers-guide.md) — building, testing, and
  maintaining cuprum
- [Roadmap](docs/roadmap.md) — planned features and progress
- [Changelog](CHANGELOG.md) — what changed in each release
- [0.2.0 migration guide](docs/v0-2-0-migration-guide.md) — upgrading an
  existing application

______________________________________________________________________

## About the name

The name is a tip of the hat to [Plumbum](https://plumbum.readthedocs.io/), the
library that showed us shell-like scripting in Python could actually be
pleasant. "Cuprum" is Latin for copper—another metal used in pipes—and we hope
to carry that spirit forward with a focus on type safety and explicit
allowlists.

______________________________________________________________________

## Licence

ISC — see [LICENSE](LICENSE) for details.

______________________________________________________________________

## Contributing

Contributions are welcome! Please read [AGENTS.md](AGENTS.md) for the house
rules and the [developers' guide](docs/developers-guide.md) for how to build
and test the project.
