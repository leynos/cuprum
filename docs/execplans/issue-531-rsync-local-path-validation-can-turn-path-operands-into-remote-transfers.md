# Stop rsync operands from reaching remote syntax through local-path validation

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: DRAFT -> IN PROGRESS (approved for implementation by the task
instruction that supplied the coding plan).

## Purpose / big picture

`rsync_sync()` in `cuprum/builders/rsync.py` validates its source and
destination with `safe_path()`. That validator was written for ordinary local
filesystem paths: it accepts any string that `PurePath` considers absolute, and
it accepts all non-empty, NUL-free, `..`-free strings when
`allow_relative=True`. Rsync has its own operand grammar in which a colon
separates a remote endpoint from a path: `host:path`, `host::module/path`,
`user@host:path`, `[::1]:path`, and the `rsync://` URL form. Because
`safe_path()` knows nothing about that grammar, a caller-supplied `host:path`
passes validation and is handed to rsync, which then performs a remote transfer.

Two independent routes reach that state.

1. On POSIX, `classify_path_string()` treats a Windows drive prefix
   (`C:/file`, `C:\file`) as absolute even though `PurePath("C:/file")` is a
   relative path there. The value is accepted under the default
   `allow_relative=False` policy, normalized to `C:/file`, and remains a
   relative colon operand for rsync — which may read it as a remote endpoint.
2. With `allow_relative=True`, any non-empty, NUL-free, `..`-free string is
   accepted. `host:path` and `host::module/path` therefore pass straight
   through to the rsync argv.

After this change, `safe_path()` decides absoluteness with host-native rules
only, and the rsync builder applies a private local-only operand check to both
operands after `safe_path()` has validated and normalized them. Remote
spellings are rejected whether or not `allow_relative` is set. The error names
the role (source or destination) when the rsync check is the layer that
rejects; under default options a relative remote spelling can instead fail the
earlier generic check as `PathRejection.NOT_ABSOLUTE`. Ordinary local absolute
and relative paths continue to work exactly as before.

Observable success: the regression tests, now in
`cuprum/unittests/test_rsync_operand_policy.py`, failed on the pre-change tree
and pass afterwards; `rsync_sync()` can never emit an operand that begins with
`rsync://` or contains `:` before its first `/`.

## Constraints

- Do not add a public remote-endpoint API. The task explicitly scopes that out;
  remote transfers need a custom builder. The public exports of
  `cuprum/builders/rsync.py` and `cuprum/builders/__init__.py` stay unchanged.
- Do not change `cuprum/builders/tar.py`, `cuprum/builders/git.py`, or
  `cuprum/builders/__init__.py`.
- Do not change the rsync flag order, the operand order, or the argv shape.
  Validation must finish before `sh.make(RSYNC)` runs.
- Do not add `--` to the rsync argv. The fix is validation, not operand
  disambiguation.
- Keep the `PathRejection` member set and order unchanged. `safe_path()` still
  reports `EMPTY`, `NUL`, `PARENT_SEGMENT`, `NOT_ABSOLUTE`, in that order.
- `allow_relative=True` continues to mean "relative local paths are
  acceptable". It must never mean "remote syntax is acceptable".
- English prose uses en-GB-oxendict spelling. Markdown wraps prose at 80
  columns and code blocks at 120 columns.
- No new third-party dependencies.
- Every code file stays under 400 lines. The touched test modules are close to
  that ceiling, so additions must be sized against the current line count.

## Tolerances (exception triggers)

- Scope: stop and escalate if the fix requires touching more than the files
  named in `Decision log` entry D1.
- Interface: stop and escalate if a public signature, export, or default must
  change beyond the documented behaviour of `safe_path()`.
- Tests: stop and escalate if the new tests still fail after two full fix
  attempts, with the failing logs attached.
- Lint: stop and escalate if a gate demands a suppression that cannot be
  justified in place. Do not lower an enforced surface or raise a threshold.
- Ambiguity: if a legitimate local path must be rejected to satisfy the
  requirement, stop and ask before accepting the regression.

## Risks

- Risk: host-native absoluteness rejects a Windows drive path on POSIX, which a
  user might rely on for cross-host construction. Severity: medium. Likelihood:
  medium. Mitigation: it was never usable as a local path, and the users' guide
  explains the rsync-port spelling (`/cygdrive/c/...`) for callers on Windows,
  alongside the rule that remote transfers need a custom builder.
