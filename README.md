# 🔧 cuprum

[![Ask DeepWiki][dw]][deepwiki] [![PyPI Version][pypi]][package]

[dw]: https://deepwiki.com/badge.svg
[deepwiki]: https://deepwiki.com/leynos/cuprum
[pypi]: https://img.shields.io/pypi/v/cuprum "PyPI package"
[package]: https://pypi.org/project/cuprum/

*Typed, async command execution for Python—so you can ditch the shell scripts
without losing your mind.*

Cuprum runs a known set of external programs from Python without ever building
a shell command string. You say which programs are allowed, build argument
lists with ordinary Python values, and get a structured result back from every
run.

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

Cuprum needs Python 3.12 or newer, and runs on Linux, macOS, and Windows.

This README and the guides describe Cuprum 0.2.0, which is currently in beta.
Installers skip prereleases unless asked, so a bare `pip install cuprum` would
fetch the older 0.1.0 release instead. Ask for the beta explicitly with pip:

<!-- shell-example: readme-install-pip -->

```shell
python -m pip install "cuprum>=0.2.0b2"
```

Or add it to a [uv](https://docs.astral.sh/uv/) project:

<!-- shell-example: readme-install-uv -->

```shell
uv add "cuprum>=0.2.0b2"
```

The same requirement keeps working once 0.2.0 itself is released.

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

Four small pieces did the work there:

1. `ProgramCatalogue.from_programs()` approved one program.
2. `sh.make()` checked that approval and returned a builder.
3. Calling the builder produced a command: an argument vector, not a string.
4. `run()` started the child and returned a `CommandResult`.

Prefer to skip the event loop? `command.run_sync()` returns the same result.

### Where next?

- **New to Cuprum?** The [users' guide][guide] picks up where this example
  stops, one task at a time: handling failure, controlling output, input and
  deadlines, pipelines, and policy.
- **Replacing `subprocess` calls?** Start at
  [Migrate a caller][guide-migrate], which sets a `subprocess.run()` call
  beside its Cuprum equivalent.
- **Upgrading from Cuprum 0.1?** The [0.2.0 migration guide][migration] lists
  the breaking changes first, each with the fix.

______________________________________________________________________

## Features

- **Catalogue-backed builders** – Unknown programs raise
  `UnknownProgramError` before anything runs ([run a command][guide-run]).
- **Structured results** – Exit code, process ID, captured output, timing,
  resource measurements where the platform supports them, and a handy `ok`
  property ([handle failure][guide-failure]).
- **Output your way** – Capture, echo, per-line observation, byte-exact
  capture, and an optional heartbeat for quiet children can each be switched on
  independently, or a stream can go straight to a file
  ([control output][guide-output]).
- **Input of any size** – Send a small payload in one go, or stream a large one
  from an async producer, with backpressure
  ([stream input][guide-stream-input]).
- **Graceful cancellation** – Cancelled or timed-out runs terminate the child,
  wait for a configurable grace period, then force-kill it (`SIGKILL` on
  POSIX). Opt in to process-group ownership to reclaim the child's descendants
  too ([handle a slow command][guide-slow]).
- **Hands on the wheel** – `start()` returns a handle to a running child, so
  your code decides when to wait, signal, or stop it
  ([hold a running child][guide-handle]).
- **Composition** – Build pipelines, or run commands concurrently with a
  bounded level of parallelism ([connect a pipeline][guide-pipeline]).
- **Context policy** – Scoped allowlists, environment policies, executable
  bindings, and hooks keep each part of your application to the commands it
  should use ([apply a policy][guide-policy]).
- **Observability** – Lifecycle events, with adapters for structured logging,
  metrics, and tracing, plus framed output on GitHub Actions and opt-in
  redaction of secrets from what they export
  ([log, measure, and trace runs][guide-telemetry]).
- **Optional acceleration** – A Rust extension speeds up stream handling between
  pipeline stages; the pure Python installation has no runtime dependencies at
  all ([choosing a stream backend][guide-backend]).

______________________________________________________________________

## Status

Cuprum 0.2.0 is in beta. The command runtime, scoped allowlists, hooks, and the
optional native stream backend are all in place, and the examples in this
README and the guides are executed by the test suite. The public API may still
change before a stable 1.0 release, so pin the version you depend on and read
the [changelog][changelog] and [migration guide][migration] when you upgrade.

______________________________________________________________________

## Learn more

- [Users' guide][guide] — from a first command to pipelines, policy,
  telemetry, and troubleshooting
- [0.2.0 migration guide][migration] — upgrading an existing application
- [Changelog][changelog] — what changed in each release
- [Roadmap][roadmap] — planned features and progress
- [Developers' guide][developers] — building, testing, and maintaining cuprum

______________________________________________________________________

## About the name

The name is a tip of the hat to [Plumbum](https://plumbum.readthedocs.io/), the
library that showed us shell-like scripting in Python could actually be
pleasant. "Cuprum" is Latin for copper—another metal used in pipes—and we hope
to carry that spirit forward with a focus on type safety and explicit
allowlists.

______________________________________________________________________

## Licence

ISC — see [LICENSE][licence] for details.

______________________________________________________________________

## Contributing

Contributions are welcome! Start with the [developers' guide][developers] for
how to build and test the project; automated contributors should also follow
[AGENTS.md][agents].

<!-- This README is also the PyPI project description, where relative links do
not resolve, so every link to a repository file is absolute. -->

[guide]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md
[guide-run]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md#run-a-command
[guide-failure]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md#handle-failure
[guide-output]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md#control-output
[guide-slow]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md#handle-a-slow-command
[guide-handle]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md#hold-a-running-child-and-decide-when-to-stop-it
[guide-pipeline]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md#connect-a-pipeline
[guide-policy]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md#apply-a-policy
[guide-telemetry]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md#log-measure-and-trace-runs
[guide-backend]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md#choosing-a-stream-backend
[guide-migrate]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md#migrate-a-caller
[guide-stream-input]: https://github.com/leynos/cuprum/blob/main/docs/users-guide.md#stream-input-from-a-producer
[migration]: https://github.com/leynos/cuprum/blob/main/docs/v0-2-0-migration-guide.md
[changelog]: https://github.com/leynos/cuprum/blob/main/CHANGELOG.md
[roadmap]: https://github.com/leynos/cuprum/blob/main/docs/roadmap.md
[developers]: https://github.com/leynos/cuprum/blob/main/docs/developers-guide.md
[licence]: https://github.com/leynos/cuprum/blob/main/LICENSE
[agents]: https://github.com/leynos/cuprum/blob/main/AGENTS.md
