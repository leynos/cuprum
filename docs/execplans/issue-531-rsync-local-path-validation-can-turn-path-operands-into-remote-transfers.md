# Stop rsync operands from reaching remote syntax through local-path validation

This ExecPlan is a living document. The sections `Constraints`, `Tolerances`,
`Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: DRAFT -> IN PROGRESS -> COMPLETE (delivered on branch
`issue-531-rsync-local-path-validation-can-turn-path-operands-into-remote-transfers`,
draft PR #649, all deterministic gates green and CI `CLEAN`).

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
- [x] (2026-10-20) Rebase onto `5da6d68fb` after upstream #637 added the
      end-of-options separator and split the behaviour steps. PR #649 was
      `CONFLICTING`, so its merge ref was unbuildable and CI never ran. Five
      conflicts were resolved by keeping both sides; the remote-operand steps
      moved into `tests/behaviour/_builder_library_steps.py` to match the new
      binding-only structure. Focused suite after the rebase: 128 passed, 2
      skipped.
- [x] (2026-10-20) Split the remote-operand properties out of
      `test_builder_property_based.py`, which the merge had pushed to 442
      lines, over pylint's `max-module-lines = 400`. They now sit beside the
      named spellings they generalize in `test_rsync_operand_policy.py`.
- [x] (2026-10-21) Re-certify every gate at the rebased HEAD
      `ab53e24ebec6c9a90d1ac6315e4e79a4f582d567`. All seven logs record that
      HEAD and `EXIT=0`: `check-fmt`, `lint`, `typecheck`, `test`,
      `markdownlint`, `nixie`, and `duplication`. Evidence: `make test`
      reported 5249 passed, 116 skipped across its Python session summaries,
      167 of 167 passed in Rust nextest, and an all-`ok` doctest pass. The
      earlier run at this HEAD exited 2 with one
      `test_pinned_doctest_route_rejects_a_warning` timeout (1200 s against a
      3836-passed run that itself took 3 h 23 min under host contention);
      that single test passed alone in 319.86 s once inode-lock contention
      fell to zero, confirming the failure was environmental rather than a
      regression.
- [x] (2026-10-21) Push the rebased branch, confirm the PR merge ref is
      buildable, and watch CI to success. The remote tip was the pre-rebase
      `b1378b57a`, so the push used
      `--force-with-lease=...:b1378b57ac664207d40612f2866de2532ac2bd64`
      after confirming that SHA matched the reflog's rebase-start parent;
      `HEAD`, the refreshed tracking ref, and `git ls-remote` all then read
      `c8e8d25b0bb3c4a6a8603192e9b2116b6595d5af`. PR #649 went from
      `CONFLICTING` to `MERGEABLE`/`CLEAN`, and all 18 reported checks
      passed, including `lint-test`, the three Python version jobs, both
      extension-gated boundary jobs, all five native wheel builds,
      `verify-wheel-install`, and the 20-minute `coverage` job.
- [x] (2026-10-21) Re-run all seven gates at the delivery commit
      `160744a5e01dc882181c47db63bffd3841916a9b`, which adds only this
      ExecPlan record to the tree CI approved. Every log again records that
      HEAD and `EXIT=0`; `make test` reproduced the same totals of 5249
      passed and 116 skipped (Python), 167 of 167 (Rust nextest), and `ok`
      doctests. This closes the gap where the earlier logs certified the
      pre-record tree rather than the pushed one.
- [x] (2026-10-21) CodeRabbit pass over the rebased diff. It raised one major
      finding: the BDD fixture built rsync operands from `tmp_path`, whose
      Windows drive prefix is a remote operand, so the destination examples
      would report a source error. Fixed by anchoring both operands on
      `//rsync-scenario/...` and by asserting the non-remote counterpart is a
      local path. Focused suites: 128 passed, 2 skipped. The fix was verified by
      mutation: restoring the drive-prefix spelling reproduced five failures,
      four of them "the rejection must name the offending operand role".
- [x] (2026-10-21) Confirm CI at the fix commit
      `9fea46cafd8b5532fce0fce673a8cd08ad9af0cb`.
      All seven gates had already exited 0 at that HEAD with the same totals as
      the previous certification (5249 passed and 116 skipped in Python, 167 of
      167 in Rust nextest, all doctests `ok`). `gh pr checks 649` then reported
      18 passed and 3 skipped with no failures, including the 20-minute
      `coverage` job and `benchmark-ratchet`; the three skips are `automerge`,
      `Loom model smoke test`, and `Kody Code Review`, none of which this PR
      triggers. `gh pr view 649` reported `MERGEABLE` and `CLEAN` at that head.