- Risk: the local-only predicate over-rejects a legitimate local file whose
  name contains a colon. Severity: low. Likelihood: low. Mitigation: the
  predicate only rejects a colon that appears before the first `/`, which is
  precisely rsync's remote-spec position; `dir/host:path` and `/abs/host:path`
  remain accepted and are tested.
- Risk: the property-test strategy generates colon-bearing operands that a
  different layer already rejects, making the new property vacuous. Severity:
  medium. Likelihood: medium. Mitigation: the strategy only builds strings that
  pass `safe_path(allow_relative=True)` before the rsync check, so each example
  reaches the predicate. A non-vacuity witness is recorded in the
  `Verification plan`.

## Progress

- [x] (2026-10-19) Write this ExecPlan.
- [x] (2026-10-19) Red: add failing unit, property, and behaviour tests.
      Evidence: 57 failed, 56 passed, 2 skipped; failures included
      `AttributeError: module 'cuprum.builders.rsync' has no attribute
      '_local_rsync_operand'` and `Failed: DID NOT RAISE`.
- [x] (2026-10-19) Green: host-native `safe_path()` absoluteness.
- [x] (2026-10-19) Green: private rsync local-only operand check.
      Evidence: focused suite 117 passed, 2 skipped.
- [x] (2026-10-19) Update users' guide, developers' guide, and ExecPlan 3.1.1.
- [x] (2026-10-19) Run deterministic gates at the shipped tree. Every gate
      exited 0 with `HEAD=102951f4860a0a0a41e75abbebb8b2b7558f5d6e` recorded
      in its own log. Evidence: `make test` reported 3771 passed, 95 skipped
      (Python) and 167 of 167 passed (Rust nextest); `make markdownlint`
      reported 0 issues in 0 files; `make nixie` validated all diagrams;
      `make duplication` passed with 65 reasoned exceptions. Logs:
      `/tmp/lint-issue-531.out`, `/tmp/typecheck-issue-531.out`,
      `/tmp/test-issue-531.out`, `/tmp/markdownlint-issue-531.out`,
      `/tmp/nixie-issue-531.out`, `/tmp/duplication-issue-531.out`.
- [x] (2026-10-19) Commit, push, and open a draft PR with `Closes #531`.
      Evidence: commit `af8798eaa`, draft PR #649.
- [x] (2026-10-20) Re-run every deterministic gate at the fix commit. All
      gates exited 0 with `HEAD=dca91e3758b82c1db815e1788793fdb6035b30fc`
      recorded in each log. Evidence: `make test` reported 3770 passed, 95
      skipped (Python; one fewer than the first run because a redundant
      drive-prefix destination test was folded into the parametrized matrix)
      and 167 of 167 passed (Rust nextest). Logs:
      `/tmp/check-fmt-issue-531.out`, `/tmp/lint-issue-531.out`,
      `/tmp/typecheck-issue-531.out`, `/tmp/test-issue-531.out`,
      `/tmp/markdownlint-issue-531.out`, `/tmp/nixie-issue-531.out`,
      `/tmp/duplication-issue-531.out`.
- [x] (2026-10-20) CodeRabbit review and concern disposition. First pass
      produced three findings, all addressed: neutral test operands no longer
      assume POSIX absoluteness; the role-message claim is qualified in the
      guides; platform-specific cases are scoped by marker. The rsync operand
      tests now live in `cuprum/unittests/test_rsync_operand_policy.py` so
      `test_builder_library.py` stays under the line cap. Second pass over the
      committed range (`--committed --base main`) reviewed all eleven changed
      files and returned zero findings.

## Surprises & discoveries

- Observation: `classify_path_string()` calls `PurePath(raw_value)` before the
  empty-string check. Evidence: `PurePath("")` is valid and yields `"."`, so
  the empty check still runs first in the rejection order. Impact: none; the
  ordering is preserved when the Windows regex is removed.
- Observation: `cuprum/executable_paths.py` has its own
  `_WINDOWS_ABS_PATTERN` and a host-native check. Evidence: it is a separate
  module with a separate contract for executable bindings. Impact: out of
  scope; only `cuprum/builders/args.py` changes.
