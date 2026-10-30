# RFC 0002: Reusable command composition helpers for `sh.make` builders

## Preamble

- **RFC number:** 0002
- **Status:** Proposed
- **Created:** 2026-10-25
- **Author:** raised from the leynos/lading adoption of `0.2.0b1` (lading
  adoption assessment §6.4)

## Problem

`sh.make(program)` returns a builder that turns one call into an argument
vector with exactly two shapes. Each positional value becomes one token, in the
order supplied. Each keyword value becomes `--name=value`, with underscores in
the name turned into hyphens. Everything else a real tool's grammar needs is
handwritten at the call site:

- **A presence flag.** A boolean keyword renders `--check=True`, so ADR-020's
  Assumptions prescribe the literal positional string `"--check"` instead.
  Nothing distinguishes the flag from an ordinary operand; only the caller's
  memory does.
- **An option whose value is a separate token.** `builder("-f", archive)`
  spells the pair by hand, even though the same call site already uses keyword
  serialization for every attached option.
- **A repeated option.** `builder("--include", first, "--include", second)` or
  a comprehension that flattens pairs. Repeating the name is the caller's job,
  and a comprehension that forgets the name produces a bare operand list.
- **A positional separator.** `builder("--", *paths)` places operands behind
  the end-of-options marker. That spelling is only safe when no keyword is
  passed: `build_argv` appends generated keyword flags after every positional
  token, so `builder("--", verbose=True)` produces `("--", "--verbose=True")`
  and the flag arrives as an operand. This was confirmed against the checked
  out tree:

  ```plaintext
  build_argv("--", x=1) == ("--", "--x=1")
  ```

The design below addresses this by making `separated_operands` the sanctioned
way to emit a separator and by rejecting a keyword flag that follows the
separator it emits. A handwritten `"--"` stays accepted, so the guard changes
no existing call shape.

The four shapes are not exotic; they are the common grammar of command-line
tools. Their absence is visible in cuprum's own source, where each typed
builder assembles argv differently:

- `cuprum/builders/rsync.py` keeps an ordered `_FLAG_ORDER` table of
  `(attribute, spelling)` pairs, filters it by `getattr`, and appends the
  separator and both operands.
- `cuprum/builders/tar.py` builds `["-c", compression?, "-f", archive]`,
  appends `"--"`, then extends with validated sources. Its docstring records
  why the separator matters: without it, GNU tar pairs an attacker-supplied
  `--checkpoint=1` source with `--checkpoint-action=exec=...` and runs a
  command with the caller's privileges (issue #538).
- `cuprum/builders/git.py` appends each conditional flag and its value with
  separate statements, because `-B` versus `-b` and the `detach`/`force`
  interaction do not fit a table.

Each is correct, each is tested separately, and none can be reused by an
adopter whose tool is not git, rsync, or tar.

The adopter view is the same one. The lading assessment's Table 2 tells callers
to "preserve existing argument vectors as positional tokens: `builder(*argv)`",
because the generic builder "does not know Cargo's boolean flags, repeated
options, subcommands, or `--` separator rules". Section 6.4 raises that as a
gap and asks for "reusable composition for flags, repeated options, option
values, and positional separators", while keeping tool-specific schemas
optional. Today an adopter either keeps a local assembly helper per tool or
writes the separator and repeat loops inline, with the separator trap one
keyword argument away.

## Current state

`cuprum/sh/argv.py` publishes `ArgValue`, the accepted value domain
(`str | int | float | bool | Path | os.PathLike[str]`), and `build_argv`, which
stringifies positional values through `_stringify_arg`, serializes keywords
through `_serialize_kwargs`, and returns `positional + flags`. The builder
closure in `cuprum/sh/factory.py` rejects keyword names that collide with
execution options, then delegates to `build_argv` and constructs a `SafeCmd`
from the resulting tuple.

`cuprum/sh/builder.py` states the callable contract. `SafeCmdBuilder.__call__`
accepts `*args: ArgValue` and `**kwargs: ArgValue`, so a tuple of `str` is
already a valid argument list; composing argv outside the builder needs no
protocol or signature change.

ADR-020 established that contract and recorded the current flag spelling as an
assumption rather than a decision to revisit. The accepted text says a caller
wanting a bare `--check` passes it positionally. The ADR is append-only, so a
change to that position is recorded as an addendum, not a rewrite.

The positional-separator doctrine already exists. `docs/developers-guide.md`
§"End-of-options separators for positional operands" states the rule for future
builders: every positional, user-supplied operand must follow `--`, or the
builder must reject leading dashes at validation time, as `git_ref` does. A
builder whose only user input is an option value needs neither. The doctrine is
enforced by per-builder tests in `cuprum/unittests/test_builder_library.py`,
`cuprum/unittests/test_builder_property_based.py`, and the real-tool
regressions in `cuprum/unittests/test_builder_operand_separation.py`. No test
covers the generic composition a caller performs before reaching a builder, and
no shared vocabulary exists to perform it with.

