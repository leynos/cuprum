# Architectural decision record (ADR) 020: Publish a typed argument contract for `sh.make` builders

## Status

Accepted on 2026-09-26.

## Date

2026-09-26.

## Context and problem statement

`sh.make(program)` returned a value annotated `Callable[..., SafeCmd]`. The
ellipsis erased the accepted argument domain from every static checker that
read the annotation. A call such as `sh.make(ECHO)(object())` type-checked
cleanly, and the runtime did not disagree: `str()` rendered the object's
`repr`, so `<object object at 0x...>` became a real element of a real argv.
Bytes, lists, and user-defined `os.PathLike` objects were admitted the same
way, each arriving on the command line as its `repr` rather than as a value.

The failure mode was therefore silent in both directions. The checker could not
reject an unsupported argument, and the runtime would not either, so the only
place the problem surfaced was the invoked program's own error output, detached
from the call that caused it. `None` was the sole value the runtime rejected,
and that check was hard-coded rather than derived from the annotation.

The published annotation and the implemented behaviour had no shared source of
truth, so even a correct annotation could drift from the validation without
anything failing.

## Decision drivers

- An unsupported argument must be rejected at the call site when a checker
  runs, and at call time when it does not.
- The annotated domain and the validated domain must derive from one
  definition, so they cannot drift apart.
- The published name must be usable by callers annotating their own wrappers
  and test fixtures, without repeating the union or reaching for `object`.
- Rejection must name the offending type rather than render it.
- Existing behaviour for accepted values must not change: `str` conversion,
  boolean serialization, flag normalization, and argument ordering all stay as
  they were.
- Consumers that pass `list[str]` into a builder must keep type-checking
  without edits.

## Considered options

1. **Annotate the return as `Callable[[ArgValue, ...], SafeCmd]`.** This
   states a domain but cannot express per-argument types, and it loses the
   keyword-flag parameter entirely, so `builder(porcelain=True)` stops checking.
2. **Keep `Callable[..., SafeCmd]` and validate only at runtime.** This
   rejects bad values at call time but leaves the call site unchecked, which is
   the defect under repair.
3. **Return a `typing.Protocol` whose `__call__` states both variadic
   parameters**, with a public alias naming the accepted values. (Chosen.)

## Decision outcome

Publish `ArgValue` in `cuprum/sh/argv.py`:

```python
type ArgValue = str | int | float | bool | Path
```

and return a `SafeCmdBuilder` protocol from `make`:

```python
class SafeCmdBuilder(typ.Protocol):
    def __call__(self, *args: ArgValue, **kwargs: ArgValue) -> SafeCmd: ...
```

The protocol lives in `cuprum/sh/builder.py`, which imports `SafeCmd` from
`cuprum/sh/safe_cmd.py`. The reverse import would close a cycle, so `safe_cmd`
must not import `builder` back; the module docstring records this.

The protocol is deliberately not decorated with `typing.runtime_checkable`. It
describes a call signature to static checkers, and an `isinstance` test against
a protocol whose only method is `__call__` would confirm little beyond the
object being callable at all.

`_stringify_arg` derives its runtime type tuple from the alias itself:

```python
_ARG_TYPES = typ.get_args(ArgValue.__value__)
```

so the validation and the published annotation cannot drift. `ArgValue` is a
PEP 695 alias, whose `__value__` carries the union `typing.get_args` can
unpack; calling `typing.get_args` on the alias object directly returns `()`.
This is the single source of truth the "one definition" driver requires.

Values outside the tuple raise `TypeError` naming the offending type, for
example `object is not a valid argv element for sh.make`. `None` keeps its
dedicated pre-existing message, `None is not a valid argv element for sh.make`,
because callers may match on it.

`ArgValue` is exported from both `cuprum` and `cuprum.sh`. `make` and
`build_argv` remain under `cuprum.sh` and are not package-root exports.

### Consequences

The contract is falsifiable: a call passing an unsupported value now fails
`make typecheck`. The checked-in positive fixture
`cuprum/unittests/typing_fixtures/sh_make_valid.py` is walked by that gate as
ordinary source, and `cuprum/unittests/test_sh_typing_contract.py` writes each
rejected call to a temporary directory and requires the pinned `ty` to report
`invalid-argument-type` with a non-zero exit. The negative cases stay outside
the repository-wide check scope deliberately, because a file the checker is
meant to reject cannot be checked in without failing the gate.

`git.py`, `tar.py`, and `rsync.py` required no edits: they pass `list[str]`
into builders, and `str` is a member of `ArgValue`.

### Corrections to earlier records

This ADR corrects two statements in the 2026-09-25 addendum to
[ADR-007](adr-007-subprocess-execution-module-boundaries.md), which are left in
place because accepted ADR text is append-only:

- That addendum lists `_ArgValue` as belonging to `cuprum/sh/argv.py`. The
  alias is now public and named `ArgValue`.
- It lists `SafeCmdBuilder` as belonging to `cuprum/sh/safe_cmd.py`. The
  protocol now lives in `cuprum/sh/builder.py`, for the cycle reason above.

The rest of that addendum's submodule inventory still describes the code.

## Assumptions

- Callers wanting a bare `--check` flag pass it positionally as `"--check"`;
  `check=True` produces `--check=True`. This predates the ADR and is unchanged.
- An arbitrary `os.PathLike[str]` implementation is rejected rather than
  converted. `ArgValue` names the concrete `pathlib.Path`, so only that class
  is accepted. Widening to `os.PathLike` would be a separate decision with its
  own compatibility surface.

## Addendum (2026-10-11): widen `ArgValue` to text path-likes and harden the domain test

The Assumptions section above records that an arbitrary `os.PathLike[str]`
implementation was rejected, and that widening the alias would be a separate
decision. That decision is now taken, in response to review, and the earlier
text is left in place because accepted ADR text is append-only.

Three statements from the original text are superseded:

- The `ArgValue` alias now reads
  `str | int | float | bool | Path | os.PathLike[str]`, and the runtime type
  tuple is reduced to checkable classes with
  `typ.get_origin(member) or member`, because `os.PathLike[str]` is a
  parameterized generic that `issubclass` rejects. The single-source-of-truth
  property is unchanged: the tuple is still derived from `ArgValue` itself.
- A path-like value is converted with `os.fspath()` rather than `str()`. For a
  `pathlib.Path` the two agree, so accepted behaviour is unchanged; for any
  other implementation the protocol's conversion is the correct one.
- The domain check uses `issubclass(type(value), _ARG_TYPES)` rather than
  `isinstance`. `isinstance` consults `__class__`, so `Mock(spec=int)` — and
  any object exposing a `__class__` property — satisfied the check while
  remaining an unsupported type whose `repr` then reached argv. That is the
  original defect this ADR exists to close, so it is closed for proxies too.

Two narrower rules follow from the widening:

- `os.PathLike` is structural, so the runtime cannot enforce the `str`
  parameter from the annotation. The `os.fspath()` result is therefore
  re-checked, and a `__fspath__` returning `bytes` raises `TypeError` with the
  bytes rule's message, naming `bytes` rather than the wrapper class because
  the payload is what would have reached argv.
- Keyword-only calls are unaffected: `None` keeps its dedicated message, and
  the reserved execution-option guard still runs ahead of argument
  serialization.

The user-facing and maintainer accounts move with the alias:
`docs/users-guide.md` states the accepted domain and the `os.fspath()`
conversion, and `docs/developers-guide.md` records the honest runtime check.
Both the static contract and the runtime contract are exercised by the existing
fixtures and tests, with new cases for a custom text path-like, a
byte-returning path-like, and a `__class__`-spoofing proxy.