- Observation: under the default `allow_relative=False` policy the role-specific
  rsync message is unreachable for the remote spellings, because `safe_path()`
  rejects them first as `NOT_ABSOLUTE`. Evidence: the first green attempt
  asserted the role message for every row and 18 tests failed with
  `SafePath requires an absolute path by default`. Impact: the default-policy
  tests only require `ValueError`; the role-specific assertion is pinned by the
  `allow_relative=True` tests and by a direct private-helper test.
- Observation: Ruff's `DOC` rules require NumPy `Returns`/`Raises` sections on
  private helpers that return or raise. Evidence: `make lint` reported
  `docstring-missing-returns` and `docstring-missing-exception` for the new
  rsync helpers. Impact: both private helpers now carry structured sections.
- Observation: a POSIX absolute path cannot be the neutral operand in a
  cross-platform test. Evidence: `PureWindowsPath("/srv/local").is_absolute()`
  is `False`, so on Windows the counterpart operand would be rejected as
  `NOT_ABSOLUTE` and mask which operand the assertion targets. A Windows drive
  path is worse: `PureWindowsPath("C:/path").is_absolute()` is `True`, yet `C:`
  before any separator is exactly rsync's remote grammar. Impact: neutral
  operands are now the MSYS mount spelling `/cygdrive/c/data` under
  `allow_relative=True`, or a `tmp_path`-derived native path in POSIX-only
  tests.
- Observation: the rsync operand tests pushed
  `cuprum/unittests/test_builder_library.py` to the 400-line ceiling. Evidence:
  the module reached 407 lines mid-edit. Impact: the policy tests were moved to
  a dedicated `test_rsync_operand_policy.py`, which also groups them with the
  contract they pin.

## Decision log

- D1. Files in scope: `cuprum/builders/args.py`, `cuprum/builders/rsync.py`,
  `cuprum/unittests/test_builder_library.py`,
  `cuprum/unittests/test_rsync_operand_policy.py`,
  `cuprum/unittests/test_builder_property_based.py`,
  `tests/features/builder_library.feature`,
  `tests/behaviour/test_builder_library_behaviour.py`, `docs/users-guide.md`,
  `docs/developers-guide.md`, and
  `docs/execplans/3-1-1-core-builder-set-for-common-tools.md`. Rationale: the
  coding plan names exactly these; `tar.py` and `git.py` do not take remote
  operands and are explicitly excluded.
- D2. `safe_path()` uses `PurePath(raw_value).is_absolute()` only. The
  `_WINDOWS_ABS_PATTERN` constant is deleted rather than made conditional.
  Rationale: a conditional would still accept a foreign-host absolute on the
  host whose grammar does not support it; the requirement is host-native
  detection. On Windows, `PurePath("C:/file").is_absolute()` is true, so the
  Windows-only acceptance test still passes.
- D3. The rsync predicate rejects a colon before the first `/` or a
  case-insensitive `rsync://` prefix, and is applied to both the raw string and
  the normalized `SafePath` string. Rationale: normalization could otherwise
  hide or introduce a colon position; checking both makes the sink contract
  independent of which representation a caller supplied.
- D4. The predicate and helper are private (leading underscore) and omitted
  from `__all__`. Rationale: the ticket requires a local-only policy, not a
  remote-endpoint API. Remote transfers remain possible through a custom
  builder.
- D5. Rejection messages name the role and append a fixed hint. The exact
  text is shown below. Rationale: the task's validation bullets require
  role-specific assertions and an actionable hint.

```plaintext
rsync source must be a local path; remote syntax such as host:path, host::module or rsync:// is not supported
```

## Outcomes & retrospective

Both independent routes to the sink are closed. `safe_path()` now decides
absoluteness with `PurePath(...).is_absolute()` for the running host, so on
POSIX `C:/file` is rejected as `NOT_ABSOLUTE` instead of being normalized and
handed to rsync. `_local_rsync_operand()` applies rsync's own remote grammar to
both operands after `safe_path()`, so `host:path`, `host::module/path`,
`user@host:path`, `[::1]:path`, and `rsync://...` are rejected even with
`allow_relative=True`. Legitimate local names with a colon after a separator
(`dir/host:path`) still work.

The validation order matters and is now documented: the generic policy runs
first, so a remote spelling under default options reports the generic
`SafePath` message rather than the rsync-specific one. The role-specific
message is reserved for the layer that actually knows the operand is a remote
endpoint, which is the relative-opt-in path and direct helper calls.