## Goals and non-goals

- Goals:
  - Provide one first-party vocabulary for the four argv shapes every builder
    re-implements: presence flags, option values in either spelling, repeated
    options, and separated positional operands.
  - Reuse the published `ArgValue` domain and the builder's own conversion and
    error messages, so a rejected value reads the same wherever it is caught.
  - Make the end-of-options discipline the easy path, and make a
    helper-composed separator followed by keyword flags impossible to express
    silently.
  - Keep tool-specific schemas optional. A caller composes an argv for a tool
    cuprum has never catalogued without writing a builder library.
- Non-goals:
  - No Cargo, git, or other tool DSL, and no knowledge of which spellings a
    tool accepts.
  - No argv string parsing, shell quoting, or `shlex`-style splitting. Cuprum
    passes a sequence to the child and never builds a command-line string.
  - No reordering. Helpers emit values in the order supplied; ordering remains
    part of each builder's published argv contract.
  - No change to `sh.make`, `build_argv`'s signature, or `SafeCmdBuilder`.
  - No replacement for the typed builders in `cuprum.builders`, which add the
    validation the generic helpers deliberately lack.

## Proposed design

### The helper vocabulary

A new module `cuprum/sh/compose.py` holds pure functions that each return
`tuple[str, ...]`. The module is importable as `cuprum.sh.compose`, and its
public names are re-exported from `cuprum.sh`, matching how `argv.py` and
`build_argv` are published today.

```python
# cuprum/sh/compose.py
class OptionSpelling(enum.Enum):
    """How an option's value is attached to the option's name."""

    SEPARATE = "separate"  # ("--output", "dist")
    JOINED = "joined"  # ("--output=dist",)


def flags(*specs: tuple[str, bool]) -> tuple[str, ...]: ...


def option(
    name: str,
    value: ArgValue,
    *,
    spelling: OptionSpelling = OptionSpelling.SEPARATE,
) -> tuple[str, ...]: ...


def repeated_option(
    name: str,
    values: cabc.Iterable[ArgValue],
    *,
    spelling: OptionSpelling = OptionSpelling.SEPARATE,
) -> tuple[str, ...]: ...


def separated_operands(*values: ArgValue) -> tuple[str, ...]: ...
```

| Call                                                                          | Result                                 |
| ----------------------------------------------------------------------------- | -------------------------------------- |
| `flags(("--archive", True), ("--delete", False))`                             | `("--archive",)`                       |
| `flags()`                                                                     | `()`                                   |
| `option("--output", "dist")`                                                  | `("--output", "dist")`                 |
| `option("--output", "dist", spelling=OptionSpelling.JOINED)`                  | `("--output=dist",)`                   |
| `repeated_option("--include", ["a", "b"])`                                    | `("--include", "a", "--include", "b")` |
| `repeated_option("--define", ["a=1", "b=2"], spelling=OptionSpelling.JOINED)` | `("--define=a=1", "--define=b=2")`     |
| `separated_operands("a", "-b")`                                               | `("--", "a", "-b")`                    |
| `separated_operands()`                                                        | `()`                                   |

A builder becomes a single expression:

```python
from cuprum import sh
from cuprum.sh import OptionSpelling, compose


def git_log_argv(
    ref: str,
    *,
    paths: cabc.Sequence[str | Path],
    max_count: int,
    oneline: bool,
) -> tuple[str, ...]:
    return (
        "log",
        *compose.flags(("--oneline", oneline)),
        *compose.option("--max-count", max_count),
        str(git_ref(ref)),
        *compose.separated_operands(*(safe_path(p) for p in paths)),
    )


command = sh.make(GIT)(*git_log_argv(ref, paths=paths, max_count=5, oneline=True))
```

The validated ref stays a plain positional token because `git_ref` already
rejects a leading dash, which is exactly the exception the developers' guide
allows. The paths are user-supplied and unvalidated for dash-leading names, so
they follow the separator.

### Why the helpers return plain tuples

`tuple[str, ...]` splats into the existing callable contract: every element is a
`str`, and `str` is a member of `ArgValue`, so `SafeCmdBuilder.__call__`
accepts the result without an annotation or protocol change. A caller can also
concatenate helper output with a literal prefix, such as a subcommand, using
ordinary tuple syntax.

The helpers call `_stringify_arg` from `cuprum.sh.argv` rather than
re-implementing conversion. That keeps the accepted domain, the `os.fspath`
handling, the rejection of `bytes`, and the dedicated `None` message identical
to the builder's. A value rejected inside a helper is rejected with the same
wording it would have received in a builder call, which matters because callers
match on the `None` message today.

