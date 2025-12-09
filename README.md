# cuprum

Typed, async command execution for Python—so you can ditch the shell scripts
without losing your mind.

## What is this?

If you've ever written a Python script that calls out to external commands,
you've probably experienced the joys of `subprocess`: stringly-typed arguments,
mysterious failures, and output that vanishes into the void. Cuprum is here to
help.

We give you a **typed, safe approach to running external programs**. Instead of
passing arbitrary strings to a shell, you work with a curated catalogue of
approved executables. Each command carries metadata about its project, so
downstream tooling knows how to filter noise from logs or where to find
documentation.

Cuprum is async-first but provides synchronous wrappers for scripts that don't
need the full async machinery. Whether you're building deployment helpers, CI
glue, or maintenance scripts, we want "Python instead of Bash" to feel like an
upgrade rather than a chore.

## Quick taste

```python
from cuprum import ECHO, ExecutionContext, sh

# Create a builder for a curated program
echo = sh.make(ECHO)

# Build a command with typed arguments
cmd = echo("-n", "hello, cuprum!")

# Run it (async)
result = await cmd.run(echo=True)
if result.ok:
    print(f"Output: {result.stdout}")

# Or run it synchronously
result = cmd.run_sync()
```

## Features

- **Catalogue-based safety** – Only approved programs can run; unknown
  executables raise `UnknownProgramError`.
- **Typed command building** – `sh.make()` returns builders that validate
  arguments and carry project metadata.
- **Async-first execution** – `await cmd.run()` with capture/echo toggles,
  environment overlays, and working directory control.
- **Synchronous convenience** – `cmd.run_sync()` when you don't need async.
- **Graceful cancellation** – Cancelled tasks send `SIGTERM`, wait briefly,
  then escalate to `SIGKILL`.
- **Structured results** – `CommandResult` gives you exit code, pid, stdout,
  stderr, and an `ok` helper.
- **Zero dependencies** – Just Python 3.12+ and the standard library.

## Installation

```shell
pip install cuprum
```

Or with [uv](https://docs.astral.sh/uv/):

```shell
uv add cuprum
```

## Status

Cuprum is in early development. The typed command core and execution runtime
are complete (Phase 1 of the [roadmap](docs/roadmap.md)), but context-scoped
allowlists and hooks are still on the way. The API may shift as we learn what
works best.

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