Lesson: the first green attempt asserted the role-specific message for every
default-policy row and failed 18 tests. The fix was to assert each layer's own
contract rather than collapsing both into one expected string.

## Conformance basis

Upstream artefacts:

- Issue #531,
  <https://github.com/leynos/cuprum/issues/531>, finding
  `csf_71386ab83a8e3256ae58c34f`, revision `ebd6a41`.
- Roadmap item 3.1.1 (core builder set) and its ExecPlan
  `docs/execplans/3-1-1-core-builder-set-for-common-tools.md`, whose risk
  register already records that remote rsync targets are out of scope.
- Design document `docs/cuprum-design.md`, §11.3.1, "Core builder library
  decisions".

Trace chain:

```plaintext
issue-531 remote-spec sink
  -> EP-M1 host-native safe_path absoluteness
  -> EP-M2 rsync local-only operand policy
  -> tests::test_safe_path_rejects_windows_drive_paths_on_posix
  -> tests::test_rsync_rejects_remote_operands_when_relative[source|destination]
  -> tests::test_rsync_accepted_operands_are_never_remote (property)
  -> tests::test_rsync_remote_operand_rejection (behaviour)
```

## Verification plan

Obligations, methods, and evidence:

1. Invariant: on POSIX, `safe_path("C:/file")` and `safe_path("C:\\file")`
   raise `ValueError(PathRejection.NOT_ABSOLUTE.value)`. Method: named
   regression test, POSIX-only, skipped on Windows. Non-vacuity: the same test
   asserts a normal relative path is still rejected and a native absolute path
   is accepted, so the skip is scoped, not the assertion.
2. Invariant: on Windows, `safe_path("C:/file")` is accepted. Method: named
   test, Windows-only. Non-vacuity: the test asserts the returned value equals
   the POSIX-normalized spelling.
3. Invariant: for every operand string that begins with `rsync://`
   (case-insensitive) or contains `:` before the first `/`, `rsync_sync()`
   raises `ValueError`, and under `allow_relative=True` that error names the
   role, for source and destination. Method: parameterized unit test over the
   spellings the ticket names, crossed with two roles and two option settings.
   Non-vacuity: the relative-mode rows assert the role word, which is only
   reachable once `safe_path()` has accepted the spelling; the default-mode
   rows use `match="SafePath"` because the generic policy rejects them first. A
   direct test on `_local_rsync_operand` pins the helper's own behaviour
   independently of `_rsync_argv`.
4. Invariant: accepted local operands are emitted exactly as `safe_path()`
   normalizes them. Method: named acceptance test over absolute paths and
   relative `dir/file` and `dir/host:path` with `allow_relative=True`.
   Non-vacuity: `dir/host:path` specifically pins that the predicate does not
   over-reject.
5. Invariant: no emitted operand can start with `rsync://` or contain `:`
   before the first `/`. Method: Hypothesis property over generated
   `<host><: or ::><path>` strings, asserting rejection; plus an invariant
   property over accepted operands asserting the sink condition. Non-vacuity
   evidence (`--hypothesis-show-statistics`): the rejection property ran 200
   passing examples with both separators and both roles; the acceptance
   property ran 200 passing examples mixing rejected and accepted candidates;
   the sink-check witness property ran 100 passing examples, each asserting that
   `safe_path(allow_relative=True)` accepts the generated operand first.
6. Invariant: ordinary local relative paths continue to be accepted with
   `allow_relative=True` and unchanged in the argv. Method: property test over
   colon-free segment lists; the existing
   `test_rsync_sync_rejects_relative_paths_unless_opted_in` keeps its meaning.
7. Invariant: BDD-observable behaviour — a remote operand raises before any
   command is built. Method: a new scenario in
   `tests/features/builder_library.feature` with a step that captures the
   exception and asserts no `SafeCmd` was produced.
8. Axiom: rsync's operand grammar treats a colon before the first path
   separator as a remote specification. Source: `rsync(1)` and the ticket's
   static trace. This is treated as an external contract, not re-derived.
9. Axiom: `PurePath(...).is_absolute()` is host-native on the running
   interpreter. Source: `pathlib` documentation; exercised by both platform
   tests.

Residual gaps: no live rsync is executed, so the tests establish the argv
contract rather than observable remote traffic. The ticket's own reproduction
is static, and the security scan did not run a live exploit.

## Context and orientation