Empty inputs return `()`. A caller composing an optional group therefore needs
no conditional around the helper itself; `flags` already filters disabled
specs, and `repeated_option` and `separated_operands` return nothing for an
empty sequence rather than emitting a dangling name or separator.

### The separator guard

`separated_operands` marks the `--` it emits, and `build_argv` rejects a call
in which keyword flags would follow a marked separator. The marker lives in
`cuprum/sh/argv.py`, which owns the argv domain, so `compose.py` can import it
without a cycle:

```python
# cuprum/sh/argv.py
class _Separator(str):
    """An end-of-options ``--`` emitted by ``compose.separated_operands``."""

    __slots__ = ()


_SEPARATOR = _Separator("--")


def build_argv(*args: ArgValue, **kwargs: ArgValue) -> tuple[str, ...]:
    positional = tuple(_stringify_arg(arg) for arg in args)
    flags = _serialize_kwargs(kwargs)
    if flags and any(isinstance(arg, _Separator) for arg in args):
        msg = (
            "cannot append keyword flags after a '--' separator; "
            "pass them positionally before the separator"
        )
        raise TypeError(msg)
    return positional + flags
```

The guard tests the type of the supplied argument, not the text it renders. A
`str` subclass is copied to a genuine `str` by `str()`, which is how
`_stringify_arg` renders it, so the marker survives only as far as the guard:
`positional` and therefore `SafeCmd.argv` hold ordinary `"--"`. The marker
never reaches the child, an argv comparison in a test, or any consumer that
inspects `SafeCmd`.

Without the guard the composition contract has an unenforced precondition:
`separated_operands` is safe only in a call that passes no keyword. With it, a
call that would silently turn a flag into an operand fails at construction, and
the message names the fix.

An ordinary `"--"` is never treated as a separator. That keeps an option value
distinct from a separator, so `build_argv("-f", "--", check=True)` returns
`("-f", "--", "--check=True")` — the shape a tool consuming `--` as the value of
`-f` expects. It also keeps the helpers composable, because
`option("-f", "--")` uses the default separate spelling and must stay usable
alongside keyword flags.

The corollary is that the guard protects the helper, not a handwritten
separator: `build_argv("--", x=1)` still returns `("--", "--x=1")`. That is
today's behaviour, so nothing regresses, and the documented way to emit a
separator is `separated_operands`, which the guard does cover. A caller who
wants the protection uses the helper.

### What the helpers deliberately do not do

The helpers do not validate tool syntax: `option("--no-such-flag", value)`
builds successfully, exactly as `builder("--no-such-flag=value")` does today.
They do not decide whether a value may begin with `-`; that judgement belongs
to a typed builder such as `safe_path` or `git_ref`, and the separate spelling
exists so an option value is never re-parsed regardless. They do not dedupe: a
repeated presence flag (`-v -v -v`) and a repeated option are both legal, and
order is preserved.

## Requirements

### Functional requirements

- The module provides the four shapes above: presence flags, option values in
  separate or joined spelling, repeated options in either spelling, and
  separated positional operands.
- `flags` filters by its boolean member and preserves spec order. Every spec
  is validated even when disabled, so a misspelled flag cannot hide behind
  `False`.
- `option` and `repeated_option` accept exactly the `ArgValue` domain and
  raise the same `TypeError` wording the builder raises for a rejected value.
- A malformed option name raises `ValueError` naming the spelling. A name must
  start with `-`, must not be exactly `-` or `--`, and must not contain
  whitespace, `=`, or a NUL character.
- `separated_operands` emits `--` exactly once, immediately before the first
  operand, and emits `()` for no operands.
- `build_argv` raises `TypeError` when keyword flags would follow the separator
  `separated_operands` emits. Calls without that separator, and calls with it
  and no keyword, are unchanged. A literal `"--"` is an ordinary value and
  never triggers the guard.

### Technical requirements

- Helpers are pure: no I/O, no context lookup, no module-level mutable state.
- Every helper returns `tuple[str, ...]` and is annotated with `ArgValue` where
  it accepts a value, so the result splats into `SafeCmdBuilder` unchanged.
- The public names are importable from `cuprum.sh` and from
  `cuprum.sh.compose`, and are not package-root exports, matching ADR-020's
  export policy for `make` and `build_argv`.
- Public functions carry NumPy-style docstrings with worked examples; the new
  module stays within the 400-line file limit.
- A property test proves that, for every value the helpers accept, the emitted
  text equals the builder's own conversion, and that no helper emits a
  keyword-style token unless the joined spelling was requested.
- No new dependency.

## Compatibility and migration

Additive, apart from one deliberate rejection. The new module introduces names;
no existing signature, return type, or error message changes. Existing typed
builders keep their current argv, and their unit, property, and real-tool
suites remain the evidence.