- [x] (2026-10-21) Re-run CodeRabbit over the committed range after the fix.
      `coderabbit review --agent --committed --base main` reviewed all twelve
      changed files and returned zero findings, closing the one major finding
      from the previous pass.
- [x] (2026-10-21) Re-run all seven gates at
      `777afd82251743b70e29664d62582f17d4262be3`,
      the commit carrying the previous record. Every log records that HEAD and
      `EXIT=0`; `make test` again reported 5249 passed and 116 skipped across
      its 22 pytest session summaries, 167 of 167 in Rust nextest, and three
      `test result: ok` doctest lines. The earlier attempt at this record
      produced two logs labelled with the pre-edit HEAD while exercising the
      edited tree, so those logs were deleted rather than cited.
- [x] (2026-10-21) Act on the ExecPlan completeness assessment
      (`ep531-completeness-70b69d31a`). CodeRabbit found the production fix
      complete but named three remaining in-scope test defects, and inspection
      confirmed all three. (1) `test_rsync_builder_outputs_args` still built its
      operands from `tmp_path`, so on Windows the acceptance test would have
      raised instead of asserting the argv; it now uses the portable
      `//rsync-scenario/share` anchor, and the Windows boundary job cannot
      settle this because it runs only `make test-extension`. (2)
      `_operand_is_local` called `safe_path` alone, which also accepts
      `host:path` and drive prefixes, so the counterpart assertion could not
      establish the rsync-local property; it now calls `_local_rsync_operand`
      for the counterpart's own role. (3) No test proved the construction
      order, so the new
      `test_rsync_rejection_precedes_command_construction` records `sh.make`
      calls and asserts none occur for a rejected operand in either role. A
      fourth defect was found while fixing the first: the anchor was a
      two-component UNC path, which `PureWindowsPath` treats as a bare share
      and normalizes with a trailing separator, so a `share` component was
      added to keep the argv identical on both hosts. Non-vacuity evidence:
      the `tmp_path` spelling is `remote=True` under the Windows grammar while
      the new anchor is `remote=False`; and moving `sh.make` ahead of
      `_rsync_argv` fails all 18 construction-order cases. Focused suite: 133
      passed, 2 skipped.
- [x] (2026-10-21) Certify the delivery tip
      `1fb7884e04a17db1ac16635efcb233c7c1ba03d7`. All seven gates exited 0
      with that HEAD recorded in each log, and `make test` reproduced 5249
      passed and 116 skipped (Python), 167 of 167 (Rust nextest), and three
      `test result: ok` doctest lines. CI then reported 18 checks passed and
      the PR `CLEAN`/`MERGEABLE`. Because this record is itself a Markdown
      edit, `make check-fmt`, `make lint`, `make markdownlint`, and
      `make nixie` were re-run against the record commit; the full seven-gate
      certification therefore covers every file except this paragraph.
- [x] (2026-10-21) Certify the assessment-fix tree at
      `7a300bba29380685b3129e61c9d5a7ca25ccd217`. Two gate defects surfaced
      between the fix and this certification and were corrected in their own
      commits: `make fmt` reflowed one paragraph of decision log D7
      (`369add6c9`), and two lint leaves rejected the new construction-order
      test's spelling — classic Pylint C1803 for `factory_calls == []` and
      Ruff `banned-api` for `typing.Callable` (`0888ef2a7`, then `7a300bba2`).
      All seven gates then exited 0 with that HEAD recorded in each log:
      `check-fmt`, `lint`, `typecheck`, `test`, `markdownlint`, `nixie`, and
      `duplication`. Evidence: `make test` reported 5267 passed and 116
      skipped across its 22 Python session summaries, 167 of 167 in Rust
      nextest, and `test result: ok` doctest lines; `make lint` reported
      10.00/10 from both Pylint environments and `duplication` recorded 65
      reasoned exceptions. Logs carry the `-7a300bba2` suffix under `/tmp`.
      Because this record is itself a Markdown edit, the Markdown-scoped gates
      are re-run against the record commit, so the full certification covers
      every file except this paragraph.
