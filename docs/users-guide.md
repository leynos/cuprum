# Cuprum users' guide

Cuprum helps Python applications run a known set of external programs without
building shell command strings. This guide describes Cuprum 0.2.0. It starts
with a complete command, then continues through failures, output, input,
pipelines, policy, and production use. Each section builds on the ones before
it, so reading from the top is the gentlest route; each section also stands
alone for readers arriving from a search or a link.

Every Python example in this guide is executed by the project's test suite, so
it runs as printed. Copy any of them into a file and run it with `python` to
experiment. The examples that launch a child use the current Python interpreter
(`sys.executable`), which avoids assuming that an optional executable such as
git is installed. The [glossary](#glossary) at the end defines recurring terms.

## Choose a path

The guide has three parts. Table 1 suggests where to start.

| Part                                                                       | Read it when                                                                          |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| [Install Cuprum](#install-cuprum) to [Migrate a caller](#migrate-a-caller) | Learning Cuprum: one task per section, in the order most people need them.            |
| [Going further](#going-further)                                            | An application relies on Cuprum: complete recipes for common tasks.                   |
| [Operational reference](#operational-reference)                            | Diagnosing or integrating: exact events, metrics, log records, and backend behaviour. |

_Table 1: The three parts of this guide._

Or jump straight to a task:

- **Run a first command:** [Run a command](#run-a-command).
- **Replace `subprocess` calls:** [Migrate a caller](#migrate-a-caller), then
  the [0.2.0 migration guide](v0-2-0-migration-guide.md) when upgrading from
  Cuprum 0.1.
- **Make a script dependable:** [handle failure](#handle-failure),
  [control output](#control-output), and
  [supply input and a deadline](#supply-input-environment-and-a-deadline).
- **Write output to a file, or feed in large input:**
  [send a stream to a file](#send-a-stream-to-a-file-or-the-parent) or
  [stream input from a producer](#stream-input-from-a-producer).
- **Run several commands:** [connect a pipeline](#connect-a-pipeline) or
  [run commands concurrently](#run-commands-concurrently).
- **Limit what may run:** [apply a policy](#apply-a-policy).
- **Supervise a long-running child:**
  [hold a running child](#hold-a-running-child-and-decide-when-to-stop-it).
- **Watch runs in production:**
  [observe production runs](#observe-production-runs).
- **Something went wrong:** [troubleshoot a run](#troubleshoot-a-run).

## Install Cuprum

Cuprum requires Python 3.12 or newer. Cuprum 0.2.0 is currently published as a
beta, and installers skip prereleases unless asked, so a bare
`pip install cuprum` installs the older 0.1.0 release, which lacks most of what
this guide describes. Name the beta explicitly when installing with pip:

<!-- shell-example: install-pip -->

```shell
python -m pip install "cuprum>=0.2.0b2"
```

Or add it to a uv project:

<!-- shell-example: install-uv -->

```shell
uv add "cuprum>=0.2.0b2"
```

The same requirement keeps working once 0.2.0 itself is released. To confirm
which version is installed, check `importlib.metadata.version("cuprum")`.

The pure Python installation has no runtime dependencies. On glibc-based Linux
(x86_64 and aarch64, glibc 2.28 or newer), macOS (x86_64 and arm64), and
Windows (x86_64), pip selects a native wheel instead, which adds optional Rust
acceleration for pipelines; behaviour is the same either way. See
[Choosing a stream backend](#choosing-a-stream-backend) for details.

Each release also publishes a source distribution (`cuprum-<version>.tar.gz`)
on PyPI and on the GitHub Release, alongside the wheels. pip and uv install it
only when no wheel matches the current platform, or when asked for it
explicitly, for example `pip install --no-binary cuprum cuprum`. In practice the
`py3-none-any` wheel already covers every platform, so the sdist mainly
matters to packagers and to people building from source. Building or installing
from it always yields the pure Python package: its build backend (`uv_build`)
never compiles the Rust extension, even when a Rust toolchain is present, so
`is_rust_available()` then returns `False`. Install a platform wheel for native
acceleration; see [Choosing a stream backend](#choosing-a-stream-backend).

Cuprum ships a `py.typed` marker, so type checkers read its annotations under
[PEP 561](https://peps.python.org/pep-0561/) and infer the real types of the
symbols it exports. Every published distribution carries the marker — the pure
Python wheel, the native wheels, and the source distribution — so a source
build keeps the same typing support as a binary install. No configuration is
needed: a checker that resolves the installed package picks the marker up on
its own.

## Run a command

Running a command takes four steps, and each one has a job:

1. **Declare the program.** `Program("git")` names an executable. It is a typed
   string, so a program name cannot be confused with an ordinary argument.
2. **Approve it in a catalogue.** A `ProgramCatalogue` is the list of programs
   the application may build commands for. Cuprum refuses to build anything
   that is not listed, which is how a typo or an unexpected tool is caught
   before it runs.
3. **Make a builder.** `sh.make()` checks the program against the catalogue
   and returns a builder. Calling the builder with arguments produces a
   `SafeCmd`: the program plus an argument vector, never a shell string.
4. **Run it.** `run_sync()` (or `await run()`) starts the child, waits for it,
   and returns a `CommandResult`.

The default catalogue already contains common tools such as `ECHO`, `GIT`, `LS`,
`RSYNC`, and `TAR`, so `sh.make(GIT)` works without one. For any other
executable, build a catalogue and pass it, either as an argument, as here, or
by activating it in a [scope](#apply-a-policy):

<!-- tested-example: first-command -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="my-script")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python("-c", "print('hello, cuprum!')")
result = command.run_sync()
assert result.ok
assert result.stdout == "hello, cuprum!\n"
assert result.stderr == ""
```

`run_sync()` suits a synchronous script. In an async application, use
`await command.run()` inside an async function. Both return `CommandResult`.
Cuprum passes an argument vector directly to the child, without a shell: shell
quoting and pipes in a string are not interpreted.

## Handle failure

`UnknownProgramError` means `sh.make()` could not find a program in its
catalogue — the explicit argument, the innermost `scoped(catalogue=...)`, or
`DEFAULT_CATALOGUE`. That is a policy or configuration error, raised while
building rather than running. A registered executable that is absent from the
operating system raises `FileNotFoundError` at launch. A child that exits
non-zero returns a result with `ok == False` and an `exit_code`; it does not
automatically raise.

<!-- tested-example: failures -->

```python
import sys

from cuprum import Program, ProgramCatalogue, UnknownProgramError, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="failures")
try:
    sh.make(Program("not-in-this-catalogue"), catalogue=catalogue)
except UnknownProgramError:
    pass
else:
    raise AssertionError("an unknown program should be rejected")

python = sh.make(Program(sys.executable), catalogue=catalogue)
result = python("-c", "import sys; sys.exit(7)").run_sync()
assert not result.ok
assert result.exit_code == 7
```

Treat a non-zero result according to the called tool's exit-code contract.
Inspect `stderr` for diagnostics, but do not parse it to decide whether
Cuprum's catalogue accepted the program. For a missing executable, check the
path or the child's inherited `PATH`.

## Build arguments deliberately

Each argument passed to a builder becomes exactly one entry in the child's
argument vector. An argument containing spaces remains one argument, and quotes,
`*`, `|`, and `$` are passed through literally because no shell is involved.

Positional and keyword arguments both accept `str`, `int`, `float`, `bool`,
`pathlib.Path`, and other path-like objects. Keyword arguments become
`--name=value`, with underscores in the name turned into hyphens, which suits
tools that accept that form:

<!-- tested-example: arguments -->

```python
import sys
from pathlib import Path

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="arguments")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python("-c", "print('two words')", Path("data.txt"), log_level=3)
assert command.argv == ("-c", "print('two words')", "data.txt", "--log-level=3")
assert command.argv_with_program == (sys.executable, *command.argv)
```

For a presence flag such as `--check`, pass it positionally: `check=True` would
produce `--check=True`. Booleans are values, not presence switches:
`porcelain=True` produces `--porcelain=True`, and `porcelain=False` produces
`--porcelain=False`. `None` raises `TypeError` in either position, so decide
whether to omit or substitute an optional flag before building.

### Execution options are not child flags

Names that configure _how_ a command runs are reserved: every field of
`ExecutionContext` (currently `env`, `env_mode`, `cwd`, `cancel_grace`,
`native_pump_cleanup_grace`, `timeout`, `stdout_sink`, `stderr_sink`,
`encoding`, `errors`, `tags`, and `process_group`) and the parameters of
`run_sync` (`output`, `timeout`, `context`, `stdin`). Passing one to a builder
as a keyword raises `TypeError` instead of rendering it as a child flag, and
the message names the right place for it:

<!-- tested-example: reserved-execution-options -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="reserved")
python = sh.make(Program(sys.executable), catalogue=catalogue)
try:
    python("-c", "print('unused')", cwd="/tmp")
except TypeError as exc:
    assert str(exc) == (
        "cwd is an execution option; pass ExecutionContext(cwd=...) to run_sync"
    )
else:
    raise AssertionError("cwd should not become a child flag")
try:
    python("-c", "print('unused')", timeout=5)
except TypeError as exc:
    assert str(exc) == "timeout is an execution option; pass timeout=... to run_sync"
else:
    raise AssertionError("timeout should not become a child flag")
```

Otherwise `cwd=repo_dir` would quietly become `--cwd=<path>` in the child's
argv, and a run that rejected the flag would fail loudly while one that ignored
it would run in the wrong directory. Pass the value where it belongs —
`run_sync(context=ExecutionContext(cwd=...), timeout=..., stdin=...)`, as
[Supply input, environment, and a deadline](#supply-input-environment-and-a-deadline)
shows. A tool whose command line genuinely takes such a flag still receives it
positionally, as `"--cwd=<dir>"`. Names merely resembling the reserved ones,
such as `working_dir` or `stdin_file`, are unaffected.

### Exactly which values are accepted

The accepted domain is published as the `ArgValue` alias,
`str | int | float | bool | Path | os.PathLike[str]`, importable from both
`cuprum` and `cuprum.sh`. The builder `sh.make()` returns is a
`SafeCmdBuilder`, whose signature states that domain, so a static type checker
rejects a bad argument at the call site as well as Cuprum rejecting it at run
time.

Any other type raises `TypeError` naming the real type, rather than being
rendered with `str()`: `bytes`, `list`, and a bare `object()` all fail. A
path-like value is converted with `os.fspath()`, so a `pathlib.Path` and any
other object implementing `__fspath__` both serialize to the same text; this
matches the convention the standard library follows for command arguments.
`ArgValue` names `os.PathLike[str]` rather than the unparameterized
`os.PathLike` because only a `__fspath__` returning text is accepted: one
returning `bytes` is rejected with the offending value quoted. Rejection tests
the value's real type, so a proxy that reports a supported `__class__` while
remaining a different type is still refused.

`bytes` arguments are rejected for the same reason, so decode caller-held
filesystem bytes before building the command. `os.fsdecode()` does that using
the configured filesystem encoding and error handler:

<!-- tested-example: filesystem-bytes-arguments -->

```python
import os
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="fs-bytes")
python = sh.make(Program(sys.executable), catalogue=catalogue)
filesystem_argument = os.fsdecode(b"v\xc3\xbf")
command = python("-c", "pass", filesystem_argument)
assert command.argv == ("-c", "pass", filesystem_argument)
try:
    python("-c", "pass", b"v\xc3\xbf")
except TypeError:
    pass
else:
    raise AssertionError("bytes arguments should be rejected")
```

On POSIX, the default `surrogateescape` handler maps undecodable bytes to
surrogate code points, which round-trip unchanged when Python encodes the
argument for the child process. On Windows, the default `surrogatepass` handler
rejects undecodable bytes such as `b"v\xff"` with `UnicodeDecodeError`, so only
byte sequences the filesystem encoding accepts can pass through.

### Validated builders and project metadata

For domain-specific constraints, use the typed builders in `cuprum.builders`
for Git, rsync, and tar. They validate relevant paths, refs, and options at
construction time; see
[Wrap commands in project builders](#wrap-commands-in-project-builders).

Use `ProgramCatalogue.from_project(ProjectSettings(...))` when one project's
name, documentation locations, and noise rules should travel with its commands.
Cuprum stores noise rules for downstream log processing but does not apply them
itself. A catalogue that lists the same program twice raises
`DuplicateProgramError`; a multi-project `ProgramCatalogue(projects=...)` with
two projects of the same name raises `DuplicateProjectError`. Both are
`ValueError` subclasses importable from `cuprum.catalogue`.
[Define an application catalogue](#define-an-application-catalogue) shows a
complete example.

## Control output

### Capture and echo

By default Cuprum captures a child's output and keeps it quiet: `stdout` and
`stderr` on the result hold the decoded text, and nothing appears on the
terminal. _Echo_ mirrors output to the parent's streams as it arrives, which
suits progress a person is watching. The two are independent, so a run can
capture, echo, do both, or do neither. When capture is off, `stdout` and
`stderr` are `None`.

A captured single-command text run returns `CapturedResult`, whose `stdout` and
`stderr` are always strings; an empty stream is `""`. Calling `run()` or
`run_sync()` without `output` or with `output=None` is typed as returning
`CapturedResult`. Passing explicit `RunOutputOptions` is typed as returning
`CommandResult`, because those options may disable capture, even when a
particular instance sets `capture=True`. That captured call still returns
`CapturedResult` at runtime. A variable typed `RunOutputOptions | None` also
has a `CommandResult` return type for the same reason.

Use `result.require_captured()` to narrow an explicit-options result. It
returns the same object if already narrowed, copies a result whose two streams
are strings, or raises `ValueError` with the missing stream names when capture
was off. The error points to `RunOutputOptions(capture=True)`.

Use checked narrowing instead of coalescing optional streams with `or ""`. That
fallback turns a forgotten capture request into an apparently empty diagnostic.
`require_captured()` keeps an empty captured stream distinct from a stream that
was never captured.

`CapturedResult` is a distinct dataclass, so it does not compare equal to a
`CommandResult` with the same fields. Exact checks such as
`type(result) is CommandResult` also fail for captured runs. Use
`isinstance(result, CommandResult)` for base-result checks and compare fields
individually when comparing results across these types.

The narrowed type is promised only when both streams are decoded text. A run
that captures one stream and leaves the other uncaptured returns a
`CommandResult` with `None` for the stream it did not capture. A run whose
`FileBackedCapture` spilled a stream to a file also returns a `CommandResult`,
and that stream's `stdout` or `stderr` is `None`: the text is in the file the
stream's record names under `captures`, not in the payload.
`require_captured()` raises `ValueError` naming such a stream rather than
reporting an empty string, because an absent payload and an empty stream are
different facts. See
[ADR-026](adr-026-narrowed-results-only-for-complete-text-capture.md).

<!-- tested-example: capture-and-echo -->

```python
import io
import sys

from cuprum import ExecutionContext, Program, ProgramCatalogue, RunOutputOptions, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="capture-and-echo")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python("-c", "import sys; print('out'); print('err', file=sys.stderr)")

captured = command.run_sync()
assert captured.stdout == "out\n" and captured.stderr == "err\n"

# Echo to a sink of our choosing, without keeping a copy.
terminal = io.StringIO()
echoed = command.run_sync(
    output=RunOutputOptions(capture=False, echo=True),
    context=ExecutionContext(stdout_sink=terminal, stderr_sink=terminal),
)
assert echoed.stdout is None and echoed.stderr is None
assert sorted(terminal.getvalue().splitlines()) == ["err", "out"]
```

`echo=True` mirrors both streams to `sys.stdout` and `sys.stderr` unless the
`ExecutionContext` names other sinks. `echo_stdout` and `echo_stderr` choose
each stream separately; either overrides `echo` for its own stream.

Echo normally limits each mirrored line to 64 KiB, including its truncation
marker and terminator; captured output remains complete. Set
`max_echo_line_bytes=None` only when unbounded mirroring is appropriate.

### Line-level output

To react to each line as it arrives, pass a callback as `on_line`. It receives a
`LineEvent` carrying the stream name, the arrival time, and the line's text
without its terminator. It observes decoded lines even when capture and echo
are both off:

<!-- tested-example: output -->

```python
import sys

from cuprum import Program, ProgramCatalogue, RunOutputOptions, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="output")
python = sh.make(Program(sys.executable), catalogue=catalogue)
lines = []
result = python("-c", "print('one'); print('two')").run_sync(
    output=RunOutputOptions(capture=False, on_line=lines.append),
)
assert result.ok and result.stdout is None
assert [(line.stream, line.text) for line in lines] == [
    ("stdout", "one"),
    ("stdout", "two"),
]
```

The callback runs synchronously and should return promptly. Line order is
guaranteed within each stream, not across streams.

A child that never terminates a line would otherwise grow the retained text
without limit, and rescan it for every read. Observation therefore keeps at most
`max_line_chars` decoded characters (64 Ki by default) for one line, and
publishes anything longer through the same callback as fragments of exactly
that many characters. The count covers line content only; terminators are not
counted, so a line of exactly the bound with a terminator is still one event. A
fragment is not a line in the grammatical sense: it can split a grapheme
cluster, and the same fragmenting applies to `SafeCmd.lines()` and pipeline
`on_line` callbacks. The bound is independent of `capture` and `echo` — a run
with capture off retains no more than a capturing one — and it is distinct from
`max_echo_line_bytes`, which bounds the mirrored copy in bytes only. Set
`max_line_chars=None` only when retaining one unbounded line is appropriate.
Both bounds accept a positive integer or `None`. A non-integer, including
`bool`, raises `TypeError`; zero or a negative integer raises `ValueError`.

For async consumers needing backpressure, `SafeCmd.lines()` returns a
`LineStream`: an async iterator of `LineEvent(stream, at, text)` whose `result`
is available after iteration finishes.
[Stream lines as they arrive](#stream-lines-as-they-arrive) shows it in use.
Breaking out of the loop does not stop the child: close the stream with
`async with cmd.lines() as stream:` or `await stream.aclose()` to guarantee
teardown. The stream is prepared eagerly and run lazily, so leaving the
creating task does not by itself authorize the run: the allowlist, the timeout,
the executable binding, and the environment policy in force when the first
event is pulled govern it. Pipelines have no `lines()` method; `on_line` on a
pipeline run observes final-stage stdout and every stage's stderr. Interior
stdout feeds the next stage instead.

Line observation decodes for display, so an undecodable byte in text mode
raises under `ExecutionContext(errors="strict")`, exactly as it always has; the
[byte-exact mode](#binary-output)
is the one place observation replaces such bytes instead, so that the bytes
returned to the caller are not decided by a decode the caller never asked for.

### When an echo destination fails

A sink can fail while echoing: a text-only console may be unable to encode the
child's output, or the reader at the other end of a pipe may go away.
`CommandResult.relay_fallbacks` records each handled sink failure by stream,
without including output content. A text-only sink that cannot encode the
child's bytes is always handled: echo stops for that stream while capture
completes. A sink whose destination has closed is handled only when the run
opts in.

Pass `RunOutputOptions(broken_pipe_policy=BrokenPipePolicy.BEST_EFFORT)` to
stop echoing the affected stream when it raises `BrokenPipeError`, so capture,
line observation, and child reaping continue and `run_sync` still returns a
result whose `relay_fallbacks` names the `broken_pipe` category. The default,
`BrokenPipePolicy.STRICT`, propagates the error and aborts the run. Only
`BrokenPipeError` is affected; any other sink `OSError` propagates under both
policies.

`BrokenPipePolicy` is exported from the package root next to
`RunOutputOptions`, so an existing
`from cuprum import Program, ProgramCatalogue, RunOutputOptions, sh` line only
needs `BrokenPipePolicy` added to it.

### Quiet children

A long build or download can go quiet for minutes, and a silent terminal is
hard to tell apart from a stuck one. `RunOutputOptions(idle_after=30.0)`
enables an optional heartbeat after 30 seconds without outward output, then
once per further silent interval. The default is off. The built-in notification
is one line, such as `[cuprum] still running cargo (idle 30s, total 4m10s)`,
written to the configured stderr sink or `sys.stderr`; it is never mixed into
captured child output. A pipeline monitors its final stdout and all stage
stderr streams.

`on_idle(total, idle)` replaces the built-in notification, receiving the
seconds since the run started and since output was last seen:

<!-- tested-example: idle-heartbeat -->

```python
import sys

from cuprum import Program, ProgramCatalogue, RunOutputOptions, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="idle")
python = sh.make(Program(sys.executable), catalogue=catalogue)
heartbeats = []
result = python("-c", "import time; time.sleep(0.6)").run_sync(
    output=RunOutputOptions(
        idle_after=0.1,
        on_idle=lambda total, idle: heartbeats.append((total, idle)),
    ),
)
assert result.ok
assert heartbeats, "a silent child should produce at least one heartbeat"
```

Without bounded delivery, the renderer's sink write and flush run on the
command's event loop; keep them prompt. `on_idle` is always called
synchronously on that loop, even when bounded delivery is enabled, so the
callback must also stay prompt. A heartbeat reports silence, not a deadlock,
and does not alter the deadline or exit status.

To move the built-in heartbeat write off the loop, opt in to bounded delivery
through `RunOutputOptions`:

<!-- tested-example: bounded-delivery-idle -->

```python
import io
import sys

from cuprum import (
    BoundedDelivery,
    ExecutionContext,
    Program,
    ProgramCatalogue,
    RunOutputOptions,
    sh,
)

catalogue = ProgramCatalogue.from_programs(sys.executable, name="idle-delivery")
python = sh.make(Program(sys.executable), catalogue=catalogue)
heartbeat_log = io.StringIO()
result = python("-c", "import time; time.sleep(0.08)").run_sync(
    output=RunOutputOptions(
        idle_after=0.02,
        delivery=BoundedDelivery(max_queued_bytes=64 * 1024),
    ),
    context=ExecutionContext(stderr_sink=heartbeat_log),
)

assert result.ok
assert heartbeat_log.getvalue().startswith("[cuprum] still running")
```

The same option moves echoed child output to one run-owned daemon worker. Its
single FIFO preserves the order in which the loop enqueues stdout and stderr
chunks, including in pipelines. `max_queued_bytes` bounds queued payload bytes,
accounted from each original output chunk before decoding; one payload already
being written can be in flight separately. If a chunk would exceed the bound,
Cuprum drops that presentation chunk without blocking the loop and reports its
byte count as `DROPPED`. Captured output remains complete.

A single chunk larger than `max_queued_bytes` is dropped even when the queue is
empty. Choose a bound at least as large as the largest expected output chunk.

### Send a stream to a file or the parent

Capture keeps output in memory for the caller to read. When output should land
somewhere else, such as a log file or the parent's own terminal, redirect the
stream instead. `RunOutputOptions.stdout` and `.stderr` accept a `StdioTarget`,
and the four kinds differ in who owns the destination (Table 2).

| Target                  | The child's stream goes to                                                | Owned by |
| ----------------------- | ------------------------------------------------------------------------- | -------- |
| `StdioTarget.pipe()`    | A pipe Cuprum reads, for capture, echo, line observation, or a heartbeat. | Cuprum   |
| `StdioTarget.inherit()` | The parent's own stream, passed straight through.                         | Parent   |
| `StdioTarget.path(p)`   | The file at `p`, opened just before the spawn and closed just after.      | Cuprum   |
| `StdioTarget.fd(f)`     | A descriptor or open file object; flushed before the spawn, never closed. | Caller   |

_Table 2: Standard-stream targets._

When a stream is left unset, Cuprum uses a pipe if something needs to read it
and discards the output otherwise. `inherit()` is therefore the way to let an
unobserved stream reach the parent's terminal.

Any target other than `pipe()` counts as a redirect. Capture is on by default,
so pass `capture=False` when redirecting stdout:

<!-- tested-example: redirect-stdout-to-a-file -->

```python
import sys
import tempfile
from pathlib import Path

from cuprum import Program, ProgramCatalogue, RunOutputOptions, StdioTarget, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="redirect")
python = sh.make(Program(sys.executable), catalogue=catalogue)
log = Path(tempfile.mkdtemp()) / "out.log"
result = python("-c", "print('redirected')").run_sync(
    output=RunOutputOptions(capture=False, stdout=StdioTarget.path(log)),
)
assert result.stdout is None, "a redirected stream is not captured"
assert log.read_text(encoding="utf-8") == "redirected\n"
```

A few combinations are contradictory, and Cuprum rejects them with `ValueError`
before the child starts, rather than silently choosing one side:

- **Capture or echo with a redirected stream, including `inherit()`.** Both
  read from a pipe, and a redirected stream has none. Line observation and the
  heartbeat are not rejected, but they see nothing from a redirected stream.
- **One path for both stdout and stderr.** Each open starts at offset 0, so
  the two streams would overwrite each other. Use two paths, or one `fd()`
  target the caller manages.
- **A redirected stdout with `lines()`, or either stream with `start()`.** Both
  read the child's output in the parent. Use `run()` or `run_sync()` instead.
- **An inherited stdin alongside a stdin source.**
  `RunOutputOptions(stdin=StdioTarget.inherit())` with `stdin=StdinInput(...)`
  or `stdin=StdinStream(...)` gives two answers to where the input comes from.
  This is checked when the run starts. A `pipe()` stdin target with a source is
  accepted, because the source is written through that pipe.
- **Targets on a `Pipeline`.** Redirect a single command instead.
- **`path()` or `fd()` on Windows.** File and descriptor redirection needs
  POSIX descriptors; `pipe()` and `inherit()` work everywhere.

Reusing one `fd()` target across several runs shares its file offset, so each
run continues where the previous one stopped. `RunOutputOptions.stdin` accepts
only `pipe()` and `inherit()`: the input itself is passed to the run call's
`stdin=` argument, as it has always been; see
[Stream input from a producer](#stream-input-from-a-producer).

### Capture policies

Capture is configured per stream, and a stream's policy decides where its bytes
are kept. `capture=True` and `capture=False` are the shorthands for the
whole-run switch, and each stream can name its own policy with `capture_stdout`
and `capture_stderr`:

| Policy                        | What the run reports                                     |
| ----------------------------- | -------------------------------------------------------- |
| `True`                        | Complete text, held in the parent's memory. The default. |
| `False`                       | Nothing: `None` for that stream, and no record.          |
| `FullCapture()`               | The same as `True`, stated explicitly.                   |
| `BoundedCapture(max_bytes=N)` | The last `N` bytes, held in memory.                      |
| `FileBackedCapture(...)`      | Text in memory until the threshold, then a named file.   |

For `run()` and pipelines, a per-stream field left at `None` inherits
`capture`, and a per-stream value always wins for its own stream:
`capture=True, capture_stderr=False` captures stdout in full and nothing on
stderr. `SafeCmd.start()` is the handle-specific exception: it forces both
streams on, while retaining any explicit capture policy. Mixing `capture=` with
`capture_stdout=` is allowed and well defined, not an error. A value that is
neither a policy nor a `bool` is refused with `ValueError` before the child is
spawned.

A stdout or stderr target other than `pipe` is refused while that stream's
resolved capture policy or echo is enabled, because the parent has no pipe to
read. Redirection never disables capture or echo automatically. Set the matching
`capture_stdout`/`capture_stderr` field to `False` (or use `capture=False`
while leaving that field unset), and set the matching
`echo_stdout`/`echo_stderr` field to `False` (or use `echo=False` while leaving
that field unset). An explicit per-stream field still overrides its shorthand.

### Bounded capture

`BoundedCapture` keeps the tail of a stream in memory and reports what it
dropped. The bound counts **bytes, not characters**, so a multi-byte character
is never split _in text capture_; a text run's retained tail is always
decodable on its own. A byte-exact run (`run_bytes()`) asks for the child's own
bytes, so it is handed the exact byte tail instead: trimming it to a code-point
boundary would discard data the caller asked for, and that tail need not be
independently decodable.

<!-- tested-example: users-guide-bounded-capture -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.capture_policy import BoundedCapture
from cuprum.sh import RunOutputOptions

catalogue = ProgramCatalogue.from_programs(sys.executable, name="bounded-capture")
command = sh.make(Program(sys.executable), catalogue=catalogue)(
    "-c", "import sys; sys.stdout.write('abcdefghij')"
)
options = RunOutputOptions(
    capture_stdout=BoundedCapture(max_bytes=4), capture_stderr=False
)
result = command.run_sync(output=options)
assert result.stdout == "ghij"

# The record says how much of the stream the result does not show.
(record,) = result.captures
assert record.stream == "stdout"
assert record.policy == "bounded"
assert record.total_bytes == 10
assert record.dropped_bytes == 6
assert record.truncated is True
```

A bound is a decision about what the _result_ keeps. It never shortens what a
pipeline stage hands to the stage after it, and it never shortens the mirrored
copy an echo sink receives; those read the child's own pipe.

Because the head of the stream is gone, a bounded _text_ capture can only be
trimmed back to a character boundary for encodings whose boundaries survive
that loss. UTF-8, the fixed-width UTF-16 and UTF-32 codecs, and single-byte
encodings such as Latin-1 or CP1252 all qualify. A multi-byte East Asian codec
— Shift-JIS, EUC-JP, GBK, Big5, and their relatives — does not: once the lead
byte is dropped, its trail byte is indistinguishable from a character of its
own. A run that pairs `BoundedCapture` with such an encoding is refused with
`ValueError` before the child is spawned, rather than returning text the child
never wrote. Use `FullCapture` or `FileBackedCapture` for those encodings, or
capture the stream as bytes, where nothing is decoded and no cut can be misread.

### File-backed capture

`FileBackedCapture` keeps the stream on the parent's heap until it passes
`spill_threshold_bytes`, then moves it to a file and appends there. Past the
threshold the parent's memory stops tracking the child's output, so a long or
noisy command no longer costs memory proportional to what it wrote:

<!-- tested-example: users-guide-file-backed-capture -->

```python
import sys
import tempfile

from cuprum import Program, ProgramCatalogue, sh
from cuprum.capture_policy import FileBackedCapture
from cuprum.sh import RunOutputOptions

catalogue = ProgramCatalogue.from_programs(sys.executable, name="file-backed")
command = sh.make(Program(sys.executable), catalogue=catalogue)(
    "-c", "import sys; sys.stdout.write('z' * 4096)"
)
with tempfile.TemporaryDirectory() as directory:
    options = RunOutputOptions(
        capture_stdout=FileBackedCapture(spill_threshold_bytes=64, directory=directory),
        capture_stderr=False,
    )
    result = command.run_sync(output=options)
    (record,) = result.captures
    assert record.storage == "file"
    # Text mode reports the spill as a file rather than reading it back, so
    # the result carries no text for a stream the parent did not retain.
    assert result.stdout is None
    assert record.path is not None
    # The run removes its own file before returning; the record is a statement
    # that the spill happened, not a promise the file is still there.
    assert not record.path.exists()
```

In byte mode (`run_bytes()`) a spilled capture is read back, because a caller
who asked for the child's own bytes cannot be handed anything less. That read
is O(stream size), so byte mode does not carry the bounded-memory guarantee
that text mode does.

`directory` defaults to the system temporary directory, and the file's name is a
`NamedTemporaryFile` name. Pass `keep_file=True` to keep it: the file then
survives the run, `record.owned` is `False`, and removing it is the caller's
job. Without it, `record.owned` is `True` and the run removes the file before
returning the result — on the success path, on a non-zero exit, on a timeout,
and on cancellation alike. One cancellation is the exception: if the run stops
before the initial spill's prefix is written, the file has not become the
caller's yet and cleanup removes it even under `keep_file=True`.

Spill I/O runs off the event loop but is shielded, so a write already handed to
a worker is never abandoned mid-flight. The operational cost is that a stalled
spill filesystem — a hung NFS mount, a frozen FUSE filesystem — stalls the run
with it: the child's deadline may terminate the process, but the run cannot
settle until the blocked write returns. Put `directory` on a local, responsive
filesystem when the timeout and cancellation guarantees matter.

A failure on any of the spill file's routes — create, append, read back in byte
mode, or remove at the end of the run — is absorbed rather than raised: a
failure of the storage is not a failure of the command that produced it. When
the run otherwise returns, the caller still receives a result and the three
retention failures set `storage_failed=True` on the record. Read that flag
together with `storage`, because the pair distinguishes the cases:

- The file could not be created. The stream never left memory, so `storage`
  stays `"memory"`, `path` is `None`, and the capture is _complete_ — the
  caller has the whole stream and only lost the spill.
- An append failed after the spill began. `storage` is `"file"` and `path`
  names the file; no further writes are accepted, but the bytes accepted before
  the failure are offered in a final flush. If that flush and byte-mode
  read-back succeed, the returned payload includes those bytes. If the flush
  also fails, the earlier append-failure event remains authoritative, and
  buffered bytes are not guaranteed in the payload. `storage_failed=True` is
  what tells the difference between a short capture and an incomplete one.
- Byte-mode read-back failed. `storage` is `"file"` but no text or bytes are
  reported, because the bytes could not be recovered. The file follows the
  policy rather than the failure: it is left in place only under
  `keep_file=True`, so retrying the read at `record.path` requires that
  setting. Under the default, cleanup removes the file before the run returns.

A failed removal sets nothing: it is housekeeping, and cleanup runs on the way
out of every run, including one already carrying the caller's own timeout or
cancellation. An error about a temporary file must not replace either, so that
route absorbs its own failure silently: it sets no flag and emits no
`CaptureEvent`, and the surviving file is the only trace. A storage failure
never turns a timeout or cancellation into an ordinary result; it is absorbed
so the run's own outcome stands.

### Reading a stream's capture metadata

Every captured stream contributes one `CapturedStream` to `result.captures`, in
stdout-then-stderr order, and a stream that was not captured contributes none.
The record is how a caller learns that a result is a tail rather than the whole
stream, or that the payload is a file rather than text:

| Field            | Meaning                                       |
| ---------------- | --------------------------------------------- |
| `stream`         | `"stdout"` or `"stderr"`.                     |
| `policy`         | `"full"`, `"bounded"`, or `"file_backed"`.    |
| `total_bytes`    | What the drain received, not what was kept.   |
| `dropped_bytes`  | What the bound discarded. `0` for the others. |
| `storage`        | `"memory"` or `"file"`.                       |
| `path`           | The spill file, when the storage is a file.   |
| `owned`          | Whether this run still must remove that file. |
| `storage_failed` | Whether the spill failed, as above.           |
| `truncated`      | Derived: `dropped_bytes > 0`.                 |

For a kept spill, `record.read_text()` reads the path synchronously and may
block. A memory-backed record raises `ValueError`; a missing or unreadable path
raises `OSError`. Unknown encodings or error handlers raise `LookupError`, and
strict decoding can raise `UnicodeError`. Callers that need to avoid blocking
can offload the read to a worker thread.

`truncated` is derived rather than stored, so it cannot disagree with the count
beside it. Spill transitions and storage failures are also published as
`CaptureEvent`s; see [Observe production runs](#observe-production-runs).

On a pipeline, `PipelineResult.captures` reports the final stage alone: an
interior stage relays its stdout to the next stage rather than capturing it, so
those bytes have no record anywhere. An interior stage's stderr, when that
stage captures it, is recorded on that stage's own `stages[i].captures`; a
stage that captured neither stream has an empty `captures`.

### Binary output

Capture decodes by default, so `stdout` and `stderr` are strings and bytes the
child wrote that are not valid UTF-8 arrive with each offending byte replaced by
`U+FFFD`. That is the right answer for text and the wrong one for anything
else, such as an archive or an image: a decoded capture cannot be turned back
into the bytes the child wrote.

`SafeCmd.run_bytes()` and `Pipeline.run_bytes()`, with their `run_bytes_sync()`
counterparts, are the same runs as `run()` and `run_sync()` with capture left
undecoded:

<!-- tested-example: binary-output -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="binary-output")
python = sh.make(Program(sys.executable), catalogue=catalogue)
payload = bytes(range(256)) + b"\xff\x00\xfe\x80"
result = python(
    "-c", f"import sys; sys.stdout.buffer.write({payload!r})"
).run_bytes_sync()
assert result.ok
assert result.stdout == payload
```

The same command through `run_sync()` reports `stdout` as text: a `str` of the
same length whose invalid bytes have each become `U+FFFD`, which is why the
comparison above can only pass in binary mode.

The byte-mode entry points take the same `output`, `timeout`, `context`, and
(for a single command) `stdin` arguments, and return `BytesCommandResult` or
`BytesPipelineResult` instead of their text-mode counterparts. Those two
classes declare the same fields as `CommandResult` and `PipelineResult`, so
each command or stage keeps the full set of measurements (`pid`, `started_at`,
`duration`, `max_rss_bytes`, `user_cpu_seconds`, `system_cpu_seconds`, and
`relay_fallbacks`). The output fields of a `BytesCommandResult` are
`bytes | None`, and each stage of a `BytesPipelineResult` is a
`BytesCommandResult`.

Byte-exact results carry `captures` too, with the same records and meaning as
in text mode, and `BytesPipelineResult.captures` reads the final stage exactly
as its `stdout` does. A byte payload cannot say on its own whether a policy
shortened it, moved it to a file, or failed to retain it, so the records are
how a `run_bytes()` caller learns any of that. A bounded byte capture returns
the retained bytes and reports the discarded count, and a spilled byte capture
returns the bytes read back from its file.

Text remains the default, and the mode is explicit: `run()` never returns bytes
and `run_bytes()` never returns text, so a mismatch is a bug rather than a
runtime surprise. Everything the text-mode entry points promise still holds —
timeout partial output is carried through in the same field, `capture=False`
still yields `None` rather than `b""`, and an external cancellation still
re-raises `asyncio.CancelledError` instead of being reported as a timeout.

Binary mode covers capture. A run that asks for `on_line` cannot be given one:
the capture it returns is byte-exact, so a callback carrying decoded text would
be a second, contradictory contract for the same stream.
`run_bytes(output=RunOutputOptions(on_line=lines.append))` is rejected with
`ValueError` before the child is spawned. Echoing, sinks, and idle heartbeats
are unaffected, since they mirror output rather than report it; a sink that
cannot encode the child's bytes records a `relay_fallback` exactly as it does
in text mode.

The refusal applies only to a caller-supplied `on_line`. Structured observation
registered with `sh.observe()` still fires in byte mode: those hooks receive
decoded lines, so undecodable bytes are replaced in the _event_ while the
captured payload keeps the child's own bytes. The two travel on separate
channels, and neither is a substitute for the other.

That replacement is scoped to byte mode. `ExecutionContext.errors` keeps its
meaning everywhere else: a text-mode run with `errors="strict"` still raises
`UnicodeDecodeError` from line observation and from echo on undecodable bytes.
A byte-exact run instead returns the child's own bytes for the caller to
validate — the run cannot both promise bytes nobody decoded and let a decode
decide its outcome.

For a pipeline, the same rule applies to each stage: only the final stage's
stdout is captured, interior stdout feeds the next stage and is reported as
`None`, and each stage reports its own `stderr`.
[Byte-exact runs in telemetry](#byte-exact-runs-in-telemetry) describes how
logs, metrics, and traces tell the two modes apart.

## Supply input, environment, and a deadline

Everything about _how_ a command runs, as opposed to _what_ it runs, is passed
to `run_sync()` or `run()` rather than to the builder:

- **Input:** `stdin=StdinInput(text=...)` sends text, encoded with the
  context's encoding; `StdinInput(data=...)` sends bytes. Specify only one. For
  input too large to hold in memory, stream it instead; see
  [Stream input from a producer](#stream-input-from-a-producer).
- **Environment and directory:** `context=ExecutionContext(...)` carries `env`,
  `cwd`, and the other per-run settings. `env` is layered over the live parent
  environment under the default `EnvMode.OVERLAY`, so an empty mapping still
  inherits it. `EnvMode.REPLACE` (imported from `cuprum.context`) instead
  starts the child from an empty environment, so only the supplied mapping is
  visible; see
  [Choose how a child environment is composed](#choose-how-a-child-environment-is-composed).
  `cwd` changes the child's working directory.
- **Deadline:** `timeout=` sets a limit in seconds. A call-level `timeout`
  overrides `ExecutionContext.timeout`.

<!-- tested-example: input-and-context -->

```python
import sys

from cuprum import Program, ExecutionContext, ProgramCatalogue, StdinInput, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="input")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python(
    "-c", "import os,sys; print(os.getenv('CUPRUM_EXAMPLE'), sys.stdin.read())"
)
result = command.run_sync(
    context=ExecutionContext(env={"CUPRUM_EXAMPLE": "ready"}),
    stdin=StdinInput(text="hello"),
    timeout=5,
)
assert result.stdout == "ready hello\n"
```

A timeout raises `TimeoutExpired` rather than returning a result, so catch it
separately from a non-zero exit;
[Handle a slow command](#handle-a-slow-command) shows how. Cancelling an async
run also starts child teardown. `cancel_grace` in `ExecutionContext` configures
the wait between termination and forced kill, and defaults to 0.5 seconds. When
the caller needs to decide later when a child stops, `start()` hands back an
owned handle to stop by hand instead; see
[hold a running child](#hold-a-running-child-and-decide-when-to-stop-it).

### Stream input from a producer

`StdinInput` holds the whole payload in memory. For input that is large, or
that is produced as the run goes, pass `stdin=StdinStream(chunks=...)` with an
async iterable instead:

<!-- tested-example: streaming-stdin -->

```python
import sys
from collections.abc import AsyncIterator

from cuprum import Program, ProgramCatalogue, StdinStream, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="stream-input")
python = sh.make(Program(sys.executable), catalogue=catalogue)


async def rows() -> AsyncIterator[str]:
    for number in range(1, 1001):
        yield f"row {number}\n"


count_lines = python("-c", "import sys; print(sum(1 for _ in sys.stdin))")
result = count_lines.run_sync(stdin=StdinStream(chunks=rows()))
assert result.stdout == "1000\n"
```

Cuprum pulls one chunk, writes it, and waits for the write to drain before
pulling the next, so a slow reader slows the producer down rather than letting
input pile up in memory. A drained write is not proof that the child has read
the chunk: some bytes can still sit in the transport buffer or the OS pipe.
Each chunk is held in full while it is written, so yield chunks of a bounded
size when peak memory matters.

- **Chunk types:** a `str` chunk is encoded with the context's `encoding` and
  `errors`, and a character split across two chunks is still encoded correctly.
  A `bytes` chunk is written as it is.
- **Producer failure:** if the producer raises, or yields something that is
  neither `str` nor `bytes`, Cuprum terminates the child and raises
  `StdinSourceError`, with the original exception as `__cause__`. Cancellation
  propagates unchanged.
- **A child that stops reading:** closing stdin early, as `head` does, is not
  an error. The run continues to the child's exit code, and the cut-short write
  is reported as a `stdin_error` event.
- **Clean-up:** on every exit path, Cuprum closes the child's stdin and then
  calls the producer's `aclose()`, when it has one. Both waits are bounded: a
  producer whose `aclose()` takes longer is cancelled, and one that ignores the
  cancellation may still be cleaning up after the run returns. A generator
  needs no closing by the caller.

## Connect a pipeline

Use `|` between command objects. Only final-stage stdout is captured; each
stage has an exit result. Inspect `PipelineResult.ok` or `failure_index`, since
the final stage can succeed after an earlier failure.

<!-- tested-example: pipeline -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="pipeline")
python = sh.make(Program(sys.executable), catalogue=catalogue)
producer = python("-c", "print('hello')")
consumer = python("-c", "import sys; print(sys.stdin.read().upper(), end='')")
result = (producer | consumer).run_sync()
assert result.ok
assert result.stdout == "HELLO\n"
assert [stage.exit_code for stage in result.stages] == [0, 0]
```

With capture enabled, the final stage's `stdout` and every stage's `stderr` are
strings, including `""` for empty streams. The final stage can be narrowed with
`result.final.require_captured()`; an interior stage raises `ValueError`
because its `stdout` was passed to the next process. Pipeline results keep their
`PipelineResult` type and optional stream annotations.

Timeouts still raise `TimeoutExpired`, whose `output` and `stderr` remain
`str | bytes | None` because the streams may be partial when the child stops.

When a non-final stage fails while peers remain active, Cuprum terminates those
peers. The first observed failure is recorded in `failure_index`;
near-simultaneous completions may use stage order as a tie-break. Interior
stdout is consumed by the next stage and appears as `None` in that stage's
result.

## Run commands concurrently

`run_concurrent()` is async; `run_concurrent_sync()` is for synchronous
callers. Set `ConcurrentConfig.concurrency` to bound simultaneous children.
Results follow submission order. `failures` indexes the returned result tuple;
`failure_submission_indices` maps failures to the original commands when
fail-fast cancellation omitted some results.

<!-- tested-example: concurrent -->

```python
import sys

from cuprum import Program, ConcurrentConfig, ProgramCatalogue, run_concurrent_sync, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="concurrent")
python = sh.make(Program(sys.executable), catalogue=catalogue)
commands = [python("-c", f"print({number})") for number in (1, 2)]
result = run_concurrent_sync(*commands, config=ConcurrentConfig(concurrency=2))
assert result.ok
assert [item.stdout for item in result.results] == ["1\n", "2\n"]
assert result.submission_indices == (0, 1)
```

With `fail_fast=True`, remaining commands are cancelled after the first
non-zero exit. A command that already finished can still appear in results. Use
`submission_indices` rather than assuming a compacted result position equals
its original submission index.

## Apply a policy

A catalogue controls builder creation. A scope additionally narrows which
commands may run. Nested scopes cannot widen their parent's allowlist.
Registrations such as `env()`, `before()`, `after()`, and `observe()` are
context-local and are removed when their context manager exits. They do not
change global environment variables.

`scoped(catalogue=catalogue)` activates a catalogue for the block, so
`sh.make()` inside it resolves the program from that catalogue without being
told again:

<!-- tested-example: scoped-catalogue-resolution -->

```python
import sys

from cuprum import ECHO, Program, ProgramCatalogue, UnknownProgramError, scoped, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="scoped-policy")

# Outside any catalogue scope, the default catalogue still applies.
assert sh.make(ECHO)("-n", "hello").project.name == "core-ops"

with scoped(catalogue=catalogue):
    build_python = sh.make(Program(sys.executable))
    assert build_python("-c", "pass").project.name == "scoped-policy"

# A builder resolved inside the scope keeps its catalogue after the block exits.
assert build_python("-c", "pass").project.name == "scoped-policy"

# A catalogue missing the program rejects it, rather than falling back.
other = ProgramCatalogue.from_programs(Program("some-other-tool"), name="other")
with scoped(catalogue=other):
    try:
        sh.make(Program(sys.executable))
    except UnknownProgramError:
        pass
    else:
        raise AssertionError("the scoped catalogue should reject the program")
```

`sh.make()` resolves its catalogue in this order: an explicit `catalogue`
argument, then the innermost active `scoped(catalogue=...)`, then
`DEFAULT_CATALOGUE` outside any catalogue scope. A scope that supplies only an
allowlist, such as `scoped(ScopeConfig(allowlist=...))`, does not change which
catalogue is active. Resolution happens once, when `sh.make()` is called: the
returned builder keeps its catalogue for its whole life, including after the
scope exits.

### Two enforcement points

Cuprum checks the catalogue when a builder is created and the allowlist when a
command runs. The two checks answer different questions, and they can disagree:

- **`sh.make()` checks membership.** It looks the program up in the resolved
  catalogue and raises `UnknownProgramError` if the catalogue does not list it.
  This catches a typo or a misconfigured catalogue before any builder exists.
- **The active scope checks permission.** When a command runs, the allowlist of
  whichever context is active at that moment decides whether the program may
  execute. If it may not, `run()` and `run_sync()` raise
  `ForbiddenProgramError` before a process starts.

A builder constructed from a catalogue the enclosing scope does not allow
therefore constructs successfully and fails only at run time:

<!-- tested-example: enforcement-points -->

```python
import sys

from cuprum import (
    ForbiddenProgramError,
    Program,
    ProgramCatalogue,
    current_context,
    scoped,
    sh,
)

allowed = ProgramCatalogue.from_programs(sys.executable, name="allowed")
foreign = ProgramCatalogue.from_programs(Program("some-other-tool"), name="foreign")
other = Program("some-other-tool")

with scoped(catalogue=allowed):
    # Construction consults the named catalogue, so this succeeds.
    command = sh.make(other, catalogue=foreign)("--version")
    # Execution consults the scope, which does not allow the program.
    assert not current_context().is_allowed(other)
    try:
        command.run_sync()
    except ForbiddenProgramError:
        pass
    else:
        raise AssertionError("the scope should forbid the foreign program")
```

The two catalogues need not agree: each is a separate policy surface. Naming a
catalogue explicitly is how a caller opts out of the scope's catalogue for
construction, but it never opts out of the scope's allowlist for execution.

`ScopeConfig` accepts allowlist, hook, timeout, and environment settings when a
catalogue alone is insufficient. `allow()` is the explicit way to widen policy:
it adds programs to the current allowlist, even inside a restricted scope,
until the registration is detached or its block exits. Calling `allow()` in the
default unrestricted context restricts it to exactly the programs allowed.
Review `allow()` calls as policy changes.

A failing `before` or `after` hook is application code and can affect a run; see
[Run code around every command](#run-code-around-every-command). Use
`observe()` for structured `ExecEvent` lifecycle events; telemetry adapters in
`cuprum.adapters` project them to logging, metrics, or tracing. Avoid making
untrusted arguments into metric labels or log fields.

## Observe production runs

Cuprum offers several observation channels, and each answers a different
question. Choose by the question:

- **Is a quiet job still alive?** Set `idle_after`; see
  [Quiet children](#quiet-children). A heartbeat means no monitored output was
  read; it says nothing about whether the child is stuck.
- **What is the child printing right now?** Use `on_line` or `lines()`; see
  [Line-level output](#line-level-output).
- **What ran, for how long, and how did it end?** Register lifecycle hooks or
  the ready-made logging, metrics, and tracing adapters; see
  [Log, measure, and trace runs](#log-measure-and-trace-runs).
- **Could a secret reach the logs?** Give the exporting adapters a redaction
  policy; see [Keep secrets out of telemetry](#keep-secrets-out-of-telemetry).
- **How is the job presented in CI?** On GitHub Actions,
  `RunOutputOptions(group=True, annotate_failure=True)` frames echoed output
  and failures; see
  [Present output in GitHub Actions](#present-output-in-github-actions).
- **How are the stream paths behaving?** Aggregate stream-operation events and
  Rust-pump events cover throughput and routing; see
  [Other observation channels](#other-observation-channels). A Rust pump
  decline means Cuprum used another stream path, not that the child failed.
- **What did a capture policy do to the output?** `observe_capture` reports a
  `CaptureEvent` when a bound discards bytes, a stream spills to a file, or a
  spill-file create, write, flush, or read-back fails; see
  [Other observation channels](#other-observation-channels).

Keep callbacks bounded, and avoid recording payloads or arguments as metric
labels.

## Migrate a caller

Moving a `subprocess` call to Cuprum is mostly a matter of moving each argument
to its new home. Here is the same command before and after:

<!-- tested-example: migrate-from-subprocess -->

```python
import subprocess
import sys
import tempfile
from pathlib import Path

from cuprum import ExecutionContext, Program, ProgramCatalogue, sh

script = "import os; print(os.path.basename(os.getcwd()))"
workdir = tempfile.mkdtemp()

# Before: subprocess, checking the exit status and capturing text.
before = subprocess.run(
    [sys.executable, "-c", script],
    capture_output=True,
    text=True,
    check=True,
    cwd=workdir,
)

# After: declare the program once, then build and run the command.
catalogue = ProgramCatalogue.from_programs(sys.executable, name="migrated")
python = sh.make(Program(sys.executable), catalogue=catalogue)
result = python("-c", script).run_sync(context=ExecutionContext(cwd=workdir))
if not result.ok:
    raise RuntimeError(f"exited with {result.exit_code}: {result.stderr}")

assert result.stdout == before.stdout == f"{Path(workdir).name}\n"
Path(workdir).rmdir()
```

Table 3 maps the other common `subprocess` arguments.

| `subprocess`                             | Cuprum                                                                                            |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `"git status --short"` with `shell=True` | `sh.make(GIT)("status", "--short")`: one Python argument per child argument                       |
| `capture_output=True, text=True`         | The default: `result.stdout` and `result.stderr` hold decoded text                                |
| `check=True`                             | Test `result.ok` and raise according to the application's error policy                            |
| `cwd=...`                                | `context=ExecutionContext(cwd=...)`                                                               |
| `env=...` (replaces the environment)     | `context=ExecutionContext(env=..., env_mode=EnvMode.REPLACE)`                                     |
| `input=...`                              | `stdin=StdinInput(text=...)` or `StdinInput(data=...)`                                            |
| `timeout=...`                            | `timeout=...`; catch `cuprum.TimeoutExpired`, a `TimeoutError`                                    |
| A shell pipe, `a \| b`                   | `(a_cmd \| b_cmd).run_sync()`; see [Connect a pipeline](#connect-a-pipeline)                      |
| `stdout=None` (inherit the terminal)     | `output=RunOutputOptions(capture=False, echo=True)`                                               |
| `subprocess.Popen` and `.terminate()`    | `await cmd.start()`; see [Hold a running child](#hold-a-running-child-and-decide-when-to-stop-it) |

_Table 3: Where each `subprocess` argument goes in Cuprum._

Note the environment row: `subprocess` replaces the child's environment with
`env=`, whereas Cuprum layers `ExecutionContext.env` over the parent's unless
`EnvMode.REPLACE` (from `cuprum.context`) asks for a replacement. Keep flags
positional unless the tool accepts `--name=value`; see
[Build arguments deliberately](#build-arguments-deliberately). Applications
upgrading from Cuprum 0.1 should also read the
[0.2.0 migration guide](v0-2-0-migration-guide.md).

## Troubleshoot a run

| Symptom                                                    | Check                                                                                                 |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `ImportError` or `AttributeError` for a name in this guide | An older Cuprum may be installed; see [Install Cuprum](#install-cuprum).                              |
| `UnknownProgramError`                                      | Register the exact program in the builder's catalogue, or in the active `scoped(catalogue=...)`.      |
| `ForbiddenProgramError`                                    | Inspect the active scope and parent allowlist; see [Two enforcement points](#two-enforcement-points). |
| `TypeError` while building a command                       | The message names the problem: an unsupported value type, `None`, or a reserved execution option.     |
| `FileNotFoundError`                                        | Confirm the executable path or inherited `PATH`.                                                      |
| `ExecutableResolutionError`                                | An executable-binding resolver failed; its own exception is chained as `__cause__`.                   |
| `TimeoutExpired`                                           | Set an appropriate deadline; inspect the child for slow or blocked work.                              |
| `ValueError` naming a redirected stream                    | See the rejected combinations in [Send a stream to a file](#send-a-stream-to-a-file-or-the-parent).   |
| `StdinSourceError`                                         | The stdin producer failed; its own exception is chained as `__cause__`.                               |
| `RedactionError`                                           | A redaction rule failed; nothing was exported unredacted. Check the rule it names.                    |
| `SignalAfterExitError`                                     | The child has already exited; treat the recorded outcome as the command's ending.                     |
| `UnsupportedSignalError`                                   | Use `terminate()` or `kill()`, which use the platform's own signals.                                  |
| Non-zero `exit_code`                                       | Use captured stderr and the tool's documented exit codes.                                             |
| No visible output                                          | Capture is silent by default; pass `RunOutputOptions(echo=True)` or read `result.stdout`.             |
| No heartbeat                                               | Supply a positive `idle_after`; output activity resets its clock.                                     |
| Descendant processes outlive a timeout                     | Opt in to [process-group ownership](#reclaim-a-descendant-with-process-group-ownership) on POSIX.     |
| No native speed-up                                         | The optional extension may be unavailable or this operation may use Python.                           |

_Table 4: First checks for common execution outcomes._

For the optional native extension, see
[Choosing a stream backend](#choosing-a-stream-backend) and
[Troubleshooting the native extension](#troubleshooting-the-native-extension).
Benchmarking and the implementation's verification are maintainer topics,
covered in the
[developers' guide](developers-guide.md), [Rust boundary verification](rust-boundary-verification.md),
and the [Cuprum design](cuprum-design.md); none of them is needed to run
commands.

## Going further

These recipes build on the sections above for tasks that come up once an
application relies on Cuprum. Each one is a complete, executed example.

### Define an application catalogue

Give an application's programs a named project so that every command carries
its documentation links and log-noise rules. `ProgramCatalogue.from_project()`
builds a catalogue from one `ProjectSettings`; use
`ProgramCatalogue(projects=(...))` when several projects must each own their
programs. Cuprum stores `noise_rules` for downstream log processing but does
not filter output itself.

<!-- tested-example: application-catalogue -->

```python
import sys

from cuprum import Program, ProgramCatalogue, ProjectSettings, sh
from cuprum.catalogue import DuplicateProgramError

PYTHON = Program(sys.executable)
settings = ProjectSettings(
    name="report-builder",
    programs=(PYTHON,),
    documentation_locations=("docs/reports.md",),
    noise_rules=(r"^progress:",),
)
catalogue = ProgramCatalogue.from_project(settings)

command = sh.make(PYTHON, catalogue=catalogue)("-c", "print('ok')")
assert command.project.name == "report-builder"
assert command.project.noise_rules == (r"^progress:",)
assert catalogue.lookup(PYTHON).project.documentation_locations == ("docs/reports.md",)

try:
    ProgramCatalogue.from_programs(PYTHON, PYTHON)
except DuplicateProgramError as exc:
    assert exc.program == PYTHON
else:
    raise AssertionError("a program listed twice should be rejected")
```

`catalogue.visible_settings` returns a read-only mapping from project name to
`ProjectSettings` for services that propagate this metadata. An absolute
program path is allowlisted exactly as written, so `/usr/bin/git` does not also
permit `git` found on `PATH`.

### Wrap commands in project builders

Centralize argument construction in small functions that validate input and
return a `SafeCmd`. Callers then cannot build an unvalidated command line.
`cuprum.builders` provides ready-made builders for git, rsync, and tar, plus the
`safe_path()` and `git_ref()` validators they use. Those builders resolve
their program from the active catalogue — the default catalogue outside any
`scoped(catalogue=...)` block — so building their commands does not require the
tools to be installed; running them does. Inside a catalogue scope, that
scope's catalogue must list the tool.

<!-- tested-example: project-builders -->

```python
import sys
import tempfile
from pathlib import Path

from cuprum import Program, ProgramCatalogue, SafeCmd, sh
from cuprum.builders import git_rev_parse, safe_path

PYTHON = Program(sys.executable)
CATALOGUE = ProgramCatalogue.from_programs(PYTHON, name="line-counter")


def count_lines(path: Path) -> SafeCmd:
    """Build a command that counts the lines in one existing file."""
    checked = safe_path(path.resolve())
    script = "import sys; print(sum(1 for _ in open(sys.argv[1])))"
    return sh.make(PYTHON, catalogue=CATALOGUE)("-c", script, str(checked))


with tempfile.TemporaryDirectory() as workdir:
    notes = Path(workdir) / "notes.txt"
    notes.write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = count_lines(notes).run_sync()
assert result.ok and result.stdout == "3\n"

command = git_rev_parse("main")
assert command.argv_with_program == ("git", "rev-parse", "main")
try:
    git_rev_parse("main..evil")
except ValueError:
    pass
else:
    raise AssertionError("an unsafe ref should be rejected")
```

`safe_path()` rejects empty paths, NUL characters, and `..` segments, and
requires a **host-native** absolute path unless `allow_relative=True` is
passed. Absoluteness is decided by `pathlib` for the running host, so on POSIX
a Windows drive spelling such as `C:/file` is a relative path and is rejected
by default. It accepts names that begin with `-`; separating those names from
options is the builder's job, not the validator's.

`rsync_sync()` accepts local operands only. Both the source and the destination
are rejected when they use rsync remote syntax — `host:path`,
`host::module/path`, `user@host:path`, `[::1]:path`, or `rsync://host/module` —
and that rejection applies even with `allow_relative=True`. To name a local
file that contains a colon, put a directory separator first, as in
`dir/name:part`; rsync reads a colon before the first `/` as a remote
specification. Callers on Windows must spell operands the way their rsync port
does, for example `/cygdrive/c/...` with `allow_relative=True`, because rsync
itself interprets `C:/...` as a remote endpoint. Remote transfers are outside
the core builder set and need a custom builder.

`git_ref()` rejects whitespace, a leading `-`, `..`, `@{`, and other constructs
that Git would interpret as options or revision syntax.

The tar and rsync builders emit `--` before their positional path operands.
`tar_create()` places it after the archive value, so a source named
`--checkpoint=1` is archived as a file rather than parsed as an option, and
`rsync_sync()` places it after the generated flags, so source and destination
names beginning with `-` are copied rather than read as rsync options.
`tar_extract()` needs no separator: its archive and destination are the values
of `-f` and `-C`, and an option's value is never re-read as an option, so
leading dashes there are already literal. A bare `-` is still special:
`tar_extract()` reads the archive from standard input, while `tar_create()`
writes it to standard output when its archive value is `-`.

### Stream lines as they arrive

Use `lines()` when a caller should react to output before the command finishes.
Use it as an async context manager whenever the loop can exit early: leaving
the block closes the stream, which terminates the child in the same way as a
cancelled `run()`.

<!-- tested-example: stream-lines -->

```python
import asyncio
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="follow")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python(
    "-c", "import time\nfor n in range(100):\n print(n, flush=True); time.sleep(0.01)"
)


async def first_line() -> str:
    async with command.lines() as stream:
        async for event in stream:
            # Leaving the block closes the stream and stops the child.
            return event.text
    raise AssertionError("the child should print at least one line")


assert asyncio.run(first_line()) == "0"
```

After a loop that runs to completion, `stream.result` holds the same
`CommandResult` that `run()` would have returned.

`lines()` is lazy: it validates the program and resolves the deadline when the
stream is built, but it does not plan or spawn the child until the first event
is requested. Because those two moments can sit in different scopes, every
decision the building scope made on the caller's behalf is made again in the
task that pulls the first event. A program forbidden by the scope that iterates
the stream raises `ForbiddenProgramError` then — with no plan or start event,
no before hook, and no child — and `stream.result` stays `None`. Preparing a
stream is not, by itself, permission to run the program, and the timeout it was
prepared with does not outlive the scope that supplied it.

The same applies to the executable binding and the environment policy, because
a scope can change more than whether a program may run. If the consuming scope
binds the program to a different file, the stream runs _that_ file: the
prepared binding is discarded rather than reaching the spawn as the child's
executable. `CommandResult.resolved_path` and the `cuprum.resolved_path` span
attribute report the consuming scope's binding accordingly. The environment
overlay and mode are likewise re-read, so the `env_mode` and `env` values on the
`plan`, line, and terminal events describe the policy the run actually used.
The child's own environment already came from the consuming scope at the spawn;
without this the events would have described a policy the run never had.

Registered hooks are re-collected in the consuming scope for the same reason. A
`before`, `after`, or `observe` hook registered around the constructing scope
does not run for a stream that is pulled after that scope has exited: the hooks
that fire are the ones registered where the first event is requested, and the
events describe the same set. Hooks an inner scope inherits through nesting
still apply, because registration layers onto the outer set; it is only the
hooks of a scope the caller has since _left_ that are dropped.

An unterminated line reaches `lines()` in bounded fragments too, exactly as it
reaches `on_line`: each event carries at most `max_line_chars` decoded
characters, the bound applies to both streams, and terminator characters are
not counted. A slow iterator keeps working because the event queue is finite:
the child's drain waits for a free slot instead of buffering output without
limit. Neither mechanism changes what is captured; `max_line_chars` only bounds
how much any single event carries.

`lines()` accepts the same `timeout` as `run()`, and the deadline it enforces
includes the time the iterator spends waiting on a caller that has stopped
consuming. A line already yielded is never taken back; once the deadline
expires, iteration ends with `TimeoutExpired` and whatever had been captured
stays available on the exception.

A caller that has stopped iterating is a suspended coroutine, so nothing can be
raised _into_ it at the deadline. The expiry is recorded when it happens and is
observed when the caller next asks for an item: an idle consumer is not woken
at the deadline, and the exception arrives on the next `async for` step, not
from the line it has already received. Consuming promptly is therefore what
makes the timeout prompt.

### Handle a slow command

Pass `timeout` to `run()` or `run_sync()` and catch `TimeoutExpired`, which
subclasses the built-in `TimeoutError`. `ExecutionContext.cancel_grace` sets
how long Cuprum waits after asking the child to terminate before killing it.
Output captured before the deadline stays available on the exception.

The same `timeout` argument exists on `lines()` and on `Pipeline.run()`. It
limits the command as a whole rather than each stage separately, and it keeps
running after the child exits: a descendant still holding the pipes counts
against it. See [Timeouts](#timeouts) for the exact bound on how far a run can
overshoot.

<!-- tested-example: slow-command -->

```python
import sys

from cuprum import ExecutionContext, Program, ProgramCatalogue, TimeoutExpired, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="slow")
python = sh.make(Program(sys.executable), catalogue=catalogue)
slow = python("-c", "import time; time.sleep(30)")

try:
    slow.run_sync(timeout=0.5, context=ExecutionContext(cancel_grace=0.2))
except TimeoutExpired as exc:
    assert exc.timeout == 0.5
    assert exc.output == "" and exc.stderr == ""
else:
    raise AssertionError("the command should have timed out")
```

[Timeouts](#timeouts) in the reference covers scope-level defaults, pipeline
deadlines, and the log records each expiry writes.

### Hold a running child and decide when to stop it

`start()` spawns the child and hands back an owned `RunningCommand` instead of
awaiting it. Await the handle's `wait()` to get the outcome, read `result` once
the handle has settled, or stop the child with `terminate()` — `SIGTERM`, the
cancel grace, then `SIGKILL` if it is still there — or `kill()`, which sends
`SIGKILL` without asking. The `async with` block below is what guarantees
teardown; leaving it stops a child still running.

<!-- tested-example: hold-a-handle -->

```python
import asyncio
import sys

from cuprum import ExecutionContext, Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="handle")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python("-c", "import time; time.sleep(30)")


async def stop_a_child() -> int | None:
    async with await command.start(
        context=ExecutionContext(cancel_grace=0.2),
    ) as handle:
        assert handle.pid is not None and handle.result is None
        result = await handle.terminate()
        assert result.exit_code == -15
        return handle.result.exit_code if handle.result is not None else None


assert asyncio.run(stop_a_child()) == -15
```

`wait(timeout=...)` bounds one call: its expiry stops the child the way a
timed-out `run()` does and raises `TimeoutExpired` with whatever the child had
written before it. Because the child is stopped and reaped before that error
arrives, the outcome it produced is recorded as well — the expiry reaches the
wait whose clock ran out and no other, so a later `wait()`, `terminate()`,
`kill()`, or teardown on the same handle reports the exit code instead of
re-raising an error about a child that is already gone. A `timeout` passed to
`start()` is instead the default deadline for every `wait()` on that handle,
because the clock a timeout is measured against starts at the wait that asks
for one. Capture is always on for a handle, so a stopped child still reports
its output on the result. A per-stream `False` value inherits the forced-on
shorthand; an explicit capture policy still controls how that stream is
retained. Because `start()` reads both streams through parent-side pipes, it
raises `ValueError` for redirected stdout or stderr targets even when that
stream's capture field is `False`. Use `run()` or `run_sync()` when redirecting
output.

`terminate(grace=...)` overrides the cancel grace for one call. The example's
exit code of `-15` is the POSIX convention for a child ended by `SIGTERM`; one
that had to be killed reports `-9`.

`signal()` delivers one POSIX signal without settling the run. The two refusals
are separate errors because the remedy differs: `SignalAfterExitError` (a
`ProcessLookupError`) means the child has gone, and `UnsupportedSignalError`
means the platform has no route to a caller-named signal, so `terminate()` or
`kill()` is the only option there. Stopping an already-exited child is not an
error: `terminate()` and `kill()` are no-ops that report the recorded outcome,
so both are safe to call twice or from two places at once.

### Restrict a block of code to specific programs

A scope narrows which catalogued programs may run inside a `with` block.
Running anything else raises `ForbiddenProgramError`, a `PermissionError`
subclass, before a process starts. `current_context().is_allowed()` checks a
program without running it. A `scoped(catalogue=...)` block narrows the
allowlist to the catalogue's programs _and_ supplies that catalogue to
`sh.make()`; a `scoped(ScopeConfig(allowlist=...))` block narrows only the
allowlist and leaves the active catalogue alone. See
[Two enforcement points](#two-enforcement-points).

Each entry derives its policy from the context active at that moment. A scope
manager constructed before a restriction therefore still intersects with that
restriction when entered. Managers may be reused sequentially, nested, or in
different async tasks; every entry derives fresh policy and must exit in
last-in, first-out order in the context where it was entered. An out-of-order
or foreign-context exit raises `RuntimeError` without changing the active
context. Use `allow()` when code needs to widen an active allowlist explicitly.

<!-- tested-example: restricted-scope -->

```python
import sys

from cuprum import (
    ForbiddenProgramError,
    Program,
    ProgramCatalogue,
    ScopeConfig,
    current_context,
    scoped,
    sh,
)

PYTHON = Program(sys.executable)
OTHER = Program("some-other-tool")
catalogue = ProgramCatalogue.from_programs(PYTHON, OTHER, name="policy")
python = sh.make(PYTHON, catalogue=catalogue)

with scoped(ScopeConfig(allowlist=frozenset([OTHER]))):
    assert not current_context().is_allowed(PYTHON)
    try:
        python("-c", "pass").run_sync()
    except ForbiddenProgramError:
        pass
    else:
        raise AssertionError("the scope should forbid the interpreter")
```

### Bind a catalogued program to a specific executable

A `Program` is the catalogue's name for a program, and by default it is also
the string that the operating system executes. It is often convenient for those
to differ: a build may need a pinned absolute path, a virtual-environment
executable, or a controlled replacement during a test. `bind_executable()`
supplies the second without changing the first.

Inside the scope, the logical program runs the bound executable while the
allowlist, project metadata, and telemetry go on carrying the logical name. The
binding is a scope, so nested scopes override only the program they name, and
nothing leaks to a sibling scope, thread, or task. A
[prepared line stream](#stream-lines-as-they-arrive) is bound by the scope that
pulls its first event, not the one that built it. `CommandResult.resolved_path`
reports what actually ran and is `None` when nothing was bound.

A binding never widens the allowlist. Binding an unlisted program and running
it still raises `ForbiddenProgramError`, and the resolver is not called.

The second argument is either a path or a resolver. A path may be a `str` or any
`os.PathLike`, so a `Path`, a `PurePosixPath`, or a `PureWindowsPath` all bind
the same way and are stored normalized. A resolver is a zero-argument callable
returning the executable string; it is stored unchanged and called once per
execution, at spawn time. Any other value — an `int` where a path was meant,
say — is rejected with `TypeError` naming the type supplied, rather than being
stored as a resolver that only fails much later, at spawn time, against a
resolver the caller never supplied.

A relative binding needs a directory to be meaningful, and which directory it
gets depends on the execution. When `ExecutionContext.cwd` is set, the binding
is anchored there and `resolved_path` reports the anchored file. When no
directory is supplied, the binding is passed through unchanged and the platform
resolves it: a bare name is looked up on `PATH`, and a relative path with a
separator is taken as relative to the process's own directory. The bound
directory is also the one the event reports, so `ExecEvent.cwd` (and its
`cuprum_cwd` and `cuprum.cwd` projections) carries the directory the caller
configured, normalized as a `Path`, not an anchored form. Prefer an absolute
binding when the same file must run regardless of where the execution starts.

A resolver must return a `str`. Returning anything else is rejected, because
`None` is reserved to mean "no binding"; a resolver returning it silently runs
the catalogued name instead of the executable the caller selected.

Which exception reports that rejection depends on the boundary. Calling
`resolve_binding(binding, cwd=...)` from `cuprum.executable_binding` directly
raises `TypeError` naming the type the resolver returned. Running a command or
pipeline that reaches the same resolver raises `ExecutableResolutionError`
instead, with that `TypeError` chained as `__cause__`. At the execution
boundary, whatever a resolver raises or returns wrongly is reported as
`ExecutableResolutionError`, which names the logical program and chains the
resolver's own exception as its `__cause__`. Import it from
`cuprum.executable_binding`. It subclasses `RuntimeError`, so
`except FileNotFoundError` around the call no longer catches a resolver that
failed to find its virtual environment, and `except TypeError` does not catch a
resolver that returned the wrong type.

<!-- tested-example: executable-bindings -->

```python
import sys

from cuprum import Program, ProgramCatalogue, ScopeConfig, bind_executable, scoped, sh

PYTHON = Program("python")
OTHER = Program("some-other-tool")
catalogue = ProgramCatalogue.from_programs(PYTHON, OTHER, name="bindings")
python = sh.make(PYTHON, catalogue=catalogue)

with (
    scoped(ScopeConfig(allowlist=frozenset([PYTHON]))),
    bind_executable(PYTHON, sys.executable),
):
    result = python("-c", "import sys; sys.stdout.write(sys.executable)").run_sync()
    assert result.program == PYTHON, "the logical identity is preserved"
    assert result.resolved_path == sys.executable, "the bound path was executed"
    assert (result.stdout or "").strip() == sys.executable, "the child agrees"
```

Path validation is advisory, not a guarantee. Construction rejects an empty
path, a relative path unless `allow_relative=True`, and a path containing NUL or
`..` segments, raising `InvalidExecutableBindingError` (a `ValueError`, from
`cuprum.executable_binding`) that names the program and the path. The file may
still be replaced between that check and the `exec`. Keeping the bound path
unwritable to the executing user, and read-only where binaries must not change,
is the operator's responsibility; see
[§5.1.2 of the design document](cuprum-design.md#512-executable-bindings).

### Choose how a child environment is composed

`EnvMode.OVERLAY` is the default environment policy. It resolves values against
the live `os.environ` when a subprocess is spawned, rather than against an
import-time or scope-entry snapshot. Variables added after a scope starts (the
common `monkeypatch.setenv` case under pytest) remain visible to its children.

`EnvMode.OVERLAY` and `EnvMode.INHERIT` compose identically: each keeps the
mode selected by an outer scope while layering its values over the inherited
overlay. `EnvMode.REPLACE` alone creates a boundary: it starts from an empty
environment, applies only its mapping, and discards every outer overlay. Use the
`UNSET` singleton as a value to remove a variable from a composed child
environment.

<!-- tested-example: env-modes -->

```python
import os
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.context import EnvMode, UNSET, env

catalogue = ProgramCatalogue.from_programs(sys.executable, name="env-modes")
python = sh.make(Program(sys.executable), catalogue=catalogue)
show = "-c", "import os; print(os.getenv('CUPRUM_DEMO'), os.getenv('CUPRUM_KEEP'))"

os.environ["CUPRUM_KEEP"] = "kept"
with env({"CUPRUM_DEMO": "overlaid"}, CUPRUM_DEMO="won"):
    assert python(*show).run_sync().stdout == "won kept\n"
with env({"CUPRUM_DEMO": UNSET, "CUPRUM_KEEP": UNSET}):
    assert python(*show).run_sync().stdout == "None None\n"
with env({"CUPRUM_DEMO": "only"}, mode=EnvMode.REPLACE):
    assert python(*show).run_sync().stdout == "only None\n"
```

The last case shows a replacement policy discarding the inherited
`CUPRUM_KEEP`, while `os.environ` itself is never mutated.

Precedence, from lowest to highest, is:

1. The current process's `os.environ`, read at spawn time.
2. Ambient scoped overlays, with inner values winning. A nested `REPLACE`
   discards every outer overlay and the live parent environment.
3. The per-call `ExecutionContext.env` mapping. Its values win overall; a
   per-call `REPLACE` also discards the ambient policy.

`env()` accepts both positional mappings and keyword arguments, mirroring
`dict(...)`, including `UNSET` values. The function returns an
`EnvRegistration` handle which can be used as a context manager or detached
manually. `ExecutionContext.cwd` remains a per-call setting, independent of
`env_mode`. On POSIX, executable lookup uses the child environment's `PATH` and
the supplied `cwd`; a replacement policy that omits `PATH` can therefore make a
bare executable name fail to resolve. Prefer an absolute program path when
using a deliberately minimal replacement environment.

### Run code around every command

`before(hook)` receives each command before it starts, and `after(hook)`
receives the command and its `CommandResult` once it finishes. Before hooks run
in registration order and after hooks in reverse order, so an outer scope's
setup runs first and its cleanup last. `logging_hook()` pairs the two to write
start and exit records through the standard `logging` module.

<!-- tested-example: before-and-after-hooks -->

```python
import logging
import sys

from cuprum import Program, ProgramCatalogue, after, before, logging_hook, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="hooks")
python = sh.make(Program(sys.executable), catalogue=catalogue)
seen = []


def note_start(cmd):
    seen.append(("before", cmd.argv[-1]))


def note_finish(cmd, result):
    seen.append(("after", result.exit_code))


logger = logging.getLogger("myapp.commands")
with before(note_start), after(note_finish), logging_hook(logger=logger):
    python("-c", "pass").run_sync()
assert seen == [("before", "pass"), ("after", 0)]
```

An exception from a before or after hook propagates to the caller, so keep
hooks that must never affect a run defensive. The start record that
`logging_hook()` writes includes the argument vector; its exit record holds
output lengths, never the output itself. To keep a secret out of that start
record, see [Keep secrets out of telemetry](#keep-secrets-out-of-telemetry).

### Log, measure, and trace runs

The adapters in `cuprum.adapters` turn lifecycle events into structured log
records, metrics, and trace spans. Each is an observe hook, so register it with
`sh.observe()`. `InMemoryMetrics` and `InMemoryTracer` are reference backends;
production code implements the `MetricsCollector` protocol, or the `Tracer` and
`Span` protocols, over its telemetry library.

<!-- tested-example: telemetry-adapters -->

```python
import logging
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.adapters.logging_adapter import structured_logging_hook
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook

catalogue = ProgramCatalogue.from_programs(sys.executable, name="telemetry")
python = sh.make(Program(sys.executable), catalogue=catalogue)
metrics = InMemoryMetrics()
tracer = InMemoryTracer()
logger = logging.getLogger("myapp.telemetry")

with (
    sh.observe(structured_logging_hook(logger=logger)),
    sh.observe(MetricsHook(metrics)),
    sh.observe(TracingHook(tracer)),
):
    python("-c", "print('traced')").run_sync()

assert metrics.counters["cuprum_executions_total"] == 1
assert [span.name for span in tracer.spans] and tracer.spans[0].ended
```

The structured log record carries the execution's effective environment policy
as `cuprum_env_mode` (`inherit`, `overlay`, or `replace`), alongside the other
correlation fields. The value is projected from the typed `ExecEvent.env_mode`
field, which only Cuprum sets, so no caller input can forge it. The tag of the
same name is reserved for the same reason: a caller-supplied `env_mode` tag is
stripped when the observation tags are built, and a replacement run's trusted
value is grafted in its place.

It also carries the run's capture mode as `cuprum_capture_mode` (`text` or
`bytes`), projected from the typed `ExecEvent.capture_mode` field under the
same trust rule. The mode names how the run reports its captures, never their
contents, so the extra holds no output, argument vector, or other unbounded
value. Without it a byte-exact run and a text run of the same command are
identical in every record and counter, and a timeout or teardown failure cannot
be attributed to the mode that hit it.

#### Keep secrets out of telemetry

The adapters export what a run looked like, and that can include secrets:
structured logging and tracing record the full argument vector, and events can
carry environment values, output lines, notes, and tag values. Passing a secret
in a file, rather than as an argument or environment value, keeps it out of
these records. Where that is not possible, a `RedactionPolicy` from
`cuprum.redaction` masks the text it is told about before it is exported:

<!-- tested-example: telemetry-redaction -->

```python
import logging
import re
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.adapters.logging_adapter import structured_logging_hook
from cuprum.adapters.tracing_adapter import InMemoryTracer, tracing_hook
from cuprum.redaction import RedactionPolicy, redacting_hook

catalogue = ProgramCatalogue.from_programs(sys.executable, name="redaction")
python = sh.make(Program(sys.executable), catalogue=catalogue)
policy = RedactionPolicy(
    literals=("token-value",),
    patterns=(re.compile(r"session-[0-9]+"),),
)
tracer = InMemoryTracer()
exports = []
logger = logging.getLogger("myapp.telemetry")
with (
    sh.observe(structured_logging_hook(logger=logger, redaction=policy)),
    sh.observe(tracing_hook(tracer, redaction=policy)),
    sh.observe(redacting_hook(exports.append, policy)),
):
    result = python(
        "-c", "import sys; print(sys.argv[1])", "token-value session-314"
    ).run_sync()

assert result.stdout == "token-value session-314\n"
assert exports and tracer.spans
assert "token-value" not in repr(exports) + repr(tracer.spans)
assert "session-314" not in repr(exports) + repr(tracer.spans)
```

How a policy behaves:

- **It masks only what it is given.** Rules are literal strings
  (`literals=`) and compiled regular expressions (`patterns=`); the policy does
  not guess at secrets. Each match becomes the mask, `[REDACTED]` by default,
  or the `mask=` text. Overlapping matches become a single mask.
- **It is opt-in at each destination.** Pass `redaction=policy` to
  `structured_logging_hook()`, `tracing_hook()` or `TracingHook`,
  `logging_hook()`, and `GitHubActionsSink`, and wrap any other observer as
  `redacting_hook(observer, policy)`. A destination without a policy exports
  text unchanged, so configure every destination that leaves the process.
- **It changes exports, not the run.** The command's arguments and
  environment, the captured result, and events seen by other observers are
  untouched.
- **It covers free text.** Argument items, environment values, output lines,
  notes, and top-level string tag values are redacted. The program name,
  project, paths, tag keys, and numeric and enum fields are not.
- **It fails closed.** If a rule fails, `RedactionError` is raised and nothing
  is exported in raw form. Like any observe-hook failure, it fails the run.

Metrics need no policy: their labels never carry arguments, environment values,
output, or tags. [Metrics adapter](#metrics-adapter) and
[Tracing adapter](#tracing-adapter) list every metric and span attribute.

### Other observation channels

`sh.observe()` reports each execution's lifecycle. A few narrower facts travel
on channels of their own, so that a dashboard can count them without parsing
logs and without every lifecycle hook having to understand them. Each channel
is registered the same way: call its `observe_*` function with a hook, and use
the returned registration as a context manager or call its `detach()` method.
Registrations are context-local, like the policy registrations in
[Apply a policy](#apply-a-policy).

| Channel               | Register with                    | Reports                                                                                                                                             | Ready-made metrics hook                                             |
| --------------------- | -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Execution lifecycle   | `sh.observe(hook)`               | An `ExecEvent` per phase; see [Structured execution events](#structured-execution-events)                                                           | `MetricsHook` in `cuprum.adapters.metrics_adapter`                  |
| Control operations    | `observe_control(hook)`          | A `ControlEvent` per `signal()`, `terminate()`, or `kill()` on a `RunningCommand`                                                                   | `control_metrics_hook` in `cuprum.adapters.control_metrics`         |
| Capture outcomes      | `observe_capture(hook)`          | A `CaptureEvent` when a bound discards bytes or a spill file moves or fails; see [Bounded capture](#bounded-capture)                                | Not applicable                                                      |
| Echo fallbacks        | `observe_echo(hook)`             | An `EchoEvent` when echo truncates a line, a sink fails, or bounded delivery drops or abandons output                                               | `EchoMetricsHook` in `cuprum.adapters.echo_metrics`                 |
| Line-stream lifecycle | `observe_line_stream(hook)`      | A `LineStreamEvent` as a `SafeCmd.lines()` stream spawns, saturates, tears down, or ends                                                            | `line_stream_metrics_hook` in `cuprum.adapters.line_stream_metrics` |
| Stream operations     | `observe_stream_operation(hook)` | A `StreamOperationEvent` per completed drain or transfer; see [Aggregate Python stream-operation events](#aggregate-python-stream-operation-events) | `stream_operation_metrics_hook` in `cuprum.adapters.stream_metrics` |
| Rust-pump routing     | `observe_pump(hook)`             | A `PumpEvent` per routing decision; see [Counting pump routing decisions](#counting-pump-routing-decisions)                                         | `PumpMetricsHook` in `cuprum.adapters.pump_metrics`                 |
| Rust-pump hop spans   | `observe_pump_span(tracer)`      | A span per native hop; see [Rust-pump executor-hop spans](#rust-pump-executor-hop-spans)                                                            | Not applicable                                                      |

_Table 5: Observation channels and their metrics adapters._

Echo and stream-operation hooks are synchronous and should return `None`.
Cuprum closes a returned native coroutine without awaiting it; it does not
await, cancel, or close other awaitables or objects. Other non-`None` returns
retain their existing warning behaviour. An ordinary `Exception` raised while
closing a coroutine is logged at `WARNING` and absorbed, so later hooks still
receive the event. The warning actions are `echo_observer_disposal_failed` and
`stream_operation_observer_disposal_failed`. `SystemExit`, `KeyboardInterrupt`,
and `asyncio.CancelledError` raised during closure propagate unchanged.

`EchoMetricsHook` counts bounded-delivery drops, sink write or flush failures,
and undelivered groups in `cuprum_echo_delivery_events_total`, labelled only by
the output stream and closed event category. It also adds dropped and
undelivered source bytes to `cuprum_echo_delivery_bytes_total` with the same
labels. The adapter never uses sink metadata or output content as a metric
label.

The `observe_*` functions are importable from `cuprum`, except
`observe_capture` and `observe_stream_operation`. They live in
`cuprum.capture_observation` and `cuprum.stream_observation`, respectively. For
example, a supervisor can record how each stop request went:

<!-- tested-example: control-observation -->

```python
import asyncio
import sys

from cuprum import ExecutionContext, Program, ProgramCatalogue, observe_control, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="control")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python("-c", "import time; time.sleep(30)")
events = []


async def stop_a_child() -> None:
    async with await command.start(
        context=ExecutionContext(cancel_grace=0.2),
    ) as handle:
        await handle.terminate()


with observe_control(events.append):
    asyncio.run(stop_a_child())
assert [(event.operation, event.outcome) for event in events] == [
    ("terminate", "delivered"),
]
```

An outcome of `escalated` means the child ignored `SIGTERM` and had to be
killed; `refused_exited` and `refused_unsupported` mirror the two `signal()`
refusals described under
[Hold a running child](#hold-a-running-child-and-decide-when-to-stop-it).

Only `sh.observe()` hooks can fail a run. Every other channel is fail-open: a
hook that raises an ordinary exception is logged at `WARNING` with its
traceback, and the operation it was observing carries on. Their events carry
only bounded values — no output, argument vectors, or exception text — so the
metrics hooks can label series without unbounded cardinality.

### Present output in GitHub Actions

On GitHub Actions, Cuprum can frame a run's echoed output in a collapsible log
group and turn a failed run into an `::error::` annotation. Both are
presentation only: capture, exit codes, and the returned result are unchanged.
The framing activates only when the parent process runs on GitHub Actions
(`GITHUB_ACTIONS` is `true`); elsewhere, a run behaves as if it were absent.

#### Group and annotate flags

The `group` and `annotate_failure` flags on `RunOutputOptions` are the
shorthand for the common case. They build a `GitHubActionsSink` and store it as
the options' sink, so shared options can carry them safely:

<!-- tested-example: group-and-annotate-flags -->

```python
import dataclasses
import io
import sys

from cuprum import Program, ProgramCatalogue, RunOutputOptions, sh
from cuprum.sinks import GitHubActionsSink

catalogue = ProgramCatalogue.from_programs(sys.executable, name="ci-flags")
python = sh.make(Program(sys.executable), catalogue=catalogue)

shared = RunOutputOptions(echo=True, group=True, annotate_failure=True)
assert isinstance(shared.sink, GitHubActionsSink)
result = python("-c", "print('building')").run_sync(output=shared)
assert result.ok and result.stdout == "building\n"

# An explicit sink wins, so shared flagged options never displace it.
explicit = GitHubActionsSink(io.StringIO(), force=True)
assert dataclasses.replace(shared, sink=explicit).sink is explicit

try:
    RunOutputOptions(group="yes")
except ValueError:
    pass
else:
    raise AssertionError("a non-bool flag should be rejected")
```

The flags are independent, and both default to `False`:

- `group=True` frames the run in a `::group::` / `::endgroup::` pair and
  protects echoed output with a stop-commands lease, so child output cannot
  inject workflow commands. A single command's group title defaults to its
  bounded run label, `"<project>: <program>"`; a pipeline receives one group
  titled `pipeline` for the whole run.
- `annotate_failure=True` writes one `::error::` annotation when the run ends
  in a non-zero exit, a timeout, or an error. On its own it frames no group and
  takes no lease: echoed output keeps its usual destinations, and child output
  keeps its usual ability to emit workflow commands.

Workflow commands go to the parent's stderr. An explicit `sink=` takes
precedence and makes both flags no-ops, which is what makes the flags safe in
shared options. Pass the sink on a new `RunOutputOptions` object or through
`dataclasses.replace(shared, sink=...)`; `run()` has no separate `sink`
argument. When `dataclasses.replace` changes a flag on flag-built options, the
sink is rebuilt to match, while an explicitly supplied sink is left alone.

The flags carry no `force` and are validated: anything but a `bool` raises
`ValueError`. The default sink does not serialize overlapping sessions, so run
grouped commands sequentially when they share the parent's stderr; otherwise
their workflow frames can interleave.
[ADR-013](adr-013-opt-in-github-actions-presentation-sink.md) records why the
flags construct the sink rather than teach the execution layer workflow
commands.

#### Configure the sink directly

Use `GitHubActionsSink` from `cuprum.sinks` for anything the flags do not
cover: a custom `title`, another `destination`, `force=True` to reproduce the
framing locally, `emit_group=False` / `emit_annotation=False` to switch off
either half of the frame, or `redaction=policy` to mask group and annotation
titles (see [Keep secrets out of telemetry](#keep-secrets-out-of-telemetry)).
The flags have no redaction setting, and redaction never alters the mirrored
child output. The default single-command title is the bounded run label
`"<project>: <program>"`; an explicit `title` takes precedence over
`argv_title=True`. Pipelines continue to use `pipeline` by default.

The group header is written before the child starts, regardless of `echo` or
`capture`. `argv_title=True` writes the full program and every argument to the
job log, joined with plain spaces, without shell quoting. Arguments may contain
secrets, so enable this only when that disclosure is intentional. A
caller-provided `title`, project name and program name also enter workflow
logs. Workflow-command escaping protects the command syntax; it does not redact
values. See the
[structured logging argv warning](#keep-secrets-out-of-telemetry) for the same
disclosure concern in telemetry records.

<!-- tested-example: github-actions-sink -->

```python
import io
import sys

from cuprum import Program, ProgramCatalogue, RunOutputOptions, sh
from cuprum.sinks import GitHubActionsSink

catalogue = ProgramCatalogue.from_programs(sys.executable, name="ci")
python = sh.make(Program(sys.executable), catalogue=catalogue)
workflow_log = io.StringIO()
sink = GitHubActionsSink(workflow_log, title="Build", force=True)
result = python("-c", "print('building')").run_sync(
    output=RunOutputOptions(echo=True, sink=sink),
)
assert result.ok and result.stdout == "building\n"
assert workflow_log.getvalue().startswith("::group::Build")
```

To use command arguments as a group title, opt in explicitly:

<!-- tested-example: github-actions-argv-title -->

```python
import io
import sys

from cuprum import Program, ProgramCatalogue, RunOutputOptions, sh
from cuprum.sinks import GitHubActionsSink

catalogue = ProgramCatalogue.from_programs(sys.executable, name="ci-argv")
python = sh.make(Program(sys.executable), catalogue=catalogue)
workflow_log = io.StringIO()
sink = GitHubActionsSink(workflow_log, argv_title=True, force=True)
command = python("-c", "print('building')", "--target", "release")
result = command.run_sync(output=RunOutputOptions(echo=False, sink=sink))
assert result.ok
assert workflow_log.getvalue().splitlines()[0] == (
    f"::group::{' '.join(command.argv_with_program)}"
)
```

That title includes every argument, including credentials if they are present.
Caller-provided titles and the project and program names in the default label
also enter workflow logs. Escaping does not redact them; configure
`redaction=policy` or use safe values. Structured logging and tracing have the
same argv disclosure risk; see
[Keep secrets out of telemetry](#keep-secrets-out-of-telemetry).

#### Keep blocking writes off the run loop

Sink writes and flushes are synchronous by default. A slow destination can
therefore delay stream reads, timeouts, and cancellation. Select
`BoundedDelivery` to move echoed output writes and flushes to a shared worker
for the run:

<!-- tested-example: bounded-delivery-sink -->

```python
import io
import sys

from cuprum import (
    BoundedDelivery,
    Program,
    ProgramCatalogue,
    RunOutputOptions,
    sh,
)
from cuprum.sinks import GitHubActionsSink

catalogue = ProgramCatalogue.from_programs(sys.executable, name="bounded-delivery")
python = sh.make(Program(sys.executable), catalogue=catalogue)
workflow_log = io.StringIO()
sink = GitHubActionsSink(workflow_log, title="Build", force=True)

result = python("-c", "print('building')").run_sync(
    output=RunOutputOptions(
        echo=True,
        sink=sink,
        delivery=BoundedDelivery(
            max_queued_bytes=256 * 1024,
            drain_grace_s=0.5,
        ),
    ),
)
assert result.ok and result.stdout == "building\n"
assert workflow_log.getvalue().startswith("::group::Build")
```

When `delivery` is omitted, the current synchronous behaviour is unchanged. The
queue drops and reports a chunk that cannot fit as `DROPPED`; capture and the
command's exit status are unaffected. A worker `write` or `flush` exception
disables further delivery and is reported as `DELIVERY_ERROR`, with the
affected stream identified and no exception text included. The stdout and
stderr chunks from a command or pipeline share one FIFO in the order the run
loop enqueues them. Any queued output discarded after a worker failure is also
reported as `UNDELIVERED`.

The same guarantees apply to `SafeCmd.lines()`: delivery setup is deferred
until iteration begins, and its worker closes before the final result is
published or during stream cleanup. A stream refused by the consuming scope
closes the worker it prepared before the denial propagates, so a denial cannot
leave the delivery thread parked.

At shutdown, Cuprum lets the worker drain for 0.25 seconds by default. Set
`drain_grace_s` to a finite, non-negative number to choose another bound. If a
write is still blocked when that grace expires, Cuprum abandons the worker as a
daemon thread and reports queued and in-flight bytes as `UNDELIVERED`. Python
cannot forcibly stop a running worker thread, so it may remain alive until the
destination returns. The run does not close caller-owned destinations; keep a
destination valid if an abandoned write might later return.

When a presentation sink is active, its closing frame is serialized behind the
delivery worker's last active write and its close runs off the execution loop.
The run waits only for the configured delivery grace. If a destination write
never returns, the closing frame may remain unfinished rather than race that
write; any background finalizer remains a daemon thread.

## Operational reference

These contracts support diagnosis and integration after the first run. They
retain the exact event and configuration names used by telemetry.

### Timeouts

Use the `timeout` parameter on `run()` / `run_sync()` to enforce a wall-clock
limit in seconds. Timeouts are opt-in; when left as `None` no limit is
enforced. When a timeout expires, Cuprum terminates the subprocess, waits for
`cancel_grace`, forcibly kills it if needed (`SIGKILL` on POSIX,
`TerminateProcess` on Windows), and raises `TimeoutExpired` (mirroring
`subprocess.TimeoutExpired`).

Any output already captured before the timeout fired is preserved on the
exception: `exc.output` / `exc.stderr` hold the partial stdout/stderr (or
`None` when `capture=False`), so callers can inspect what the command produced
before it was killed.

Under `capture=True` those attributes are always strings, never `None`: a
stream that produced nothing before the deadline reports the empty string.
`TimeoutExpired.output` and `.stderr` remain `str | bytes | None`: they may be
partial, are `None` when capture was disabled, and are bytes for byte-exact
runs.

Once the process has died, Cuprum gives its readers a brief, fixed window to
observe end of file (EOF). A reader that is still waiting when the window
closes is cancelled, but keeps whatever it had already read. This happens when
a grandchild process inherited the pipe and holds it open; the window is fixed
because teardown must never wait on a pipe that may never close.

A timeout also covers reading the child's streams, not only waiting for it to
exit. A child that has exited still counts against the deadline if something
else keeps the run's pipes open — a descendant that inherited them, which is
the same situation
[process-group ownership](#reclaim-a-descendant-with-process-group-ownership)
addresses. In that case the timeout expires as normal, settling the process
group rather than merely giving up on the readers, so the descendant is
reclaimed and the run reports `TimeoutExpired` just as it would have if the
child itself had overrun.

This holds for all three ways of running a command: `run()` / `run_sync()`,
`lines()`, and `Pipeline.run()`. In every case, the deadline is a single budget
for the whole run. Whatever the wait for the child leaves unused is what the
collection that follows gets, so a run whose child exited promptly still ends
at the configured deadline if its readers cannot finish.

A pipeline starts that budget when output collection begins, so the time spent
spawning its stages is not deducted from it. The deadline is therefore measured
from slightly later than the moment `Pipeline.run()` was called, while a direct
run or `lines()` measures it from before the child was spawned.

A run can exceed the deadline by the cleanup allowances and nothing else:

- `ExecutionContext.cancel_grace` (0.5 s by default) — the wait after asking a
  process to terminate before it is killed.
- The 0.25 s end-of-file window above, once per capturing run whose readers
  have not already finished.
- Up to 1.25 s to settle a stdin writer, for a run given `stdin` data: the
  0.5 s pipe close, two 0.25 s producer-finalization windows, and one 0.25 s
  scheduling grace.

Any run with `ProcessGroupPolicy.OWN_GROUP` — a command, `lines()`, or a
pipeline — adds a bounded settlement wait of up to one second after it forcibly
kills the group because a descendant adopted by an init process can take that
long to disappear. Without a timeout configured (`timeout=None`), there is no
run deadline for any of these bounds to be measured against and no expiry to
report. A direct run's post-exit wait for its stdin writer is still bounded, by
the shared settle window rather than by a deadline, because an unbounded wait
there would hang on a descendant that inherited the run's stdin and never reads
it; every other route behaves as it did before.

With `lines()`, a caller that stops consuming is the thing that can keep a run
open: the line queue is bounded, so a lagging consumer parks the reader, and
the run is still inside its deadline while that happens. That backpressure
counts against the deadline like any other post-exit delay. When the deadline
does expire, lines already yielded to the caller are not withdrawn — a
delivered line stays delivered, and the `TimeoutExpired` reports whatever else
had been captured.

#### Where a timeout comes from

Cuprum uses the first of these that is set:

1. An explicit `timeout` argument on `run()` / `run_sync()` / `lines()`.
2. `ExecutionContext.timeout`.
3. A `ScopeConfig(timeout=...)` default from the enclosing `scoped()` block.

For `lines()` the third term is the scope in force when the _first event is
pulled_, not the scope the stream was built in. An explicit timeout — including
`0`, which expires immediately — still wins, then `ExecutionContext.timeout`;
leaving the argument as `None` falls through to the consuming scope rather than
disabling timeouts. Only a stream that _inherits_ its deadline is bounded by
the consuming scope: an explicit timeout, such as `lines(timeout=60)` iterated
under `ScopeConfig(timeout=1)`, still wins and may exceed that scope's deadline.

`ScopeConfig(timeout=...)` and `CuprumContext(timeout=...)` validate their
timeouts when constructed. Values must be finite and non-negative; `None` means
no timeout, and numeric values are stored as `float`. Negative values, `NaN`,
and positive or negative infinity raise `ValueError`, as does a value too large
to convert to `float`.

A scope-level default applies to every command run inside it. The expired
command's partial output is still available on the exception:

<!-- tested-example: scope-timeout -->

```python
import sys

from cuprum import Program, ProgramCatalogue, ScopeConfig, TimeoutExpired, scoped, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="timeouts")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python("-c", "import time; print('started', flush=True); time.sleep(30)")

with scoped(ScopeConfig(timeout=1.0)):
    try:
        command.run_sync()
    except TimeoutExpired as exc:
        assert exc.timeout == 1.0
        # A slow start can expire before the child prints anything.
        assert exc.output in {"", "started\n"}
    else:
        raise AssertionError("the scope timeout should have expired")
```

#### Non-positive timeouts

A `timeout` of `0` or a negative value is treated as an already-elapsed
deadline, so the command expires immediately, without waiting for the process
to exit on its own. Cuprum terminates the running process (or every pipeline
stage), waits for it to exit — honouring `cancel_grace` and force-killing it if
needed — drains the stream consumers, and then raises `TimeoutExpired`.

#### Pipeline timeouts

A pipeline enforces its deadline once for the whole run; partial output follows
the same capture rules as a successful run. Expiry is reported per stage: each
stage gets a `timeout` event, a `cuprum.timeout` record with its `pid`, and one
`cuprum_timeouts_total` increment. The fields and `timeout_mode` values match
the single-command case.

The deadline covers collecting each stage's output as well as waiting for the
stages to exit. A pipeline takes no `stdin`, so the stdin-writer settlement
allowance does not apply to it, but the same end-of-file window does: a stage
that exits while a descendant keeps its stderr or the final stage's stdout open
does not extend the run indefinitely. On expiry, stages are terminated first —
including an owned group whose leader has already exited — so readers can still
capture what their stage wrote before they are settled. Output captured that
way is preserved on the `TimeoutExpired`, and, for
`ProcessGroupPolicy.OWN_GROUP`, a descendant still in the owned group is
reclaimed exactly as it is for a single command. Reclamation reaches the group,
not the family tree: a descendant that has left the group is beyond it, as the
bounded guarantee below explains.

#### Reclaim a descendant with process-group ownership

When a command whose deadline expires has spawned descendants of its own,
terminating the direct child does not terminate them. Cuprum then gives the run
a brief window to observe end of file, cancels whatever is still waiting when
it closes, and raises `TimeoutExpired` — correct, but the work the timeout was
meant to stop continues in the background, holding the pipe open.

`ExecutionContext.process_group` addresses that. It defaults to
`ProcessGroupPolicy.INHERIT`, which tears down the direct child only; set it to
`ProcessGroupPolicy.OWN_GROUP` to have the child lead a new session and process
group, so teardown can signal the whole group and every descendant still in it:

<!-- tested-example: process-group-ownership -->

```python
import sys

from cuprum import ExecutionContext, ProcessGroupPolicy, Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="process-group")
command = sh.make(Program(sys.executable), catalogue=catalogue)(
    "-c", "import os, sys; print(os.getpgrp() == os.getpid())"
)
owned = ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP)
assert owned.process_group is ProcessGroupPolicy.OWN_GROUP
assert command.run_sync(context=owned).stdout == "True\n"
assert ExecutionContext().process_group is ProcessGroupPolicy.INHERIT
```

That last assertion is the compatibility promise: a context that says nothing
about the policy keeps the child in the caller's group, so no existing run is
affected. Only a caller that names `OWN_GROUP` gets a new group.

Under `OWN_GROUP` the child's process-group identifier equals its PID, because
the spawn made it a session and process-group leader. Teardown signals exactly
that group — never the caller's — so an unrelated process or an outer group is
untouched. The two-phase grace is unchanged: `SIGTERM`, wait for
`cancel_grace`, then `SIGKILL` on the same group. A descendant that ignores
`SIGTERM` and keeps a pipe open is therefore reached by the escalation, and the
run settles instead of waiting out the EOF window.

For an owning run, settlement is decided by the group rather than by the direct
child. A run whose child has already exited — even one already reaped — is
still torn down, and the group is signalled again, for as long as the group
holds a member. That is what reaches a descendant the child left behind before
teardown began: the child's exit alone settles nothing while its group outlives
it. Under `INHERIT`, a completed direct child settles the run and no group
signal is involved at all.

Session leadership also detaches the child from the controlling terminal, and
that trade-off is worth stating because it is not obvious from the name.
`INHERIT` leaves the child in the caller's session, so a terminal interrupt
(`SIGINT`) or `SIGHUP` reaches the child and its descendants; under `OWN_GROUP`
they do not arrive from the terminal at all, and the run is reachable only
through Cuprum's own teardown. An abrupt parent death can therefore leave the
group running where `INHERIT` would have let the terminal end it. A child that
needs a terminal — one that opens `/dev/tty`, or that prompts — cannot get one.

The guarantee is bounded, and the bound is a property of the kernel rather than
of this option: a descendant that calls `setsid()` or `setpgid()` leaves the
group deliberately, and no non-privileged mechanism can contain it. Ancestors
and siblings are outside the group for the same reason. Containment of
processes that actively escape needs a supervisor, a cgroup, or a container; a
command runner cannot promise it.

`OWN_GROUP` raises `ValueError` at spawn on Windows, where POSIX process groups
do not exist. The nearest equivalent, a Job Object, cannot be assigned
atomically with process creation, so containment there could begin only after
the child had a chance to spawn a descendant. Cuprum refuses rather than
accepting the option and delivering no containment. `INHERIT` works everywhere
and remains the default on every platform.

**Do not signal a process group the run does not lead.** Under `INHERIT` the
child stays in the caller's group, and the documented limitation applies in the
other direction too: signalling that group reaches the caller's own process and
its siblings. Ownership is what makes the group signal safe, which is why it is
not the default.

#### Cleanup diagnostics

A teardown reports the outcomes it cannot resolve, so a run that left something
behind is visible rather than silent. The available records depend on the
policy: `cuprum.teardown_signal_failed` is raised under either policy, because
a direct child can refuse a signal too, while the two `process_group` records
can only come from a run that owns its group. Each record goes to the
`cuprum._group_teardown` logger at `WARNING`:

- `cuprum.teardown_signal_failed` — a signal the kernel refused for a target
  the run still believed was running. It names the signal, so a refused
  `SIGTERM` (a process never asked to leave) is distinguishable from a refused
  `SIGKILL` (a process asked and unmoved). A target that had already gone is
  not a failure and is not recorded. This one is recorded for any teardown,
  owned or not: a direct child can refuse a signal too.
- `cuprum.process_group_settlement_timeout` — the bounded wait after the
  escalation expired with group members still outstanding. Recorded only for a
  run that owns its group, since only an owned group can be left outstanding.
- `cuprum.process_group_unsignalable` — the group holds members this process
  may not signal, so it was reported unreachable rather than settled. Also
  recorded only for a run that owns its group.

A refused signal therefore reads as `cuprum.teardown_signal_failed`, and the
message reports `pid=… signal=… owns_group=… error=…` — for example
`pid=4319 signal=9 owns_group=True`. These values are part of the message text
rather than separate attributes on the log record; the `group_teardown` event
described below carries them as fields. The trailing clause states the
consequence: the target was not signalled, so a process the run believes is
running may have outlived its teardown.

The records propagate to the root logger, so an application that configures
logging receives them with no further setup; with no handler configured,
Python's last-resort handler still prints them to standard error.

Each record is also published as a `group_teardown` observe event when the run
has an observation to report against, so an adapter can count a failed teardown
rather than leave it in a log. The event carries a bounded outcome category,
not the record's prose — the metrics adapter turns that into
`cuprum_group_teardowns_total{outcome=…}`, plus a `cuprum_group_settle_seconds`
observation on the settlement timeout alone, which is the only outcome that
measured a wait. A run that reaches teardown with no execution to report
against — a spawn failure, or a run already abandoned — keeps the log record
alone; see
[Observability of group teardown](developers-guide.md#observability-of-group-teardown)
in the developers' guide for why.

#### Timeout diagnostics

Every expiry writes a structured `WARNING` record to the `cuprum.timeout`
logger, even when no observe hook is registered. The
`subprocess_timeout_expired pid=… timeout=… mode=…` record carries:

- `cuprum_operation`: `"wait"`
- `cuprum_pid`: the subprocess process ID (PID), or `None` when no process was
  spawned
- `cuprum_timeout_s`: the configured timeout in seconds
- `cuprum_timeout_mode`: `"elapsed_deadline"` or `"non_positive_immediate"`
- `cuprum_error_type`: `"TimeoutError"`

If cleanup fails to drain a stream consumer, an `ERROR`
`subprocess_teardown_drain_failed pid=… errors=…` record carries
`cuprum_operation` (`"drain"`), `cuprum_teardown_outcome` (`"drain_error"`),
`cuprum_pid`, and `cuprum_error_type` with the comma-joined failure classes.

The drain failure is absorbed so it cannot displace `TimeoutExpired` or
`CancelledError`. Logging is best-effort; the same values are emitted as
`timeout` and `teardown_error` events, so the two channels cannot disagree.

### Structured execution events

For richer observability, register an observe hook with `sh.observe()`. Observe
hooks receive `ExecEvent` values describing:

- `plan` — intent to execute the program (argv/cwd/env resolved).

- `start` — subprocess spawned (pid available).

- `stdout` / `stderr` — decoded output emitted as lines.

- `exit` — subprocess finished (exit code and duration).

- `settled` — the single definitive terminal outcome for every execution
  whose `plan` event began observation. It carries `terminal_outcome` with one
  of `exit_zero`, `exit_nonzero`, `timeout`, `cancelled`, or `error`, plus
  `pid` when available, `exec_id`, and a real `exit_code` only when supplied
  from an actual child exit. It never carries exception details. Spawn failure
  and cancellation therefore settle without an invented exit code.

- `stdin` — bytes were written to the child's stdin; `byte_count` records how
  many. `StdinInput` emits one for its payload. `StdinStream` emits one per
  chunk that produced bytes, and possibly a final one for text the encoder was
  still holding when the producer finished.

- `stdin_error` — writing or closing the child's stdin failed, typically
  because the child stopped reading early, as `head` does. `operation` names
  the failing step (`write`, `close`, or `early_close`), and execution
  continues.

- `stdio_error` — a standard-stream step failed: the stdin producer, an
  invalid chunk, the encoder, opening a `StdioTarget.path()` file, or flushing
  a borrowed file object. `operation` names the step (`produce`, `write`,
  `open`, or `flush`), `error_type` the exception class, and `error_category`
  which boundary failed. The event accompanies the exception the caller
  receives, such as `StdinSourceError` or `OSError`; it never replaces it, and
  a failing observe hook cannot mask it. No `exit` event follows, but `settled`
  does.

- `timeout` — the run exceeded its deadline (ancillary; emitted before the
  preserved `exit` event and the public `TimeoutExpired`).

- `teardown_error` — a stream consumer drained with an unexpected error during
  cleanup (ancillary; the error is absorbed to preserve the primary exception).

- `capture_eof_grace_expired` — a capturing drain exhausted its fixed EOF grace
  budget while one or more readers remained pending (ancillary).

- `group_teardown` — a teardown outcome Cuprum could not resolve, as listed
  under [Cleanup diagnostics](#cleanup-diagnostics) (ancillary). It carries
  `owns_group`, the bounded `group_outcome` (`signal_refused`,
  `settlement_timeout`, or `unsignalable`), and `settle_s` for a settlement
  timeout.

- `pipeline_fail_fast` — a pipeline is being torn down early because a
  non-final stage was the first to fail. Emitted at most once per pipeline,
  before every other still-running stage — upstream producers and downstream
  consumers alike — is terminated, and carrying `stage_index`, `stage_count`,
  `exit_code`, and `duration_s` for the failing stage. It is a decision rather
  than a lifecycle phase: the failing stage's own `exit` event still follows,
  and no stage's events are skipped or reordered because of it. Read
  `stage_index` and `stage_count` from these typed fields rather than from the
  event's tags: `ExecutionContext.tags` are merged last, so a caller that sets
  its own `pipeline_stage_index` or `pipeline_stages` tag shadows the tag
  copies, whereas the typed fields always report the stage the pipeline
  actually acted on.

Line events omit their line terminators. A carriage-return and line-feed (CRLF)
pair split across internal reads is held until both bytes arrive, so it emits
one line event and never an empty event for the boundary. A line that never
receives a terminator is published in fragments of at most `max_line_chars`
characters, so a very long line yields several events rather than one; the
fragments join back to the child's exact text.

Hooks can be used for structured logging, metrics, or tracing without coupling
Cuprum to a specific telemetry library.

The `timeout` event carries `operation` (`"wait"`), `pid`, `timeout_s`,
`error_type` (`"TimeoutError"`), and `timeout_mode`: `"elapsed_deadline"` for a
positive elapsed deadline or `"non_positive_immediate"` for a non-positive
deadline that expires without awaiting the process. The `teardown_error` event
carries `operation` (`"drain"`), `pid`, and `error_type`, the comma-joined
classes raised while cancelling and draining stream consumers. It fires at most
once per execution and excludes expected `CancelledError` failures.

The `capture_eof_grace_expired` event carries the execution's `exec_id` and
`pid`, `operation` (`"drain"`), `eof_grace_s`, and `pending_readers` (one or
two). It is emitted only when a capturing drain reaches the fixed grace limit;
the corresponding `cuprum_capture_eof_grace_expired_total` metric uses only the
`program` and `project` labels, and tracing adds a
`cuprum.capture_eof_grace_expired` event to the matching span. The grace-expiry
event itself carries no captured stdout or stderr payload.

Catalogue lookup failures (`UnknownProgramError`) and allowlist rejection
happen before an `exec_id` is minted. They emit neither `plan` nor `settled`
and are outside the observed-execution contract. A timeout retains its
`timeout` and `exit` events before `settled`; spawn failure and external
cancellation can settle without an `exit` event.

These ancillary events preserve the public outcome: `start` and `exit` are
unchanged, and synchronous hook failures handling `timeout`, `teardown_error`,
or `capture_eof_grace_expired` are suppressed rather than masking
`TimeoutExpired` or `CancelledError`. Terminal-hook failures are also
suppressed while preserving an existing timeout, cancellation, or other run
error.

Each event carries a stable `exec_id` correlation token: every lifecycle event
for one execution — a single command, or one stage of a pipeline — shares the
same token. Hooks that track per-execution state should correlate by `exec_id`
rather than `pid`, which the operating system can recycle across executions.
Events with `exec_id=None` cannot be correlated, so correlation-consuming hooks
(such as the tracing adapter) drop them.

Every event also carries `env_mode`: the effective environment policy for the
execution, once the active context and any per-call policy have been composed.
It is one of `EnvMode.INHERIT`, `EnvMode.OVERLAY`, or `EnvMode.REPLACE`, and it
is present on every phase — including `plan`, `pipeline_fail_fast`, and the
ancillary `timeout`, `teardown_error`, `capture_eof_grace_expired`, and
`group_teardown` events — because it is known before the child is spawned and
describes the whole execution rather than one measurement. The value is `None`
only on legacy or manually constructed events; the execution paths always
resolve a mode.

`capture_mode` travels the same way and for the same reason. It is
`CaptureMode.TEXT` for a decoded run and `CaptureMode.BYTES` for one started
with `run_bytes()` or `run_bytes_sync()`, and every phase of an execution
carries the mode that execution resolved. A byte-exact run and a text run of
the same command otherwise emit the same counters and span attributes, so
without the field a timeout, teardown failure, or terminal failure could not be
attributed to the mode that hit it. Like `env_mode` it is a trusted field on
the typed event rather than an observation tag, because a caller may shadow any
tag key; `None` appears only on legacy or hand-built events.

Awaitable hook results are scheduled as `asyncio.Task` instances and awaited
before the run completes.

Each output line produces a fresh `ExecEvent` with a fresh timestamp. A run
with no observe hook and no `on_line` callback creates no line callback at all,
so observing lines costs nothing until something asks for them.

#### Aggregate Python stream-operation events

For opt-in aggregate telemetry from the pure-Python stream paths, register a
hook with `cuprum.stream_observation.observe_stream_operation`:

<!-- tested-example: stream-operation-metrics -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics
from cuprum.adapters.stream_metrics import stream_operation_metrics_hook
from cuprum.stream_observation import observe_stream_operation

catalogue = ProgramCatalogue.from_programs(sys.executable, name="stream-metrics")
python = sh.make(Program(sys.executable), catalogue=catalogue)
metrics = InMemoryMetrics()
with observe_stream_operation(stream_operation_metrics_hook(metrics)):
    python("-c", "print('hello')").run_sync()
assert metrics.counters["cuprum_stream_operation_bytes_total"] > 0
```

One `StreamOperationEvent` is emitted when each completed stream drain or
pipeline transfer finishes. It reports aggregate `bytes_consumed`, completed
`read_operations` (including EOF), and monotonic `duration_s`. Its closed
`operation` values are `stream_drain` and `pipeline_transfer`; its closed
`outcome` values are `eof`, `cancelled`, `failed`, `downstream_closed`, and
`post_close_drain_timeout`. No event is emitted per read or per chunk, and no
payload is included.

Observer failures are best-effort and do not alter stream execution. `exec_id`
is present only when an existing pipeline-stage correlation context safely
provides it; direct drains carry `None`.

The optional `stream_operation_metrics_hook` records these metrics, all in the
units named by their metric:

- `cuprum_stream_operation_bytes_total` (bytes counter)
- `cuprum_stream_operation_read_operations_total` (read-operation counter)
- `cuprum_stream_operation_duration_seconds` (seconds histogram)

Metrics use only the bounded `operation` and `outcome` labels. They never label
payload, read size, path, PID, descriptor, command argument, exception text, or
another unbounded value.

#### When an observe hook raises

A failing observe hook normally fails the run. Cuprum logs the failure and
re-raises the hook's _own_ exception type out of `run()` / `run_sync()`. When a
run already has a primary timeout, cancellation, or other error, failures from
terminal telemetry are suppressed so they cannot replace it. The two hook kinds
differ in when a failure surfaces:

- A **synchronous** hook raises inline, at the moment the event is emitted.
  Emission of an ordinary event stops there, so hooks registered after it do
  not receive that event, and the exception surfaces immediately — before the
  subprocess is spawned, if the hook failed on `plan`. For `settled`, Cuprum
  attempts every hook before surfacing the first synchronous failure, allowing
  stateful adapters to release execution state. The raised exception is the
  hook's own; Cuprum's internal wrapper appears only as its `__cause__`.
- An **awaitable** hook raises inside its scheduled task. Every hook still
  receives the event, and the exception surfaces when Cuprum awaits the pending
  tasks before the run returns. When several awaitable hooks fail, only the
  first is raised.

A synchronous hook that raises on `start` needs one extra guarantee, because
the child already exists at that point and no caller has been handed a run for
it. Cuprum terminates and reaps that child before the hook's exception
propagates, releases its stdin, stdout, and stderr pipes, and leaves no writer
or consumer task behind. Any capture spill file that setup had already opened
is removed too, so a failed start never leaves a temporary file behind. The
same reclaim covers a failure in the setup that follows the spawn, such as
creating the stdin writer or starting a stream consumer, so the guarantee holds
for `run()`, `run_sync()`, and every stage of a pipeline. `start()` and
`lines()` reach the same outcome through their own unwind helpers, which reap
the child and settle every task the failed setup had started. When the run owns
its process group (`OWN_GROUP`), the teardown signals that group rather than
the child alone. Pipe release is bounded: a descendant that escaped the group
can hold a pipe end open, so Cuprum waits no longer than its fixed end-of-file
grace before surfacing the original error; see
[ADR-021](adr-021-process-group-ownership.md).

This matters most for hooks that match exhaustively on `ExecEvent.phase` and
reject unknown values. The set of phases can grow between releases, so such a
hook raises on a new phase — and so fails the run — until it grows an arm for
it. Give exhaustive hooks a default arm that ignores phases they do not track.
A hook that must never influence the run should catch its own exceptions.

`ExecHook` is defined in `cuprum.events` and re-exported from `cuprum`. Import
it with `from cuprum import ExecHook` or `from cuprum.events import ExecHook`.
The former `from cuprum.context import ExecHook` path is no longer supported;
update that import without changing the hook implementation.

`ExecutionContext.tags` is merged into each event's `tags` mapping. Cuprum also
adds default tags such as the project name and pipeline stage metadata.

### Metrics adapter

The `metrics_adapter` module provides a Prometheus-style metrics hook that
collects counters and histograms. It uses a protocol class so the backend can
be implemented with any preferred metrics library.

The hook collects:

- `cuprum_executions_total`: Counter incremented on each command start
- `cuprum_terminal_outcomes_total`: Counter labelled by `terminal_outcome`,
  incremented once for each `settled` event
- `cuprum_failures_total`: Counter incremented for every terminal category
  except `exit_zero`
- `cuprum_duration_seconds`: Histogram of durations carried by `settled`
- `cuprum_stdout_lines_total`: Counter of stdout lines emitted
- `cuprum_stderr_lines_total`: Counter of stderr lines emitted
- `cuprum_stdin_bytes_total`: Counter of successful stdin bytes written
- `cuprum_stdin_errors_total`: Counter of stdin writer failures
- `cuprum_stdio_errors_total`: Counter of standard-stream boundary failures,
  labelled by `error_category`
- `cuprum_timeouts_total`: Counter of subprocess timeout expiries
- `cuprum_teardown_errors_total`: Counter of stream-consumer drain failures
  during cleanup
- `cuprum_capture_eof_grace_expired_total`: Counter of capturing drains that
  reach the fixed EOF-grace limit with readers still pending
- `cuprum_pipeline_fail_fast_total`: Counter incremented once per pipeline torn
  down early because a non-final stage was the first to fail
- `cuprum_group_teardowns_total`: Counter incremented for each
  `group_teardown` event, labelled by `outcome`, including direct-child signal
  refusals and owning group teardowns
- `cuprum_group_settle_seconds`: Histogram of how long the capped
  post-`SIGKILL` group wait had run when it expired, observed only on the
  `settlement_timeout` outcome
- `cuprum_resource_usage_measurements_total`: Counter incremented once per
  terminal `exit` event that recorded a `resource_usage_mode`, including
  `unavailable`
- `cuprum_child_max_rss_bytes`: Histogram of the child's maximum resident set
  size (RSS) in bytes, recorded only when the operating system's `wait4` call
  attributes it to that child
- `cuprum_child_user_cpu_seconds`: Histogram of child user CPU seconds,
  observed only where that figure was actually measured
- `cuprum_child_system_cpu_seconds`: Histogram of child system CPU seconds,
  observed only where that figure was actually measured

All metrics carry `program` and `project` labels; missing, empty, or explicit
`None` project tags fall back to `unknown`.

`cuprum_executions_total` and `cuprum_failures_total` additionally carry an
`env_mode` label holding the execution's effective policy — `inherit`,
`overlay`, or `replace` — and a `capture_mode` label holding `text` or `bytes`.
Both are bounded by construction (`EnvMode` and `CaptureMode` are closed enums
resolved by Cuprum, never by caller input), so neither can give the series
unbounded cardinality, and both are applied only to those two metrics: the
per-line stream counters omit them, because a line's environment and capture
mode say nothing that its execution's mode does not already carry.

`cuprum_stdio_errors_total` also carries an `error_category` label naming the
boundary that failed: `producer`, `invalid_chunk`, `encoder`, `pipe`,
`owned_path_open`, or `borrowed_flush`. Use it to tell a failure in the
application's own producer from a pipe fault, since both raise `OSError`. The
values form a closed set, so the label cannot grow without bound, and the
counter carries no path, payload, argument, or error message.

A spawn failure has no `start` or `exit` event, because no child ran, but it
does emit one `settled` event with `terminal_outcome="error"`. The metrics hook
therefore increments `cuprum_terminal_outcomes_total` and
`cuprum_failures_total`; the failure counter carries the execution's `env_mode`
and `capture_mode` labels when those are known. The `pid` and `exit_code`
fields remain absent because there is no child status to report.

The four resource metrics also carry a low-cardinality `resource_usage_mode`
label naming how the measurement was obtained: `wait4_child`,
`aggregate_cpu_delta`, or `unavailable`. The label applies only to those four
because it means nothing for any other metric. The three resource histograms
are observed only where a figure was actually measured, while the counter is
emitted for every mode, including `unavailable`. A platform that measures
nothing therefore stays countable, and distinguishable from a run whose samples
went missing.

`cuprum_timeouts_total` counts both modes; use the `timeout` event's
`timeout_mode`, or the `cuprum.timeout` record, to distinguish an elapsed
deadline from an immediate non-positive expiry.

`cuprum_pipeline_fail_fast_total` does **not** use `exec_id`, stage index, exit
code, command arguments, or paths as labels. `exec_id` is unique per execution
and would give the series unbounded cardinality; the others would multiply
series for no aggregate a dashboard needs. Those fields remain on the
`pipeline_fail_fast` event and on the trace span, where per-incident detail
belongs.

A counter spike therefore cannot be joined to a span through the metric itself.
Use `ExecEvent.exec_id` on the observe event, or `cuprum_exec_id` on the
matching structured log record: both carry the same execution token, which
identifies the stage spans behind the spike.

To integrate with a metrics library, implement the `MetricsCollector` protocol.

### Tracing adapter

The `tracing_adapter` module provides an OpenTelemetry-style tracing hook that
creates spans for command execution. It defines protocol classes, so any
tracing library can back it.

The hook creates spans with these attributes:

- `cuprum.program`: The program being executed
- `cuprum.argv`: Full argument vector; configured text is redacted when the
  tracing hook receives a `RedactionPolicy`
- `cuprum.pid`: Process ID
- `cuprum.cwd`: Working directory (when set)
- `cuprum.exit_code`: Actual child exit code, when an `exit` event occurred
- `cuprum.duration_s`: Duration in seconds, when an `exit` event occurred
- `cuprum.terminal_outcome`: Bounded terminal category set on `settled`
- `cuprum.project`: Project name from tags; when a configured rule redacts the
  `project` tag, tracing uses the trusted event project when available
- `cuprum.resolved_path`: The bound executable that ran, when an
  [executable binding](#bind-a-catalogued-program-to-a-specific-executable)
  applied
- `cuprum.env_mode`: The execution's effective environment policy
  (`inherit`, `overlay`, or `replace`), set when the span starts
- `cuprum.capture_mode`: The run's capture mode (`text` or `bytes`), set when
  the span starts
- `cuprum.pipeline_stage_index`: Pipeline stage index (if applicable)
- `cuprum.pipeline_stages`: Total pipeline stages (when applicable)
- `cuprum.max_rss_bytes`, `cuprum.user_cpu_seconds`,
  `cuprum.system_cpu_seconds`, `cuprum.resource_usage_mode`: Terminal child
  resource figures and the mode naming their source, set from `exit`. The mode
  is carried on every terminal `exit` event — `wait4_child`,
  `aggregate_cpu_delta`, or `unavailable` — while the three figures alone are
  absent rather than null when no measurement applies

Output lines (stdout/stderr) are recorded as span events when
`record_output=True` (the default).

#### Ancillary span events

The `stdin_error`, `stdio_error`, `timeout`, `teardown_error`,
`capture_eof_grace_expired`, and `group_teardown` phases are recorded as
`cuprum.<phase>` events on the execution's open span. They leave the span
neither ended nor marked; `settled` closes it and sets its status from the
terminal category. `timeout` is followed by `exit` and then `settled`, while
spawn failure and cancellation can settle without `exit`.

Ancillary events carry whichever of the `line`, `operation`, `error_type`,
`note`, `timeout_s`, `timeout_mode`, `eof_grace_s`, `pending_readers`,
`owns_group`, `group_outcome`, `settle_s`, and `error_category` fields are set.
An event without a matching open span is dropped. The grace-expiry event
carries no captured stdout or stderr payload.

On a `stdio_error` event, `error_category` reaches the tracing backend as its
plain value, such as `producer`, and tells a producer failure apart from a pipe
fault, as it does on the metrics counter.

#### Correlating events with spans

The hook matches every event to its span by `ExecEvent.exec_id`, a stable token
minted once per execution, or once per stage in a pipeline. It does not use
`pid`, because the operating system can reuse a process ID for a later
execution; `pid` is still recorded as the `cuprum.pid` attribute.

Events emitted by Cuprum always carry an `exec_id`, so ordinary usage is
unaffected. The hook ignores hand-built or legacy events that omit it:

- a `start` event without an `exec_id` creates no span;
- any other event without one is dropped.

A pipeline's `pipeline_fail_fast` event is recorded as a
`cuprum.pipeline_fail_fast` span event on the failing stage's already-open
span, carrying `stage_index`, `stage_count`, `exit_code`, and `duration_s`. It
is `cuprum_exec_id` that joins the record, the event, and that span: the
decision reuses the failing stage's existing execution token rather than
minting one of its own, so the teardown appears in the trace of the stage that
caused it. No separate span is started, and each stage's `settled` event closes
and marks its span.

OpenTelemetry integrations implement the `Tracer` and `Span` protocols.

### Byte-exact runs in telemetry

Every run reports which capture mode it used through its structured telemetry:
the `cuprum_capture_mode` log extra, the `cuprum.capture_mode` span attribute,
and the `capture_mode` label on `cuprum_executions_total` and
`cuprum_failures_total`. The label carries only the mode, `text` or `bytes`,
never the captured payload, so a byte-exact run stays distinguishable from a
text one in dashboards without any captured output entering the metrics series.

A `run_bytes()` call that also asks for `on_line` is refused before the spawn,
so it emits no execution event. The refusal is recorded instead as a warning on
the `cuprum._bytes_run` logger naming the mode that was turned away; without
that record the only trace of a byte-exact run the caller asked for and did not
get would be the exception the caller saw. The warning names the refused
callback no more than the exception does — neither renders the callback object
itself.

### Choosing a stream backend

Most applications should leave the backend on `auto`. The
`CUPRUM_STREAM_BACKEND` environment variable accepts three values:

- `auto` (default): uses the Rust pathway when the native extension is
  installed and falls back to pure Python otherwise. The choice is recorded
  once as a `DEBUG` `resolved stream backend` record on the `cuprum._backend`
  logger, whose `resolved_backend` field names the pathway.
- `python`: forces the pure Python pathway, for example when debugging or
  reproducing an issue from a pure Python installation.
- `rust`: requires the native extension. Pipeline execution raises
  `ImportError` if it is unavailable, rather than silently falling back.

Set `CUPRUM_STREAM_BACKEND` before first backend resolution in the process.
Cuprum resolves the backend once and caches it, so later changes have no effect.

The backend applies only to inter-stage pipeline pumping. The Rust pathway runs
outside the global interpreter lock (GIL) on a dedicated worker pool that
Cuprum owns, independent of the event loop's default executor.

For stdout/stderr capture, Cuprum always uses the Python pathway, so line
callbacks, echo, and custom encodings behave identically on either backend.

Even with `rust` selected, an individual hop falls back to the Python pump when
its descriptors cannot be borrowed safely; on Windows every asyncio
subprocess-pipe hop does so. Fall-backs never change pipeline output. See
[Why a hop fell back to Python](#why-a-hop-fell-back-to-python) to detect them.

Rust acceleration pays off for large, multi-stage pipelines, especially on
Linux, where the pump uses the zero-copy `splice()` system call between pipes.
macOS uses a read and write loop. For small outputs the difference is usually
negligible, because the saving is per chunk and small payloads have few chunks.
Measure a representative workload before standardizing on `rust`: from a source
checkout, `make benchmark-e2e` runs the end-to-end throughput suite, and the
[developers' guide](developers-guide.md#running-the-benchmark-suite) explains
its scenarios.

### Checking the native extension

`is_rust_available()` reports whether the native backend can be used. It returns
`False` on a pure Python installation rather than raising, while other import
failures still surface so that a broken installation is visible:

<!-- tested-example: rust-availability -->

```python
import cuprum

assert isinstance(cuprum.is_rust_available(), bool)
```

The same check runs from a shell:

<!-- shell-example: rust-availability -->

```shell
python -c "import cuprum; print(cuprum.is_rust_available())"
```

Native stream transfers accept a `buffer_size` argument, defaulting to 65536
bytes (64 KiB). Any size from 1 byte to 1 GiB (`1 << 30`) inclusive is
accepted; a value outside that window raises `ValueError`, whether the refusal
comes from the extension or from the Python code in front of it. An integer too
large for the native boundary to represent at all — beyond `i64::MAX`, or below
`i64::MIN` — is a different failure and keeps `OverflowError`, because it never
reaches the size check.

An I/O failure while transferring raises `OSError`. When the failure came from
the operating system, the exception carries its own code — `errno` on POSIX
platforms, `winerror` on Windows — so branch on those attributes rather than on
the message text, which is not a stable interface. Expected broken-pipe
conditions do not surface at all: the pump treats a closed downstream as the
end of the transfer, swallows the condition, and keeps draining the reader so
the upstream process does not block on a full pipe buffer. A broken downstream
is therefore not reported as an error.

### Stream telemetry in profiling reports

The tee hot-path profiling harness writes bounded aggregate stream telemetry
into its JSON artefacts, so a report can be read without instrumenting a
production run. Two keys carry it:

- `stream_telemetry` in each scenario's `worker-result.json`, covering that
  worker run, including every repeat in its repeat loop;
- `stream_telemetry_summary` in a read-size sweep's `read-size-sweep.json`,
  combining the telemetry of every completed sample in the sweep.

Both use the same shape: a `groups` mapping keyed first by the closed operation
(`stream_drain` or `pipeline_transfer`) and then by the closed outcome (`eof`,
`cancelled`, `failed`, `downstream_closed`, or `post_close_drain_timeout`),
plus a `totals` group summed over every group present. Each group carries four
aggregate fields, and nothing else:

| Field              | Type    | Meaning                                                          |
| ------------------ | ------- | ---------------------------------------------------------------- |
| `bytes_consumed`   | `int`   | Bytes returned by completed readers in the group.                |
| `read_operations`  | `int`   | Completed reader calls, including the read that returns EOF.     |
| `operation_count`  | `int`   | Completed operations in the group.                               |
| `duration_seconds` | `float` | Sum of monotonic operation durations, in seconds, for the group. |

_Table 6: Aggregate fields of each `stream_telemetry` group._

The telemetry observes only the pure-Python drain and pipeline-transfer paths. A
`pipeline_transfer` group may therefore be absent when the Rust backend
handles a pipeline hop: the pure-Python path did not run, so an absent group is
not a failure. No per-read or per-chunk telemetry is emitted, and the telemetry
does not change stream read behaviour or read-size and line-ending handling.
These artefacts are profiling output rather than production telemetry; the
opt-in production channel is
[aggregate Python stream-operation events](#aggregate-python-stream-operation-events).

### Rust-pump executor-hop spans

Rust-backed pipelines can expose the executor hop that moves bytes between
stages as an opt-in span. Set `CUPRUM_STREAM_BACKEND=rust` before backend
resolution, then register a `Tracer` with `observe_pump_span` in the context
where the pipeline runs. The registration is context-local and can be used as a
context manager.

`observe_pump_span` is importable from `cuprum`. One span, named
`cuprum.rust_pump_hop`, is opened for each registered tracer and each Rust-pump
hop that is actually scheduled. A fast-path decline creates no hop span. The
span starts immediately before executor scheduling and ends from the completion
callback, after the native worker has settled; cancellation therefore includes
the cleanup drain. The pump's own Rust-side span is not parented to it, because
trace context does not cross PyO3, the layer that binds the Rust extension to
Python.

Hop spans contain only bounded attributes: `cuprum.operation` (currently
`rust_pump`), `cuprum.buffer_size`, and `cuprum.outcome`, whose values are
`succeeded`, `failed`, `cancelled`, or `failed_after_cancel`. Successful hops
also include `cuprum.total_bytes` and receive status `OK`; other outcomes do
not receive a success status. The existing `PumpEvent` channel and
`observe_pump` registrations are unchanged. Ordinary tracer observer failures
are contained so they do not alter pump execution, while control-flow
exceptions continue to propagate.

### Why a hop fell back to Python

Selecting the `rust` backend does not guarantee that every inter-stage hop
takes it. Handing the raw pipe descriptors to the Rust pump can fail in ways
that are not errors: the hop runs on the Python pump instead and produces the
same result, more slowly. Nothing surfaces to the caller, so a deployment that
has quietly stopped taking the fast path looks exactly like one that never had
it.

Each fall-back is recorded at `DEBUG` on the `cuprum._pipeline_streams` logger
with a `cuprum_action` of `rust_pump_declined` and one of these `cuprum_reason`
values:

| `cuprum_reason`             | Meaning                                                                                        |
| --------------------------- | ---------------------------------------------------------------------------------------------- |
| `raw_fd_unavailable`        | at least one asyncio transport exposed no raw descriptor, so there was nothing to hand over    |
| `reader_unresumable`        | the reader transport exposes `pause_reading` but not `resume_reading`, so it was left unpaused |
| `reader_pause_failed`       | the reader transport could not be paused, so asyncio might still consume the descriptor        |
| `blocking_mode_unavailable` | the descriptors could not be switched to the blocking mode the pump requires                   |
| `duplicate_fds_unavailable` | a transport descriptor closed before the worker's copy of it could be made                     |
| `platform_unsupported`      | Windows Proactor pipes use overlapped handles that synchronous Rust I/O cannot safely use      |

_Table 7: Reasons an inter-stage hop declines the Rust pump._

These records sit at `DEBUG` rather than `WARNING` because a fall-back is a
routing decision, not a fault; on platforms where the fast path does not apply,
every hop would otherwise warn. Raise that one logger when investigating
throughput:

<!-- tested-example: decline-logging -->

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("cuprum._pipeline_streams").setLevel(logging.DEBUG)
assert logging.getLogger("cuprum._pipeline_streams").isEnabledFor(logging.DEBUG)
```

`raw_fd_unavailable` on every hop usually means the streams are not real
operating-system pipes. The other reasons mean the descriptors were found but
could not be borrowed safely, which is worth investigating rather than
accepting. On Windows, every asyncio subprocess-pipe hop records
`platform_unsupported` and uses the Python pump.

### A pump failure hidden by cancellation

Cancelling a pipeline while the Rust pump is mid-transfer reports the
cancellation to the caller, not any failure inside the pump. Such a failure is
recorded on the same logger with a `cuprum_action` of
`rust_pump_failed_after_cancel` and the original traceback attached, so it
stays diagnosable. It also sits at `DEBUG`, so the logger adjustment above
reveals it.

### A teardown step that failed and was ignored

Returning descriptors after a native hop is best-effort: an error while closing
a worker-owned duplicate, restoring blocking mode, or resuming the reader is
suppressed rather than raised, because the transfer itself has already settled.
Each suppression records a `DEBUG` event with a `cuprum_action` of
`rust_pump_teardown_failed`, a `cuprum_site` naming the step, and the exception
class and `errno`.

The `resume`, `reader_close`, and `restore_blocking` sites log on
`cuprum._pipeline_stream_fds`; the `writer_close`, `resume_reader`, and
`restore_state` sites log on `cuprum._pipeline_streams`. The record carries
nothing drawn from the transferred data. Any of these records indicates a
teardown problem worth investigating.

### A pump hand-off failed before submission

When Cuprum cannot prepare the worker-owned descriptors, it rolls the hand-off
back and the hop falls back to the Python pump. Each failed step records a
bounded `DEBUG` diagnostic with `cuprum_action="rust_pump_handoff_failed"`,
`cuprum_outcome="failed"`, a `cuprum_phase` naming the step,
`cuprum_error_type` holding the exception class, and `cuprum_errno` when one is
available. The `duplicate_writer` and `executor_submission` phases log on
`cuprum._pipeline_streams`; the `reader_preparation` and
`platform_writer_transfer` phases log on `cuprum._streams_rs`.

A writer that could not be duplicated records the `duplicate_writer` phase,
declines with `duplicate_fds_unavailable`, and counts the
`duplicate_writer_failed` hand-off outcome. Descriptors that could not be
switched to blocking mode write no hand-off record: the hop declines with
`blocking_mode_unavailable`, and `blocking_setup_failed` appears only as the
hand-off outcome counted by the [pump metrics](#pump-metrics).

Two setup failures still reach the caller. A rejected executor submission
reports `executor_submission_rejected` and re-raises after rollback, because
every later hop would be rejected the same way. A failure to duplicate
descriptors that Cuprum already owns means descriptor exhaustion, which a
fall-back cannot route around. Both close the writer transport first, so the
downstream stage exits and the failure reaches the caller promptly instead of
waiting for a deadline.

### Counting pump routing decisions

Aggregating debug logs answers "why did this hop fall back?" one record at a
time. To count the same decisions instead, register a pump observer. It is a
separate channel from `sh.observe`: pump events describe an internal routing
decision rather than a command's lifecycle, so they are not `ExecEvent` values
and never reach an observe hook. An `ExecEvent` consumer registered elsewhere
in the process is unaffected.

The channel counts declines, Rust writer-resource hand-off outcomes,
post-cancellation failures, and native-pump cleanup. A successful hand-off
emits a `handoff` event and increments
`cuprum_rust_pump_handoff_total{outcome="submitted"}` once. The same counter
records each failed hand-off outcome once, so it can show both the number of
hops that were submitted successfully and each terminal hand-off failure.

#### Cleanup after cancellation

When a pipeline is cancelled mid-hop, the native worker keeps ownership of its
descriptors until it settles, and the caller waits for that cleanup. The pump
channel reports each step:

- `cleanup_started` when the caller begins waiting for the native worker;
- `cleanup_completed` when the worker releases descriptor ownership, with the
  monotonic wait in `PumpEvent.duration_s`;
- `cleanup_grace_expired` when the caller stops waiting, with the time spent in
  `PumpEvent.elapsed_s`;
- `cleanup_deferred` when the worker later settles and its completion callback
  finishes the cleanup.

`ExecutionContext(native_pump_cleanup_grace=seconds)` bounds the caller's wait;
the default is 0.5 seconds. When the grace expires, the caller receives its
original `CancelledError` straight away. The worker's descriptors stay
quarantined until its completion callback closes them, and the paused reader
transport is closed at expiry, while its event loop can still run the close.
The native worker pool is independent of `asyncio.run()` shutdown, so the same
bound holds for `run_sync()`, and a callback that completes after the loop has
closed still releases its descriptors.

To place these steps in a trace, register the same `TracingHook` with both
`sh.observe(hook)` and `observe_pump(hook.record_pump_event)`. The hook adds
`cuprum.cleanup_started`, `cuprum.cleanup_completed`,
`cuprum.cleanup_grace_expired`, and `cuprum.cleanup_deferred` events to the
source stage's open span, found through its `ExecId`. Each event carries the
bounded attributes `operation="native_pump_cleanup"` and `outcome`, plus
`duration_s` or `elapsed_s` in monotonic seconds where they apply.

The token is not added as a trace attribute, and no PID, descriptor number,
command argument, or exception text is emitted. An event without a matching
open span is dropped; cleanup tracing neither changes span status nor ends the
span.

#### Cleanup DEBUG records

Cancellation cleanup also emits `DEBUG` records on the
`cuprum._pipeline_streams` logger. Each record has
`cuprum_action="rust_pump_cleanup"` and
`cuprum_operation="native_pump_cleanup"`, and a `cuprum_outcome` naming the
step:

- `cuprum_outcome="started"` when the wait begins;
- `cuprum_outcome="completed"`, with `cuprum_duration_s`, on normal
  completion;
- `cuprum_outcome="grace_expired"`, with `cuprum_elapsed_s`, when the caller
  stops waiting;
- `cuprum_outcome="deferred"` from the eventual completion callback, emitted
  only after the native worker has released descriptor ownership.

#### Pump metrics

`PumpMetricsHook` takes the same `MetricsCollector` protocol as `MetricsHook`,
so one collector can back both channels:

<!-- tested-example: pump-metrics -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook
from cuprum.adapters.pump_metrics import PumpMetricsHook
from cuprum.pump_observation import observe_pump

catalogue = ProgramCatalogue.from_programs(sys.executable, name="pump-metrics")
python = sh.make(Program(sys.executable), catalogue=catalogue)
pipeline = python("-c", "print('hello')") | python(
    "-c", "import sys; print(sys.stdin.read(), end='')"
)
metrics = InMemoryMetrics()
with sh.observe(MetricsHook(metrics)), observe_pump(PumpMetricsHook(metrics)):
    result = pipeline.run_sync()
assert result.ok
assert metrics.counters["cuprum_executions_total"] == 2
# Pump counters appear only when a hop reaches the Rust pump decision.
pump_counters = [name for name in metrics.counters if "_rust_pump_" in name]
assert all(name.startswith("cuprum_rust_pump_") for name in pump_counters)
```

| Metric                                         | Labels    | Incremented when                                                 |
| ---------------------------------------------- | --------- | ---------------------------------------------------------------- |
| `cuprum_rust_pump_declined_total`              | `reason`  | a hop fell back from the Rust pump to the Python pump            |
| `cuprum_rust_pump_failed_after_cancel_total`   | none      | a cancelled hop's Rust worker failure was consumed and recorded  |
| `cuprum_rust_pump_cleanup_total`               | none      | native cleanup completed after cancellation                      |
| `cuprum_rust_pump_cleanup_duration_seconds`    | none      | one monotonic duration was observed for completed native cleanup |
| `cuprum_rust_pump_cleanup_grace_expired_total` | none      | caller-facing cleanup grace expired                              |
| `cuprum_rust_pump_cleanup_deferred_total`      | none      | deferred callback cleanup completed                              |
| `cuprum_rust_pump_handoff_total`               | `outcome` | a Rust writer-resource hand-off outcome was reached              |

_Table 8: Metrics emitted by `PumpMetricsHook`._

The cleanup metrics are emitted only when callers register
`observe_pump(PumpMetricsHook(metrics))`. `cuprum_rust_pump_cleanup_total` is
incremented once for every completed native cleanup, and
`cuprum_rust_pump_cleanup_duration_seconds` records one duration observation
for each normal cleanup. The grace-expiry and deferred-cleanup counters each
increment once for their respective outcome. All cleanup metrics are
unlabelled. The `reason` label on the decline counter retains the bounded
cardinality described below.

The `outcome` label on `cuprum_rust_pump_handoff_total` is closed to exactly
`submitted`, `blocking_setup_failed`, `executor_submission_rejected`,
`native_load_failed`, `buffer_validation_failed`,
`platform_writer_transfer_failed`, `native_io_failed`,
`duplicate_writer_failed`, `reader_preparation_failed`, and
`windows_raw_handle_rejected`. The last value represents the deliberate Windows
raw-handle rejection exposed by the Python shim. `outcome` is the only label on
this counter. Descriptor numbers, Windows handle values, errno values,
exception types, exception messages, and tracebacks never become metric labels.

The `reason` label takes exactly the values in Table 7, plus `unknown`, and
nothing else. Table 7's values are published as the `RustPumpDeclineReason`
enum and `unknown` as `cuprum.adapters.pump_metrics.UNKNOWN_DECLINE_REASON`, so
a dashboard can enumerate the series it will see:

<!-- tested-example: decline-reason-series -->

```python
from cuprum import RustPumpDeclineReason
from cuprum.adapters.pump_metrics import UNKNOWN_DECLINE_REASON

series = [reason.value for reason in RustPumpDeclineReason] + [UNKNOWN_DECLINE_REASON]
assert "platform_unsupported" in series and series[-1] == "unknown"
```

`unknown` is a guard rather than an outcome: it is what a decline whose reason
is not an enum member would be labelled, and no call site produces one. It is
listed because a dashboard filtering on the enum alone would drop such a
decline silently rather than showing it.

`PumpEvent` is a public dataclass and performs no run-time validation, so a
caller can construct one carrying any object as its `reason`. The hook
therefore checks the value against `RustPumpDeclineReason` before labelling and
degrades anything else to `unknown`; a hand-built event cannot widen the label
domain.

Nothing derived from a descriptor, an argument vector, or an exception reaches
a label, so the series count is fixed. A successful hand-off increments the
handoff counter once with `outcome="submitted"`. The `DEBUG` records above are
unchanged — the counters supplement them rather than replacing them.

> [!NOTE]
> A pump hook that raises is reported on the `cuprum.pump_observation` logger
> at `WARNING` with its traceback, and the hop continues. This differs from
> `sh.observe` hooks, whose exceptions fail the command being observed. The
> difference is deliberate: a misconfigured metrics backend must not be able to
> abort a pipe hop that would otherwise have succeeded.
>
> Only `Exception` instances are suppressed. `SystemExit`, `KeyboardInterrupt`,
> and `asyncio.CancelledError` propagate unchanged, because some emission sites
> run during cancellation unwinding, where a hook must not absorb the
> cancellation the caller asked for. Hooks must be synchronous; one that returns
> an awaitable is reported and its result discarded.

### Build prerequisites for native extensions

Building the optional Rust extension from source, for example on a platform
without a native wheel, needs:

- **Rust 1.85 or later**, including `cargo`, because the crates use
  `edition = "2024"`. Install the toolchain with [rustup](https://rustup.rs/),
  which provides `cargo` as well.
- **maturin**, the Rust-to-Python build bridge. The project pins
  `maturin==1.15.0`.

From a source checkout, run `maturin develop` inside the target Python
environment to compile the extension and install it there, then confirm it with
`is_rust_available()` as shown in
[Checking the native extension](#checking-the-native-extension). Pure Python
wheels need no Rust toolchain. Contributors should use `make develop` instead;
the [developers' guide](developers-guide.md#building-the-native-extension)
covers that workflow and distributable wheel builds.

### Troubleshooting the native extension

#### No native wheel for this platform

Pre-built native wheels are published for glibc-based Linux (x86_64, aarch64),
macOS (x86_64, arm64), and Windows (x86_64). Each targets the CPython stable
ABI, so one wheel serves CPython 3.12 and every later version. On other
platforms, and on free-threaded CPython builds, `pip install cuprum` installs
the pure Python wheel, which provides the same functionality without Rust
acceleration. To add acceleration anyway, build the extension as described in
[Build prerequisites for native extensions](#build-prerequisites-for-native-extensions).

#### A forced Rust backend raises `ImportError`

With `CUPRUM_STREAM_BACKEND=rust`, pipeline execution raises `ImportError` when
the extension is not installed, instead of silently using the fallback. Check
the installation with `is_rust_available()`, or set `CUPRUM_STREAM_BACKEND` to
`auto` to fall back to the Python pathway automatically.

#### Hops use the Python fallback on Windows

Windows native wheels remain useful even though every pipeline hop there uses
the Python pump: the restriction protects the event loop's overlapped pipe
handles, and `CUPRUM_STREAM_BACKEND=rust` still runs those hops correctly
through the fallback. On other platforms, a hop that falls back records its
reason as described in
[Why a hop fell back to Python](#why-a-hop-fell-back-to-python).

#### Benchmark results show little difference

Small payloads show little difference between the backends, because the
overhead the Rust pump avoids is per chunk. `splice()` acceleration is
Linux-only. Benchmark comparisons report speed-up as `python_mean / rust_mean`,
so values above `1.0x` mean Rust was faster. The
[developers' guide](developers-guide.md#reading-benchmark-results) explains how
to read the full benchmark output.

## Glossary

- **Allowlist:** the set of programs a context permits to run, checked when a
  command runs. The default context permits every program; a scope narrows it.
  The catalogue separately controls which programs can be built into commands.
- **Builder:** the callable that `sh.make()` returns. Calling it with arguments
  produces a `SafeCmd`. Its catalogue is fixed when `sh.make()` is called, so
  the builder still works after the scope that supplied the catalogue exits.
- **Capture:** keeping a child's output on the result as `stdout` and
  `stderr`. On by default; independent of echo.
- **Catalogue:** a `ProgramCatalogue`, the set of programs for which builders
  may be created, grouped into projects with metadata. Checked when a builder
  is created. `sh.make()` uses an explicit `catalogue` argument first, then the
  innermost active `scoped(catalogue=...)`, then `DEFAULT_CATALOGUE`; a caller
  outside any catalogue scope still names its application catalogue.
- **Descriptor:** an operating-system handle for an open pipe or file. The Rust
  pump borrows the descriptors of the pipes it connects.
- **Echo:** mirroring a child's output to the parent's streams, or to the
  sinks an `ExecutionContext` names, as it arrives. Off by default.
- **End of file (EOF):** the signal that a pipe's writer has closed and no more
  data will arrive.
- **Executable binding:** a scoped mapping, made with `bind_executable()`, from
  a catalogued program to the file that actually runs, leaving the program's
  catalogue identity unchanged.
- **Execution ID (`exec_id`):** a token shared by every event from one
  execution, used to correlate events with each other and with trace spans.
- **Global interpreter lock (GIL):** the CPython lock that lets only one thread
  run Python code at a time. The Rust pump moves data without holding it.
- **Hop:** one transfer of data from a pipeline stage's stdout to the next
  stage's stdin.
- **Observe hook:** a callable registered with `sh.observe()` that receives
  every `ExecEvent`.
- **Process ID (PID):** the operating system's number for a running process.
  Numbers are reused, so Cuprum correlates by execution ID instead.
- **Pump:** the component that performs a hop, either in Python or in the
  optional Rust extension.
- **Program:** a `Program`, the typed name of an executable. It is both the
  catalogue's identity for the program and, unless bound, the name the
  operating system runs.
- **Redaction policy:** a `RedactionPolicy`, the literal strings and patterns
  an exporting adapter masks before telemetry leaves the process. Opt-in at
  each destination; it never changes the run itself.
- **Resident set size (RSS):** the memory a process holds in RAM; Cuprum
  reports the child's peak where the platform measures it.
- **Running-command handle:** the `RunningCommand` that `SafeCmd.start()`
  returns, which owns a started child until it is waited for or stopped.
- **SafeCmd:** a built command — a program plus its argument vector — ready to
  run. Builders produce them; `run()`, `run_sync()`, `lines()`, and `start()`
  execute them.
- **Scope:** a `with scoped(...)` block that narrows the allowlist and can add
  hooks, a timeout, or environment overlays for the code inside it. A
  `scoped(catalogue=...)` scope also activates that catalogue for `sh.make()`
  inside the block.
- **Standard-stream target:** a `StdioTarget`, naming where a child's stdout
  or stderr goes: a pipe Cuprum reads, the parent's stream, a file, or a
  descriptor the caller owns.
- **Stdin stream:** a `StdinStream`, which feeds a child's stdin from an async
  producer one chunk at a time.