`cuprum/builders/args.py` holds the `SafePath` and `GitRef` validators.
`classify_path_string()` is the single source of truth for rejection categories;
`safe_path()` converts input to a string and delegates.

`cuprum/builders/rsync.py` holds `RsyncOptions`, `_FLAG_ORDER`,
`_rsync_argv()`, and `rsync_sync()`. `_rsync_argv()` is the pure argv builder;
`rsync_sync()` wraps its result with `sh.make(RSYNC)`.

Tests live in three layers: unit
(`cuprum/unittests/test_rsync_operand_policy.py` for the rsync operand policy,
`cuprum/unittests/test_builder_library.py` for the rest of the builder
surface), property (`cuprum/unittests/test_builder_property_based.py`), and
behaviour (`tests/features/builder_library.feature` plus
`tests/behaviour/test_builder_library_behaviour.py`).

## Plan of work

Milestone EP-M1 (Red then Green): host-native absoluteness in `safe_path()`.

- Red: add a POSIX-only test asserting `C:/file` and `C:\file` raise
  `NOT_ABSOLUTE`, and a Windows-only test asserting acceptance.
- Green: delete `_WINDOWS_ABS_PATTERN` and use only
  `PurePath(raw_value).is_absolute()`.
- Acceptance: the new tests pass, the existing tar tests still pass on POSIX.

Milestone EP-M2 (Red then Green): rsync local-only operand policy.

- Red: parameterized rejection and acceptance tests, the property tests, and
  the BDD scenario.
- Green: add the private predicate and `_local_rsync_operand()`; call it for
  source then destination inside `_rsync_argv()` before returning.
- Acceptance: all new tests pass and the existing argv shape tests are
  unchanged.

Milestone EP-M3: documentation.

- Update the users' guide, developers' guide, and ExecPlan 3.1.1 risk note.
- Acceptance: `make markdownlint` and `make spelling` pass.

Milestone EP-M4: publication.

- Run `make check-fmt`, `make lint`, `make typecheck`, `make test`.
- Commit atomically, push, open a draft PR that closes #531.

## Concrete steps

Run every gate from the worktree root with `tee`, sequentially:

```plaintext
set -o pipefail
make check-fmt 2>&1 | tee /tmp/check-fmt-issue-531.out
make lint 2>&1 | tee /tmp/lint-issue-531.out
make typecheck 2>&1 | tee /tmp/typecheck-issue-531.out
make test 2>&1 | tee /tmp/test-issue-531.out
make markdownlint 2>&1 | tee /tmp/markdownlint-issue-531.out
make nixie 2>&1 | tee /tmp/nixie-issue-531.out
```

Focused red/green runs:

```plaintext
uv run pytest -q cuprum/unittests/test_rsync_operand_policy.py \
  cuprum/unittests/test_builder_library.py \
  cuprum/unittests/test_builder_property_based.py \
  tests/behaviour/test_builder_library_behaviour.py
```

## Idempotence and recovery

Every step is additive and safe to rerun. If a gate fails, fix the cited file
and rerun that gate only. To revert a partial edit, use
`git checkout -- <path>` for the named file and reapply the step.

## Artefacts and notes

The regression catalogue is the ticket's Validation list. Each bullet maps to
at least one named test in the `Verification plan`.

## Interfaces and dependencies

Public surface after the change:

```python
def safe_path(value: str | Path, *, allow_relative: bool = False) -> SafePath
def rsync_sync(
    source: str | Path,
    destination: str | Path,
    *,
    options: RsyncOptions | None = None,
) -> SafeCmd
class RsyncOptions: archive, delete, dry_run, verbose, compress, allow_relative
```

Private additions in `cuprum/builders/rsync.py`:

```python
def _is_remote_rsync_operand(operand: str) -> bool
def _local_rsync_operand(
    value: str | Path, *, role: str, allow_relative: bool
) -> SafePath
```

No new dependencies.

## Revision note

- 2026-10-19: Initial draft, written at the start of implementation from the
  ticket text and the supplied coding plan.
- 2026-10-19: Recorded the red-phase and green-phase evidence, the validation
  ordering discovery, the `DOC`-rule finding, the property non-vacuity
  statistics, and the final outcomes. Updated the trace chain to the test names
  that actually exist.
- 2026-10-20: Recorded the CodeRabbit disposition, the host-neutral fixture
  discovery, the test-module split forced by the 400-line cap, and the
  re-certified gate evidence at `dca91e375`.