- [x] (2026-10-22) Act on the pre-merge walkthrough's three non-passing rows.
      All three were valid and are now addressed. (1) Testing — the row asked
      for an end-to-end rsync through `run_sync()`, which was already provided
      by `test_rsync_copies_option_shaped_relative_paths` in
      `cuprum/unittests/test_builder_operand_separation.py` (a real rsync, a
      real transfer, added by upstream #637); the missing half was the
      rejection case, now `test_rsync_with_a_remote_looking_source_transfers_nothing`
      in the same module. It places a recording `rsync` stub first on `PATH`
      and asserts no call and no destination file, with
      `allow_relative=True` so the rsync grammar layer is the one under test
      rather than `safe_path()`'s absoluteness check. Non-vacuity: bypassing
      the remote-grammar check in `_local_rsync_operand` makes it fail with
      `DID NOT RAISE`. (2) User-facing documentation — a checklist row and a
      `rsync_sync()` section now sit in `docs/v0-2-0-migration-guide.md`, and
      an `[Unreleased]` `Fixed` entry records the change in `CHANGELOG.md`,
      since the fix has not shipped in any tagged release. (3) Developer
      documentation — `docs/cuprum-design.md` §11.3.1 now records the
      local-only rsync decision, the remote grammar it rejects, and why a
      remote transfer is an extension point. Focused suite at this tree: 4
      passed in `test_builder_operand_separation.py`.
- [x] (2026-10-22) Act on CodeRabbit's re-review of `28f0a35fe`. It withdrew
      the four earlier findings and retained two precision points, both valid.
      (1) The migration guide said the `ValueError` names "the operand and its
      role"; the message identifies the source or destination role but does not
      repeat the operand value, and an operand failing the generic check first
      is rejected without the role-specific wording. Corrected. (2) The stub
      test is construction-time evidence only: it calls `rsync_sync()` and
      never `run_sync()`, so the untouched recording file shows no executable
      ran, not that the execution boundary would have refused one. This
      paragraph now says so and points at obligation 8 and the accepted-path
      test for that boundary. CodeRabbit also noted that `CHANGES_REQUESTED` is
      still the recorded provider state and advised awaiting the required
      checks; both are tracked here rather than assumed resolved.

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
- Observation: merging upstream #637 does not leave the two features
  independent. Evidence: upstream's `_rsync_argv` now returns
  `(*flags, "--", *paths)`, so the acceptance tests written before it failed on
  the extra separator element, and the merged property module reached 442
  lines. Impact: the rebase had to reconcile the argv assertions, and the
  properties moved beside the named spellings in
  `test_rsync_operand_policy.py`. The `--` separator does not weaken the
  policy: it only stops option parsing, while a colon operand is still rsync's
  remote grammar.
- Observation: a conflicting PR has no CI at all. Evidence: `gh pr checks 649`
  listed only external checks while `gh pr view --json mergeable` reported
  `CONFLICTING`; CI builds the merge ref, which upstream cannot construct.
  Impact: the rebase was a precondition for observing CI, not merely a nicety.
- Observation: the BDD fixture `given_valid_rsync_paths` used `tmp_path`, which
  is a drive prefix on Windows and therefore a remote operand for rsync there.
  Evidence: CodeRabbit's post-rebase pass reported it, and re-posing
  `C:/Users/runner/.../source` to the policy showed
  `PureWindowsPath(...).is_absolute()` is `True` while
  `_is_remote_rsync_operand` is also `True`; substituting that spelling back
  into the fixture made five behaviour cases fail, four with "the rejection
  must name the offending operand role". Impact: both rsync steps now anchor
  their operands on `//rsync-scenario/...`, which is absolute on both hosts and
  colon-free, and the scenario asserts that the operand opposite the remote one
  is itself a local path, so a wrong-role rejection cannot pass for a
  right-role one.
- Observation: the same `tmp_path` defect survived the first fix in
  `cuprum/unittests/test_builder_library.py`. Evidence: CodeRabbit's
  completeness assessment named `test_rsync_builder_outputs_args`, and the
  Windows boundary job could not have caught it because
  `.github/workflows/ci.yml` runs only `make test-extension` there, whose
  targets are the Rust-extension suites. Impact: fixing one instance of a
  platform-dependent fixture is not evidence that the fixture class is fixed;
  the acceptance test now shares the portable anchor.
- Observation: the UNC anchor itself was subtly wrong. Evidence:
  `PureWindowsPath("//rsync-scenario/source").as_posix()` is
  `//rsync-scenario/source/` — a two-component UNC path is a bare share, so
  Windows appends a separator — while the three-component
  `//rsync-scenario/share/source` normalizes identically on both hosts. Impact:
  the anchor gained a `share` component, and the constant now records why.
- Observation: asserting "no argv was captured" does not prove `sh.make` never
  ran. Evidence: moving `sh.make(RSYNC)` ahead of `_rsync_argv` still raises,
  yet fails all 18 construction-order cases once the factory call is recorded.
  Impact: the boundary is now pinned at the factory, which is what the
  ExecPlan's construction-order constraint actually claims.

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

- D6. The rsync behaviour scenario uses a fixed UNC anchor rather than
  `tmp_path`, and asserts the non-remote counterpart is local. Rationale: the
  steps are documented to run on every platform, and `tmp_path` is a remote
  operand for rsync on Windows. Pinning the counterpart keeps the role
  assertion from being satisfied by a rejection of the wrong operand.
- D7. The anchor carries three components (`//rsync-scenario/share/...`) and
  is mirrored in `test_rsync_builder_outputs_args`. Rationale: a two-component
  UNC path is a bare share to `PureWindowsPath`, which appends a trailing
  separator, so the argv would differ between hosts. The unit acceptance test
  needs the same portability for the same reason, and calling the policy rather
  than `safe_path` in the counterpart step is what makes that assertion about
  rsync locality rather than generic validity.
- D8. The construction-order test records `sh.make` calls instead of relying
  on the absence of a returned command. Rationale: a builder that constructs
  and then discards a command raises identically, so only the factory call
  distinguishes the required ordering.

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

The rebase onto upstream #637 added a `--` separator to the rsync argv. That
separator and this policy are complementary, not competing: `--` stops option
parsing, while a colon before the first separator is rsync's own remote
grammar, which `--` does not change. The acceptance tests now assert the full
argv including the separator, so the two contracts are pinned together.

Lesson: the rebase's behaviour-test fixture reintroduced the very bug this
issue is about, in the tests rather than the library. `tmp_path` is a drive
prefix on Windows, which is exactly the spelling `safe_path()` used to accept
and rsync reads as remote, so a fixture that "just built absolute paths" was
itself a remote-transfer fixture on one platform. The fix is a fixed UNC anchor
that is absolute under both `PurePath` flavours and begins with a separator,
plus a step asserting the *opposite* operand is local. That second assertion is
the general lesson: a test that checks an error names the right operand is
vacuous unless it also pins the other operand as valid, because the builder
reports whichever operand it reaches first.

Lesson: the assessment fixes themselves needed three gate attempts before they
were shippable. The first commit failed `make check-fmt` because the new
ExecPlan prose was not reflowed, the second failed classic Pylint on
`factory_calls == []`, and the third failed Ruff's `banned-api` rule on
`typing.Callable`. Each failure was mechanical and cheap to fix, but each one
orphaned the gate evidence for the preceding head, so the whole seven-gate set
had to be re-run from `check-fmt`. Running the formatting and lint leaves
first, on the exact edited tree, would have collapsed that to one round.

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
   regression test, POSIX-only, skipped on Windows. That test asserts only the
   rejection code, so the non-vacuity evidence comes from its neighbours:
   `test_safe_path_requires_absolute_by_default` establishes both that a normal
   relative path is still rejected and that a native absolute path is accepted,
   and `test_safe_path_accepts_native_windows_drive_paths` covers the Windows
   side of the same pair. The skip therefore narrows which host runs the drive
   assertion, not whether the surrounding policy is exercised at all.
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
   normalizes them, after the `--` separator. Method: named acceptance test
   over absolute paths and relative `dir/file` and `dir/host:path` with
   `allow_relative=True`. Non-vacuity: `dir/host:path` specifically pins that
   the predicate does not over-reject.
5. Invariant: no emitted operand can start with `rsync://` or contain `:`
   before the first `/`. Method: Hypothesis property over generated
   `<host><: or ::><path>` strings, asserting rejection; plus an invariant
   property over accepted operands asserting the sink condition. Non-vacuity
   evidence (`--hypothesis-show-statistics`, rerun after the properties moved
   modules): the rejection property ran 200 passing examples with both
   separators and both roles; the acceptance property ran 200 passing examples
   mixing rejected and accepted candidates; the sink-check witness property ran
   100 passing examples, each asserting that `safe_path(allow_relative=True)`
   accepts the generated operand first.
6. Invariant: ordinary local relative paths continue to be accepted with
   `allow_relative=True` and unchanged in the argv. Method: property test over
   colon-free segment lists; the existing
   `test_rsync_sync_rejects_relative_paths_unless_opted_in` keeps its meaning.
7. Invariant: BDD-observable behaviour — a remote operand raises before any
   command is built, and the error names that operand's role. Method: a new
   scenario outline in `tests/features/builder_library.feature` with a step
   that captures the exception and asserts no `SafeCmd` was produced.
   Non-vacuity: the role assertion would also pass if the *other* operand were
   rejected first, so a companion step asserts the operand opposite the remote
   one is accepted by the rsync operand policy for its own role — not by
   `safe_path` alone, which also accepts `host:path` and drive prefixes. The
   fixture anchors both operands on `//rsync-scenario/share/...` so they are
   absolute and local on both POSIX and Windows; a `tmp_path`-based fixture
   reproduced five failures when substituted back, four of them at the role
   assertion.
8. Invariant: for a rejected operand, `sh.make` is never called, in either
   role. Method: a parameterized test that records factory calls through
   `monkeypatch.setattr(sh, "make", ...)` and asserts none occurred.
   Non-vacuity: moving `sh.make(RSYNC)` ahead of `_rsync_argv` in
   `rsync_sync()` fails all 18 cases, so the assertion detects the exact
   ordering defect the plan forbids rather than merely the absence of a
   returned command.
9. Axiom: rsync's operand grammar treats a colon before the first path
   separator as a remote specification. Source: `rsync(1)` and the ticket's
   static trace. This is treated as an external contract, not re-derived.
10. Axiom: `PurePath(...).is_absolute()` is host-native on the running
    interpreter. Source: `pathlib` documentation; exercised by both platform
    tests.

Residual gaps: the accepted-path evidence runs a live rsync, but the
rejected-path evidence does not, because a rejection means no process may start.
`test_rsync_with_a_remote_looking_source_transfers_nothing` in
`cuprum/unittests/test_builder_operand_separation.py` therefore puts an
executable stub named `rsync` first on `PATH` and asserts it recorded no
invocation. This is construction-time rejection evidence: the test calls
`rsync_sync()` and never `run_sync()`, so the untouched recording file shows no
executable ran, not that the *execution* boundary would have refused one. The
execution boundary itself is covered separately, by the existing factory-call
assertions and by the production validation order that rejects both operands
before `sh.make` is reached (obligation 8), and the accepted path is covered by
the real transfer in `test_rsync_copies_option_shaped_relative_paths`. The test
runs with `allow_relative=True`, because without it `safe_path()` rejects the
spelling as non-absolute first and the remote-grammar layer is never reached.
What remains unexercised is a real rsync receiving a remote operand, which is
the behaviour the fix exists to prevent; observing it would require accepting
the operand first. The ticket's own reproduction is static, and the security
scan did not run a live exploit.

Proof inventory: not applicable. This change introduces no CrossHair, Kani,
Verus, or LemmaScript proof and materially changes no existing proof, harness,
or verified production code, so there is no proof obligation for a separate
proof assessment to scrutinize. The obligations above are discharged by named
tests, parameterized matrices, and Hypothesis properties, with the recorded
non-vacuity evidence standing in for the negative controls a proof review would
otherwise require.

## Context and orientation

`cuprum/builders/args.py` holds the `SafePath` and `GitRef` validators.
`classify_path_string()` is the single source of truth for rejection categories;
`safe_path()` converts input to a string and delegates.

`cuprum/builders/rsync.py` holds `RsyncOptions`, `_FLAG_ORDER`,
`_rsync_argv()`, and `rsync_sync()`. `_rsync_argv()` is the pure argv builder;
`rsync_sync()` wraps its result with `sh.make(RSYNC)`.

Tests live in three layers: unit and property
(`cuprum/unittests/test_rsync_operand_policy.py` for the rsync operand policy
and its generated cases, `cuprum/unittests/test_builder_library.py` for the
rest of the builder surface, and
`cuprum/unittests/test_builder_property_based.py` for the shared argv
properties), and behaviour (`tests/features/builder_library.feature`, whose
steps are bound in `tests/behaviour/test_builder_library_behaviour.py` and
implemented in `tests/behaviour/_builder_library_steps.py`).

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
- 2026-10-20: Recorded the rebase onto `5da6d68fb`, the five conflict
  resolutions, the second 400-line split created by merging with #637, the `--`
  separator's interaction with the operand policy, and the fact that a
  conflicting PR receives no CI at all.
- 2026-10-21: Recorded the completeness assessment's three in-scope findings
  and their fixes, the two-component UNC normalization defect found while
  fixing them, and the new construction-order obligation with its mutation
  evidence.
- 2026-10-21: Recorded the three gate defects found while shipping those fixes
  (mdtablefix reflow, Pylint C1803, Ruff `banned-api`), the resulting
  certification at `7a300bba2` with 5267 passed and 116 skipped in Python, and
  a justified `not applicable` entry for the proof inventory.