The only behaviour change is the `build_argv` guard, and it is narrower than a
value test: only the separator `separated_operands` emits is rejected. A caller
who passed keyword flags together with a helper-composed separator previously
produced a tuple in which those flags were operands; it now raises `TypeError`.
Migration is to spell the flag positionally before the separator, which is what
the caller meant in the first place.

No existing call shape changes. A literal `"--"` is left alone, so
`build_argv("-f", "--", check=True)` is unaffected, and the existing
`test_build_argv_orders_and_normalizes` property keeps its current expectation:
its strategies generate the text `"--"` as an ordinary positional value, which
is not a marker, so the generated flags are still appended in order. The test
plan adds separate cases for the marker instead of weakening that property.

ADR-020 is accepted and append-only. Its Assumptions clause prescribes the
positional `"--check"`, and that text stays as written. Roadmap task 3.5.5 will
append an addendum recording that RFC 0002 supersedes the workaround by making
the flag a first-class helper, in the same way the 2026-10-11 addendum widened
`ArgValue`. No earlier text is edited.

The same task will add a users' guide subsection under "Build arguments
deliberately", point builder authors at the helpers from the scripting
standards, and record the guard in the changelog.

## Alternatives considered

### Option A: An `Argv` accumulator type

A fluent `Argv().flag("--check").option("--output", path)` value would read
well and could enforce ordering internally. It adds a public type, mutable or
persistent state, and a second way to spell what a tuple already expresses. The
in-tree builders already treat argv as a tuple, and the existing contract
accepts a tuple without change. Rejected for the initial delivery; the pure
helpers can later be wrapped by such a type without breaking them.

### Option B: Per-program option schemas

A schema per catalogued program, declaring which options are boolean, repeated,
or value-taking, would let `sh.make` interpret `check=True` correctly. It
requires a schema per tool, makes an unregistered flag a failure or a silent
fallback, and turns catalogue maintenance into CLI-grammar maintenance. The
assessment explicitly keeps tool-specific schemas optional. Rejected.

### Option C: Infer a bare flag from any boolean keyword

Changing `check=True` to emit `--check` would remove the workaround for the
common case. It breaks the documented `ArgValue` contract, silently changes the
argv of every existing caller, and is wrong for tools where `--check=true` is
meaningful. It also does nothing for repeated options or separators. Rejected.

### Option D: Document the patterns and stop

The developers' guide already states the separator rule, and ADR-020 states the
flag workaround. That is what exists today, and adopters still write the same
assembly per tool. Documentation cannot enforce the separator precondition or
share the conversion rules; the helpers can. Rejected as insufficient.

## Implementation steps

1. Add `cuprum/sh/compose.py` with `OptionSpelling`, `flags`, `option`,
   `repeated_option`, and `separated_operands`; re-export them from `cuprum.sh`.
2. Add unit and property tests for the four shapes, the empty inputs, the
   malformed-name rejections, and the shared `ArgValue` conversion.
3. Add the separator marker to `cuprum/sh/argv.py`, have
   `separated_operands` emit it, and add the `build_argv` guard. Its tests
   cover the guarded shape, the error message, the unchanged call shapes, and
   the accepted `build_argv("-f", "--", check=True)` case.
4. Migrate `git_status` and `git_checkout` to `flags`/`option`, and
   `tar_create` to `separated_operands`, leaving every emitted argv identical.
   Leave `rsync_sync`'s `_FLAG_ORDER` table in place unless the helper reads at
   least as clearly there.
5. Document the vocabulary in the users' guide, the scripting standards, and
   the changelog; append the ADR-020 addendum.
6. Add an end-to-end suite that composes a command for each of git, rsync, and
   tar, compares the composed argv with the handwritten builder's, and runs the
   real tool to confirm a leading-dash operand stays an operand.

## Open questions

- Is the `OptionSpelling` enum worth its type surface, or should the joined
  spelling have a second function name (`option_joined`)? The enum avoids a
  boolean mode and covers repetition in one contract; two names are easier to
  discover.
- Should `flags` also accept a single iterable of specs, so an ordered table
  such as rsync's `_FLAG_ORDER` can be passed without a generator splat?
- Should `separated_operands` also accept a single iterable of operands, so a
  caller holding a list need not splat it?
- Should the helpers be re-exported from `cuprum.builders` for in-tree builder
  authors, or is `cuprum.sh` the single home?

## Recommendation

Adopt the four pure helpers and the `OptionSpelling` enum in
`cuprum/sh/compose.py`, add the `build_argv` separator guard, migrate only the
builders where the vocabulary removes hand-rolled assembly, and keep
tool-specific schemas out of scope. The change is additive, reuses the
published argument domain and error contract, and turns the separator rule into
an invariant `separated_operands` enforces.
