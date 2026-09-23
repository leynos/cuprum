# Add lightweight group and annotate flags to `RunOutputOptions`

Status: IN PROGRESS

This living ExecPlan records the implementation of issue
[#375](https://github.com/leynos/cuprum/issues/375). It is self-contained: a
reader with only this working tree and this file should be able to deliver the
change end to end.

## Purpose / big picture

A Continuous Integration (CI) caller who wants collapsible per-command log
groups and failure annotations currently has to construct a presentation-sink
adapter by hand:

```python
from cuprum import RunOutputOptions
from cuprum.sinks import GitHubActionsSink

RunOutputOptions(echo=True, sink=GitHubActionsSink())
```

That imports a second name, knows which adapter belongs to which CI vendor, and
requires the caller to reason about a protocol they do not otherwise use. Issue
[#375](https://github.com/leynos/cuprum/issues/375)
asks for two zero-ceremony booleans on `RunOutputOptions` instead:

```python
RunOutputOptions(echo=True, group=True, annotate_failure=True)
```

After this change, a caller who sets `group=True` sees the run's echoed output
bracketed in `::group::<program args>` / `::endgroup::` on a GitHub Actions
runner, and a caller who additionally sets `annotate_failure=True` sees exactly
one `::error::` annotation when the run ends in a non-zero exit, a timeout, or
an error. With neither flag set, output is byte-for-byte what it is today.

This is a **shim, not a replacement**. `GitHubActionsSink` remains the
extensible path — custom destinations, custom titles, and explicit activation
are properties of the adapter, and the flags deliberately expose none of them.
The flags are a convenience over the same framing contract, so a caller who
outgrows them moves to `sink=` rather than to a different feature.

The observable success is: `make test` passes, the new tests in
`cuprum/unittests/test_sinks_end_to_end.py` frame a real subprocess run, and a
run with the flags unset produces identical output to one run before this
change.

## Constraints

Hard invariants that must hold throughout implementation. Violation requires
escalation, not a workaround.

- **Defaults are inert.** With `group=False` and `annotate_failure=False` (both
  the default), parent-facing output, capture, exit codes, and the returned
  `CommandResult`/`PipelineResult` must be unchanged. This is the acceptance
  criterion "Absent the flags, output is unchanged" and it is byte-for-byte,
  not merely behavioural.
- **No workflow-command knowledge in the execution layer.** ADR-013 rejected
  Option B (group/annotate flags taught to the execution layer) precisely
  because it would spread Actions syntax across every terminal path of the
  runner and the pipeline. This plan honours that rejection by *defining* the
  flags as a thin synthesis of the existing adapter: the strings `::group::`,
  `::endgroup::`, `::error`, and `::stop-commands::` must not appear in
  `cuprum/sh.py`, `cuprum/_command_internals.py`, `cuprum/_pipeline_config.py`,
  `cuprum/_pipeline_internals.py`, or `cuprum/_sink_lifecycle.py`.
- **No new run-path code.** `SafeCmd.run`, `SafeCmd.run_sync`, `Pipeline.run`,
  `Pipeline.run_sync`, and the `_SinkBracket` lifecycle already read
  `output.sink` and open exactly one session per run (one per command; one for
  the whole pipeline). The flags must ride that existing lifecycle rather than
  add a second framing path.
- **Annotation hygiene is preserved verbatim.** The `::error::` title is the
  bounded derived label, never argv; the message is the categorical `detail`
  (`timeout`) or the `TerminalOutcome` value. Exception text and argument
  values never reach an annotation.
- **Existing public behaviour of `GitHubActionsSink` is unchanged.** The new
  adapter toggles default to the current behaviour, so the existing snapshot
  (`cuprum/unittests/__snapshots__/test_sinks_github_actions.ambr`) and every
  exact-string assertion in `cuprum/unittests/test_sinks_github_actions.py`
  must pass unmodified.
- **Python 3.12 baseline.** `pyproject.toml` sets
  `[tool.pylint.main] py-version = "3.12"`; do not introduce 3.13-only syntax.
- **Environment: en-GB-oxendict spelling** in code identifiers, comments,
  docstrings, and prose, except where an external interface dictates otherwise
  (`GITHUB_ACTIONS`, `::group::` and friends are that interface).

## Tolerances (exception triggers)

The user has authorized the full change, commits, a push, and a draft pull
request. Routine fixes inside that scope need no renewed approval.

- Scope: if implementation needs edits to more than 12 tracked files or 700 net
  changed lines, stop and escalate.
- Interface: if any *existing* public signature must change — rather than a new
  keyword defaulting to the current behaviour — stop and escalate.
- Dependencies: if a new runtime dependency is required, stop and escalate.
- Iterations: if a gate still fails after three focused attempts, stop and
  escalate with the log path rather than widening the change.
- Ambiguity: if the `sink=` interaction turns out to have a materially
  different sensible reading from the one chosen in `Decision log`, stop and
  present the options.

Do not run gates in parallel; this repository uses build caching and the
`scrutineer` subagent is the exclusive gate runner. Do not kill other agents'
processes.

## Risks

- Risk: `RunOutputOptions.__post_init__` synthesizing a sink makes a *frozen
  dataclass* mutate itself, and `object.__setattr__` is already the established
  idiom in that method for `echo_stdout`/`echo_stderr`/`idle_after`. Severity:
  low. Likelihood: low. Mitigation: follow the existing idiom, and assert the
  synthesized value in a construction test.
- Risk: the synthesized `GitHubActionsSink` is a *new* object created per
  `RunOutputOptions` construction, so
  `RunOutputOptions(…) == RunOutputOptions(…)` with flags set but no sink
  compares two `dataclass` fields holding two distinct sink objects.
  `GitHubActionsSink` defines no `__eq__`, so identity comparison makes those
  two options unequal. Severity: medium. Likelihood: high (the property test in
  `test_pipeline_output_options.py` compares resolved options for equality).
  Mitigation: the comparison only bites when a test *constructs* options twice
  and compares them; the existing property test compares
  `_resolve_pipeline_output(None, {}) == RunOutputOptions()`, which is
  unaffected because no flags are set and no sink is synthesized. New tests
  must compare fields, not whole options, when flags are set. Recorded here so
  a future equality assertion is not written by accident.
- Risk: an env-gated sink means the flag is a no-op outside GitHub Actions,
  which a caller may read as a bug. Severity: low. Likelihood: medium.
  Mitigation: this is the deliberate, ADR-013-consistent behaviour; document it
  in the docstring, the users' guide, and the CHANGELOG entry in the same
  commit.
- Risk: `cuprum/sh.py` is 977 lines against a Pylint `max-module-lines = 400`.
  Severity: low. Likelihood: certain if an import or field pushes a previously
  silent file over a threshold. Mitigation: per project memory, `pylint cuprum`
  runs under a PyPy 3.11 shim that skips files it cannot parse, so a 3.12-only
  file is silently unchecked; confirm the actual `make lint` result rather than
  assuming either way, and do not restructure `sh.py` to chase a gate that is
  not firing.
- Risk: the repository's CI builds the *merge ref* for pull requests, so a
  green local branch can still fail on main's concurrent changes. Severity:
  low. Likelihood: medium. Mitigation: re-fetch and use `git merge-tree` before
  trusting a rebase; treat a red CI check as possibly-not-ours and read the log
  before reacting.

## Progress

- [x] (2026-09-22) Renamed the branch to
  `issue-375-add-lightweight-group-annotate-flags-to-runoutputoptions` and set
  the Lody session title.
- [x] (2026-09-22) Phase 1 read-only confirmation pass complete. See
  `Surprises & discoveries` for the three corrections it produced.
- [x] (2026-09-22) Phase 2 complete: adapter toggles committed as `24c63aa8`
  and updated to the specified `emit_group=`/`emit_annotation=` API in
  `2aa2634d`. `GitHubActionsSession` takes its title and both toggles as one
  `_Annotation` value.
- [x] (2026-09-22) Phase 3 complete: `RunOutputOptions.group` and
  `annotate_failure` plus sink synthesis committed as `8276dd9d`. Gate findings
  were recorded in `5047f22d`; affected call sites were repaired in `cc319a87`.
- [x] (2026-09-22) Phase 4 complete: real-subprocess coverage is in
  `cc319a87`/`d680cfa9`; the users' guide subsection, CHANGELOG entry, ADR-013
  amendment, and plan update are in `0617b31e`.
- [x] (2026-09-23) Rebased onto `origin/main` at `e82cf9f6`; `git range-diff`
  confirmed all nine feature commits replayed without patch changes.
- [x] (2026-09-23) All seven sequential gates passed: `make check-fmt`,
  `make lint`, `make typecheck`, `make test`, `make markdownlint`,
  `make spelling`, and `make nixie`. Evidence is in the `-6.out` logs under
  `/tmp` for this worktree.
- [x] (2026-09-23) First CodeRabbit review on `563f8a52` found annotation-only
  echo routing through the sink log. The session now opts out of echo
  redirection when grouping is disabled, preserving stdout/stderr while still
  writing workflow commands to the parent's stderr. Added async command and
  pipeline integration coverage and corrected the lifecycle, guide, design,
  developer, and ADR documentation. The suggested stop-commands lease when
  `emit_group=False` conflicts with the explicit adapter contract, which says
  not to emit that lease; the tests continue to assert it is absent.
- [x] (2026-09-23) Remediation gates passed sequentially: `make check-fmt`,
  `make lint`, `make typecheck`, `make test`, `make markdownlint`,
  `make spelling`, and `make nixie`. The second run found that command stream
  routing bypasses `_SinkBracket.resolve_destination`; `_resolve_stream_sink`
  now honours the session echo-routing hint too. The corrected end-to-end test
  confirms that annotation-only mode preserves both parent streams.
- [x] (2026-09-23) Second CodeRabbit review on `2b4db194` found duplicated
  plan-evidence corrections, a public constructor compatibility gap, one `Path`
  construction improvement, and missing assertion messages. Corrected the
  plan's test evidence and stream-routing contract, retained the legacy
  `GitHubActionsSession(annotation_label=<str>)` form, and updated the tests.
- [x] (2026-09-23) Corrected the constructor dispatch to satisfy R9101, then
  passed all seven gates sequentially. `make test` reported 2,183 passed and 63
  skipped in the main suite; additional suites reported 12 passed/3 skipped and
  21 passed/13 skipped. Logs use the `-review2fix4.out` suffix under `/tmp`.
- [x] (2026-09-24) Third CodeRabbit review on `1d8147ed` found a shared-stderr
  concurrency limitation and documentation drift. Documented that concurrent
  grouped runs using the default destination must be serialized, corrected the
  shared-options sink guidance and planned session signature, and aligned the
  public option validation with issue #375's required `ValueError` contract. A
  narrow `type-check-without-type-error` suppression records that explicit
  exception requirement.
- [x] (2026-09-24) Corrected the suppression to use the configured lint rule
  name and passed all seven deterministic gates sequentially. `make test`
  reported 2,183 passed and 63 skipped in the main suite; auxiliary suites
  passed, with 3 Rust doctests ignored. Logs use the `-review3fix3.out` suffix
  under `/tmp`.
- [x] (2026-09-24) Updated the plan after the full gate run; `make fmt`,
  `make markdownlint`, `make spelling`, and `make nixie` all passed. The docs
  gate logs use the `-review3fix4.out` suffix under `/tmp`.
- [ ] Review the gated fixes with CodeRabbit and resolve any remaining
  in-scope findings.
- [ ] Push and open the draft pull request.

## Surprises & discoveries

- Observation: the ticket's proposal section cites **ADR-010**, but ADR-010 is
  "Rust pump hop span". The presentation sink is **ADR-013**
  (`docs/adr-013-opt-in-github-actions-presentation-sink.md`). Evidence:
  `ls docs/adr-010*` resolves to `docs/adr-010-rust-pump-hop-span.md`; the sink
  decision is titled "ADR 013: Opt-in GitHub Actions presentation sink".
  Impact: all new prose must cite ADR-013, and the ADR to amend is 013. The
  ticket's own coding plan already carries this correction.

- Observation: **pipelines open exactly one session for the whole pipeline**,
  not one per stage. `_prepare_pipeline_config` calls `_SinkBracket.open` once
  with `SessionStart(label="pipeline", argv=())`. Evidence:
  `cuprum/_pipeline_config.py:196-199`; the existing assertion
  `value.count("::group::") == 1` in
  `cuprum/unittests/test_sinks_end_to_end.py:224`. Impact: the ticket's
  acceptance criterion "Pipeline runs emit one group per command, matching the
  sink adapter" cannot mean one group per *stage*. The only reading consistent
  with the adapter it says it matches is: each *command* run (whether reached as
  `SafeCmd.run` or as a pipeline stage) produces at most one group, and a
  `Pipeline` emits **one group for the whole pipeline**. This plan implements
  that reading and names the test `test_flags_frame_pipeline_as_single_group`.

- Observation: `RunOutputOptions` accepts **positional** construction, and a
  test pins the positional order of `capture`/`echo`. Evidence:
  `cuprum/unittests/test_idle_heartbeat.py:126` constructs
  `RunOutputOptions(False, True)`. Impact: append `group` and
  `annotate_failure` *after* `sink` so the existing positional prefix is
  undisturbed. Note the fields are not keyword-only, so a hypothetical
  `RunOutputOptions(..., ...)` with nine positional arguments would shift — no
  caller in the tree does that, and the appended position is the least
  disruptive choice.

- Observation: **adding two keyword parameters to each of the sink's two
  constructors breaches `PLR0913`** (`max-args = 4`, `pyproject.toml:216`). The
  gate message is
  `too-many-arguments: Too many arguments in function definition (5 > 4)`.
  Evidence: `make python-lint` on the first Phase 2 draft; `max-args` is 4 for
  ruff and 5 for the separate df12 pylint pass, so ruff is the binding limit.
  Impact: both constructors needed a shape that fits four arguments. The tuple
  of `(label, emit_group, emit_annotation)` — all of which describe one
  annotation decision and always travel together — moves as a single frozen
  `_Annotation` value, which is a genuinely better grouping than a lint dodge.
  The sink's own `group`/`annotate` keywords still make five, which is
  keyword-only throughout and carries a documented
  `# ruff: ignore[too-many-arguments]`: the rule exists to catch a caller
  confusing positional argument order, and an argument after `*` has no
  position to confuse. Proven live both ways — the rule id is only accepted if
  `ruff check` reports it once the comment is removed, and the check was run in
  both states (1 finding without, 0 with).

- Observation: **`github_actions.py` was at 345 lines against a 400-line
  `max-module-lines` cap** (`pyproject.toml:226`), so the Phase 2 additions
  breached `C0302` at 428/400. Evidence: the real gate, `make python-lint`,
  printed
  `cuprum/sinks/github_actions.py:1:0: C0302: Too many lines in module
  (428/400)`
  and exited 16. A plain `uv run pylint` does *not* reproduce the gate
  exactly, because the gate runs pylint under the PyPy shim; the module was
  still parsed here (it is 3.12-compatible), so the finding was real rather
  than one of the shim's silent skips. Impact: net +45 lines was inside the
  55-line headroom but the extracted helpers spent it. Three helpers written
  for the arity fix became unnecessary once `_Annotation` carried the flags,
  and were inlined back to main's shape; two docstrings were trimmed to their
  one-line form. The module lands at 390 lines with a 10-line margin. **Lesson
  for the next phase: check `max-module-lines` before adding to a near-cap
  module, not after.**

- Observation: `raise ValueError` for the non-`bool` flag check trips ruff's
  `type-check-without-type-error` rule, which prefers `TypeError` for an
  invalid *type*. Evidence: the first Phase 3 gate reported this rule at the
  options validation. Resolution: issue #375 explicitly requires `ValueError`
  for the `RunOutputOptions` flags, so that public contract keeps the requested
  exception with a narrow suppression and an adjacent reason. Standalone
  `GitHubActionsSink` toggle validation retains its existing `TypeError`
  behaviour.

- Observation: **commit `977065cf` temporarily renamed the sink's keywords to
  `group=`/`annotate=` while `_open_gha_session` still passed `emit_group=`/
  `emit_annotation=`**, so that revision could not construct the object used by
  a third of the adapter suite. The mismatch was repaired in `77532cf2`, but
  the public keyword names still diverged from the supplied implementation
  contract. During the resumed review, the constructor and helper were aligned
  on `emit_group=`/`emit_annotation=`; this is an additive configuration
  surface on `GitHubActionsSink`, while the convenience flags retain the shorter
  `RunOutputOptions.group` and `RunOutputOptions.annotate_failure` names.
  **Lesson: keep constructor names, helpers, tests, and the recorded contract
  aligned in the same change.** The tolerance that should have caught the
  earlier mismatch is the requirement to gate every commit; a gate run is
  evidence only for the exact candidate it validated.

## Decision log

- Decision: reuse `GitHubActionsSink` and the existing `RunOutputOptions.sink`
  field; do **not** build a new framing module and do **not** touch the run
  paths. Rationale: the sink lifecycle already brackets every run path exactly
  once and already closes on every terminal path, cancellation-safely.
  Synthesizing the adapter from the flags means the flags inherit all of that
  for free, and keeps `::group::` out of the execution layer — which is exactly
  the concern ADR-013's Option B rejection names. Date/Author: 2026-09-22,
  agent.

- Decision: **an explicit `sink` wins and the flags are ignored.** No
  `ValueError`. Rationale: the ticket offers either reading. Ignoring is the
  composable one: a caller who has both a shared `RunOutputOptions` carrying
  `group=True` and an explicit sink for one call gets the sink they explicitly
  passed, with no exception to catch at the call site. It also cannot break
  existing callers, whereas raising turns a previously valid construction into
  an error. Documented in the field docstring, the users' guide, and the
  CHANGELOG. Date/Author: 2026-09-22, agent.

- Decision: name the adapter toggles `emit_group` and `emit_annotation`, not
  `group`/`annotate`. Rationale: `group` on the adapter collides conceptually
  with the group *label* the adapter already derives, and `annotate_failure` as
  an adapter parameter would over-specify (the adapter only ever annotates
  failures). The adapter names describe what the adapter writes; the option
  names describe what the caller wants. Date/Author: 2026-09-22, agent.

- Decision: validate the toggles in **both** places — `RunOutputOptions` and
  the adapter constructor. Rationale: each is usable without the other. A
  caller may pass `GitHubActionsSink(emit_group=1)` directly, and the flags may
  be set on an options object whose sink is later overridden per call. One
  check at either site would leave the other entry point accepting a merely
  truthy value. Date/Author: 2026-09-22, agent.

- Decision: keep the adapter constructor's toggle keywords as
  `emit_group=`/`emit_annotation=`, as specified in the ticket's coding plan.
  The `RunOutputOptions` fields retain `group`/`annotate_failure`; synthesis
  maps those public convenience fields to the adapter's explicit write
  controls. Rationale: the shorter names describe caller intent, while the
  adapter names state which workflow-command frames it writes. Date/Author:
  2026-09-23, agent.

- Decision: the synthesized sink carries no `force` and no `title`.
  Rationale: `force` would frame runs outside GitHub Actions, contradicting
  ADR-013's activation section and making the flag surprising. Omitting `title`
  keeps the group titled with argv and the annotation titled with the bounded
  label, preserving the split the adapter already enforces. Date/Author:
  2026-09-22, agent.

- Decision: validate `group`/`annotate_failure` as strict `bool` and raise
  `ValueError` otherwise, as issue #375 explicitly requires. Rationale: reject
  truthy non-bools such as `1` and preserve the requested public exception
  contract; a narrowly scoped `type-check-without-type-error` suppression
  records why the preferred `TypeError` is not used here. Date/Author:
  2026-09-24, agent.

## Conformance basis

Upstream artefacts, named exactly:

- `docs/adr-013-opt-in-github-actions-presentation-sink.md` (accepted
  2026-09-07) — the governing decision. Its Option B ("add `group`/`annotate`
  flags to `RunOutputOptions`") was rejected for spreading Actions syntax into
  the execution layer; its Option C (the sink protocol) was accepted. This plan
  satisfies Option B's *user-facing goal* through Option C's *mechanism*, and
  must record that reconciliation as an ADR-013 amendment.
- `docs/users-guide.md` § "Presentation sinks" (line 655) — the user-facing
  contract the new flags extend.
- `CHANGELOG.md` `## [0.2.0]` → `### Added` — where the new flags are
  announced. Per project convention the unreleased section stays open and
  undated, with no version link definition.
- Issue [#375](https://github.com/leynos/cuprum/issues/375) — the request.
  Issue [#360](https://github.com/leynos/cuprum/issues/360) — the origin of the
  sink feature the flags delegate to.
- `docs/developers-guide.md` and `docs/contents.md` — the documentation index;
  `docs/contents.md` already lists `execplans/` generically, so this plan needs
  no index edit.

Trace chain:

```plaintext
ISSUE-375-flags -> ADR-013-decision-outcome -> RunOutputOptions.group
  -> GitHubActionsSink.emit_group -> EP-M2
  -> cuprum/unittests/test_sinks_github_actions.py
  ::test_emit_group_false_suppresses_group_framing
ISSUE-375-flags -> ADR-013-decision-outcome -> RunOutputOptions.annotate_failure
  -> GitHubActionsSink.emit_annotation -> EP-M2
  -> cuprum/unittests/test_sinks_github_actions.py
  ::test_emit_annotation_false_suppresses_every_error
ISSUE-375-aggregation -> EP-M1 -> cuprum/sh.py::_resolve_pipeline_output
  -> EP-M3 -> cuprum/unittests/test_pipeline_output_options.py::test_flags_synthesize_github_actions_sink
ISSUE-375-unchanged-defaults -> ADR-013-non-goals -> EP-M4
  -> cuprum/unittests/test_sinks_end_to_end.py
  ::test_flags_leave_default_output_unchanged
```

No Terms of Reference or technical design document governs this change; the ADR
is the highest applicable authority.

## Verification plan

This change introduces no new algorithm, no arithmetic, and no concurrent
state. Its obligations are interface invariants and ordering properties, so
named example tests and finite parameter tables are the proportionate methods;
no property test beyond the existing Hypothesis corpus is warranted, and no
formal proof obligation arises. Recorded explicitly per the "if the change
introduces no non-trivial invariant or lemma" clause.

**V1 — Defaults are inert.** Statement: for any `RunOutputOptions` with
`group=False` and `annotate_failure=False`, `__post_init__` leaves `self.sink`
as `None`, and a run using the options writes no framing byte. Method: one
options test and one end-to-end comparison. Artefacts:
`cuprum/unittests/test_pipeline_output_options.py::test_group_and_annotate_failure_default_off`
asserts both defaults and `sink is None`;
`cuprum/unittests/test_sinks_end_to_end.py::test_flags_leave_default_output_unchanged`
compares bytes from flags-off and flag-absent runs. Non-vacuity: the
successful framing test opens the group and lease, closes the group, and emits
no error; `test_flags_annotate_non_zero_exit` separately proves the annotation
path. Discharge: all tests pass.

**V2 — "Sink wins" precedence.** Statement: when `sink` is supplied,
`__post_init__` binds `self.sink` to that object and never constructs a
`GitHubActionsSink`, whatever the flags say. Method: named example test.
Artefact:
`cuprum/unittests/test_pipeline_output_options.py::test_explicit_sink_wins_over_flags`.
Evidence: the test passes a `RecordingSink` double alongside both flags true
and asserts `output.sink is recording` *and* that no attribute of a synthesized
adapter leaked. Non-vacuity: `test_flags_synthesize_github_actions_sink`
asserts the synthesized instance's toggles with the flags set and no sink,
proving the synthesis branch is live in the same test module. Discharge: both
pass.

**V3 — Toggle independence.** Statement: `emit_group=False` suppresses the
group command, the stop-commands lease, and the endgroup, while leaving both
the log destination and the failure annotation intact; `emit_annotation=False`
suppresses every `::error::` line while leaving the framing intact. Method:
finite parameter table over the four toggle combinations for the framing
assertions, plus named example tests for the cross-cases. Artefacts:
`cuprum/unittests/test_sinks_github_actions.py::test_emit_group_false_suppresses_group_framing`
and `::test_emit_annotation_false_suppresses_every_error`. Evidence:
`group=False, annotation=True` yields a buffer with one `::error` and zero
`::group::`; `group=True, annotation=False` yields a buffer with one
`::group::`, one lease, one endgroup, and zero `::error` on `EXIT_NONZERO`,
`TIMEOUT`, `CANCELLED`, and `ERROR`. Non-vacuity: the pre-existing
`test_nonzero_close_emits_error_annotation` exercises both toggles at their
defaults against the same helper, so an implementation that suppressed the
annotation unconditionally would fail it. Discharge: all pass, and the snapshot
is unchanged.

**V4 — Framing order and injection shielding are preserved through the flag
path.** Statement: for a real subprocess run driven by `group=True`, the lease
opens after the group command and releases before the endgroup, and child
output that prints workflow commands cannot close the group early. Method:
end-to-end example test against a real interpreter, following the existing
index-ordering idiom. Artefact:
`cuprum/unittests/test_sinks_end_to_end.py::test_flags_stop_commands_neutralize_child_output`.
Evidence: index assertions place the child's spoofed `::endgroup::` and
`::error` before the lease release and the session's own endgroup after it.
Non-vacuity: `test_child_workflow_commands_stay_inside_the_lease` already
proves the same ordering for the explicit-sink path, so the flag path is
compared against a known-good control rather than an empty buffer. Discharge:
passes.

**V5 — Annotation content hygiene survives the flags.** Statement: an
annotation emitted through the flag path carries the bounded derived label as
its title and a categorical message, and never argv. Method: end-to-end example
test with a distinctive argument value. Artefact:
`cuprum/unittests/test_sinks_end_to_end.py::test_flags_annotation_omits_argv`.
Evidence: the argument appears in the `::group::` title and does not appear
after `::error`. Non-vacuity: the test asserts the value *is* present in the
buffer, so a run that framed nothing cannot pass it. Discharge: passes.

**V6 — Outcome matrix.** Statement: a non-zero exit returns its exit code and
annotates `exit_nonzero`; a timeout raises `TimeoutExpired` and annotates
`timeout`; a non-timeout spawn error raises `FileNotFoundError` and annotates
`error`. These tests use annotation-only mode, so they do not establish group
closure. Method: named example tests, one per terminal path. Artefacts:
`test_flags_annotate_non_zero_exit`, `test_flags_annotate_timeout`,
`test_flags_annotate_internal_error` in
`cuprum/unittests/test_sinks_end_to_end.py`. Evidence: each asserts exactly one
`::error` annotation and its categorical message; the tests also assert the
exit code, raised timeout, or unchanged spawn exception. Non-vacuity: the three
messages are distinct, so an implementation that hard-coded one categorical
string fails two of the three. Discharge: all pass.

**V7 — Pipeline aggregation.** Statement: a pipeline with `group=True` opens
exactly one group titled `pipeline`, and its annotation reflects the first
failing stage. Method: named example test. Artefact:
`cuprum/unittests/test_sinks_end_to_end.py::test_flags_frame_pipeline_as_single_group`.
Evidence: `value.count("::group::") == 1`,
`value.startswith("::group::pipeline\n")`, and the annotation reports the first
failing stage's non-zero outcome. Non-vacuity: the stage exit codes are
asserted too (`[0, 4]`), so a pipeline that silently failed to start cannot
produce the expected annotation by accident. Discharge: passes.

**V8 — Both entry points.** Statement: `group`/`annotate_failure` work through
`SafeCmd.run`, `SafeCmd.run_sync`, `Pipeline.run`, and `Pipeline.run_sync`.
Method: real-subprocess tests call both the synchronous and asynchronous entry
points directly. Artefacts: `test_flags_frame_successful_run` and
`test_flags_annotate_async_run` cover `SafeCmd.run_sync` and `SafeCmd.run`;
`test_flags_frame_pipeline_as_single_group` and
`test_flags_annotate_async_pipeline` cover `Pipeline.run_sync` and
`Pipeline.run`. Evidence: each assertion checks framing, result or stage exit
codes, and the categorical annotation. Discharge: all four pass.

**Axioms relied on (not verified here, treated as documented interfaces):**
that the GitHub Actions runner parses workflow commands from the parent's
stderr with the documented escaping, and that `secrets.token_hex` draws from
the operating system CSPRNG. Both are external contracts already relied on by
ADR-013 and its existing tests; this change does not extend the reliance.

**Negative control.** The intended mutation is "synthesize a
`GitHubActionsSink` unconditionally in `__post_init__`". V1 must fail for the
inert-default reason under that mutation. This was exercised as part of Phase 3
development by running `test_flags_leave_default_output_unchanged` before
adding the `sink is None` guard path, and it failed on the assertion that no
framing byte was written.

## Context and orientation

Cuprum is a safe subprocess-execution library. A caller builds a `SafeCmd` from
a curated `Program` and runs it; `RunOutputOptions` (`cuprum/sh.py`) carries
every output-related setting: `capture`, `echo`, `echo_stdout`, `echo_stderr`,
`max_echo_line_bytes`, `on_line`, `idle_after`, `on_idle`, `sink`, `group`, and
`annotate_failure`.

The **presentation-sink** feature (ADR-013) lets a caller reframe the
parent-facing output of a run without changing capture, exit codes, or the
returned result. The protocol lives in `cuprum/sinks/base.py`: an `OutputSink`
opens one `OutputSession` per run, routes echoed output through `session.log`
by default, and closes the session exactly once per terminal path with a
`SessionOutcome` (a `TerminalOutcome` from the closed set `exit_zero`,
`exit_nonzero`, `timeout`, `cancelled`, `error`, an optional exit code, and an
optional categorical detail). A session with `redirects_echo=False` keeps echo
on the caller's ordinary destinations while still using `session.log` for its
own workflow commands.

`cuprum/sinks/github_actions.py` implements the protocol for GitHub Actions. It
writes, in order: `::group::<program args>`, a `::stop-commands::<token>` lease
with a cryptographically random token, then (through `session.log`) the run's
echoed output when `redirects_echo` is true, then at close `::<token>::` to
release the lease, `::endgroup::`, and — for any outcome other than
`exit_zero` — one `::error title=<bounded label>::<categorical detail>`
annotation. Activation is read per run from `GITHUB_ACTIONS`, and only the
exact value `true` activates it, unless the sink was constructed with
`force=True`.

The lifecycle that brackets every run lives in `cuprum/_sink_lifecycle.py`:
`_SinkBracket.open(sink, start)` wraps `sink.open_session(start)`, and
`bracket.close(outcome=...)` is a take-once finalizer. `SafeCmd.run` opens one
bracket per command (`cuprum/_command_internals.py:282`) with
`_command_session_start(cmd)`; `Pipeline.run` opens one bracket for the whole
pipeline (`cuprum/_pipeline_config.py:196`) with
`SessionStart(label="pipeline", argv=())`. Both read `output.sink` — the only
two reads of that field in the production tree.

Naming used below and in the code: `read-only confirmation` means inspecting
without editing; `synthesis` means `RunOutputOptions.__post_init__` assigning
`self.sink`; `toggles` means the adapter's `emit_group` and `emit_annotation`.

## Plan of work

The ticket supplies a four-task plan. Run it as four milestones, each ending in
a coherent, committed state.

**Phase 1 — confirmation (no code changes). Complete.**

Already discharged by reading the tree. Its findings are recorded in
`Surprises & discoveries` and `Decision log`; the only open question it raised
(the pipeline group-count reading) is resolved there.

**Phase 2 — adapter toggles (`EP-M2`).**

In `cuprum/sinks/github_actions.py`:

1. Add `emit_group: bool = True` and `emit_annotation: bool = True` to
   `GitHubActionsSink.__init__`, store both, and document them in the class
   docstring's `Parameters` section.
2. Pass the annotation title and both toggles through `open_session` in one
   `_Annotation` value. Preserve direct `GitHubActionsSession` construction
   with the existing keyword-only `annotation_label=<str>` form; its toggles
   default to `True`.
3. In `GitHubActionsSession.__init__`, when `emit_group` is `False`, do not
   write the group command and do not write the lease — the lease exists only
   to shield a group, so a suppressed group must not leave a lease open. Its
   `log` accessor is unchanged; the optional `redirects_echo` hint lets
   annotation-only sessions preserve normal echo destinations.
4. In `GitHubActionsSession.close`, when `emit_group` is `False`, skip the
   lease release and the endgroup; when `emit_annotation` is `False`, skip
   `_emit_error_annotation`. Keep the idempotent `_closed` guard first.
5. Update the module docstring's framing-order list to state that each step is
   conditional on its toggle.

Escaping helpers, `_new_stop_token`, label derivation, and the annotation
content contract are untouched.

**Phase 3 — options and synthesis (`EP-M1`, `EP-M3`).**

In `cuprum/sh.py`:

1. Add `from cuprum.sinks.github_actions import GitHubActionsSink` beside the
   existing `from cuprum.sinks import base as sinks`. There is no import cycle:
   `cuprum.sinks.github_actions` imports only `cuprum.sinks.base`, which
   imports nothing from `cuprum`. Verify by importing `cuprum.sh` in a fresh
   interpreter.
2. Append the fields after `sink`:
   `group: bool = False` and `annotate_failure: bool = False`.
3. Extend the `Parameters` docstring with both, stating: what they do; that
   they reuse `GitHubActionsSink`; that workflow commands go to the parent's
   stderr only; that they are inert unless `GITHUB_ACTIONS == "true"` (or the
   caller passes `sink=` explicitly); that an explicit `sink` takes precedence
   and makes the flags no-ops; and that a pipeline emits one group for the
   whole pipeline. Add a doctest-style example to the existing `Examples` block.
4. In `__post_init__`, validate both are `bool` (`isinstance(x, bool)`, in a
   helper `_validate_convenience_flags` rather than inline), raising
   `ValueError` with the offending value as specified by issue #375. A narrow
   `type-check-without-type-error` suppression documents the intentional
   difference from the lint rule's preferred exception class. Do the validation
   *before* the `max_echo_line_bytes` early return so it always runs.
5. After validation, synthesize in a helper `_synthesize_sink_from_flags`:
   if `self.sink is None and (self.group or self.annotate_failure)`, then
   `object.__setattr__(self, "sink", GitHubActionsSink(emit_group=self.group,
   emit_annotation=self.annotate_failure))`.
   Otherwise leave `self.sink` alone.
6. `IOOptions` inherits the fields and the synthesis through
   `RunOutputOptions.__post_init__`; no change needed there, but confirm the
   inheritance with a test.
7. `_resolve_pipeline_output` needs **no change**: it already returns
   `output` unchanged when it is not `None`, and constructing a fresh
   `RunOutputOptions` re-runs `__post_init__`, so `group`/`annotate_failure`
   survive both resolution branches and re-synthesis happens if the caller
   reconstructed the options. Do **not** add the flags to
   `_DeprecatedOutputFlags`: they are `output=`-only, matching the ticket.

**Phase 4 — tests and documentation (`EP-M4`).**

Tests, per `Verification plan` V1–V8:

- `cuprum/unittests/test_sinks_github_actions.py`: the two toggle tests plus a
  small finite table over the four toggle/outcome combinations.
- `cuprum/unittests/test_pipeline_output_options.py`: add `group` and
  `annotate_failure` as `st.booleans()` to the `_OUTPUT_OPTIONS` strategy; add
  default-value assertions; add `ValueError` construction tests for non-bool
  inputs; add `test_flags_synthesize_github_actions_sink`,
  `test_explicit_sink_wins_over_flags` (using `RecordingSink` from
  `cuprum/unittests/_sink_test_support.py`), and
  `test_io_options_inherits_the_flags`, and a resolution test that
  `_resolve_pipeline_output` preserves both flags.
- `cuprum/unittests/test_sinks_end_to_end.py`: the flag-related tests named in
  the `Verification plan`, each monkeypatching `GITHUB_ACTIONS=true` (the
  synthesized sink is env-gated and carries no `force`), reusing the existing
  `_stop_token`/scoped-allowlist helpers and the `python_catalogue()` helper.
  Cover single-command cases across both `run` and `run_sync`.
- `test_flags_annotation_omits_argv` must not collide with the existing
  `test_failing_run_annotates_without_argv`; if a class-level grouping is used,
  keep each class at or below 20 public methods (ruff `PLR0904` / pylint
  `R0904` fire at 26).

Documentation:

- `docs/users-guide.md`: a subsection beside "Presentation sinks" (which starts
  at line 655) covering both flags, the `GitHubActionsSink` reuse, the
  `GITHUB_ACTIONS == "true"` gate, the "explicit sink wins" rule, the
  one-group-per-pipeline rule, and the annotation's bounded-label +
  categorical-message contract. Wrap prose at 80 columns.
- `CHANGELOG.md`: one `### Added` bullet under `## [0.2.0]` with a bold
  feature lead-in, naming `RunOutputOptions.group` and
  `RunOutputOptions.annotate_failure`, stating the shim relationship, and
  ending with `([#375](https://github.com/leynos/cuprum/issues/375))`.
- `docs/adr-013-opt-in-github-actions-presentation-sink.md`: an **appended**
  amendment section (following the house style of
  `docs/adr-004-interrogate-docstring-gate.md`'s
  `### Amendment (YYYY-MM-DD): …` heading) recording that the convenience flags
  delegate to the adapter, so no workflow-command knowledge reaches the
  execution layer, reconciling the flags with the earlier rejection of Option
  B. Accepted ADR text is append-only: do not edit the existing body.

## Concrete steps

All commands run from the worktree root,
`/home/leynos/.lody/repos/github---leynos---cuprum/worktrees/d0f1ffbc-0c8a-4d1e-9c73-ba3f69bbd5c1`.

The Red stage, for the adapter toggles (run before Phase 2's production edit):

```sh
uv run pytest cuprum/unittests/test_sinks_github_actions.py \
  -k "emit_group_false or emit_annotation_false" -q 2>&1 | tee /tmp/test-issue-375-red.out
```

Expect: collection succeeds and both tests fail —
`TypeError:
GitHubActionsSink.__init__() got an unexpected keyword argument 'emit_group'`
for the first. That failure is for the intended reason.

The Green stage, after the production edits:

```sh
uv run pytest cuprum/unittests/test_sinks_github_actions.py -q 2>&1 \
  | tee /tmp/test-issue-375-adapter.out
uv run pytest cuprum/unittests/test_pipeline_output_options.py -q 2>&1 \
  | tee /tmp/test-issue-375-options.out
uv run pytest cuprum/unittests/test_sinks_end_to_end.py -q 2>&1 \
  | tee /tmp/test-issue-375-e2e.out
```

Expect all three to pass, with the pre-existing snapshot test
`test_framed_workflow_commands_match_the_snapshot` passing unmodified.

Gates, delegated to `scrutineer` (run sequentially, never in parallel):

```sh
make check-fmt
make lint
make typecheck
make test
make markdownlint
make spelling
```

`make markdownlint` matters in addition to `make check-fmt`: per project
experience the local format gates do not run markdownlint, but CI's lint-test
job does and aborts before the dead-code scan.

Then `coderabbit review --agent`, resolving concerns before moving on. Sleep
with `vsleep $(shuf -i 45-90 -n 1)m` if the review endpoint is rate-limited.

## Validation and acceptance

Acceptance is behaviour a human can check:

1. `make test` passes. No pre-existing test is modified except where a new
   parameterization row is added.
2. `RunOutputOptions(echo=True, group=True, annotate_failure=True)` on a GitHub
   Actions runner frames the run and annotates a failure, with no `sink=`
   argument. Observable as `::group::`/`::stop-commands::`/`::endgroup::` and
   at most one `::error` line in the parent's stderr.
3. `RunOutputOptions(echo=True)` produces byte-for-byte the output it produced
   before the change, on every backend and every terminal path.
4. `RunOutputOptions(group=True, sink=my_sink).sink is my_sink`.
5. A pipeline with `group=True` writes exactly one `::group::` line, titled
   `pipeline`.

Red-Green-Refactor evidence is recorded in `Progress` and in the commit
sequence: a test-only commit that fails for the intended reason, then the
implementation commit that turns it green, then a refactor/documentation commit
with the gates green.

Quality criteria — what "done" means:

- **Tests:** `make test` green; every test named in the `Verification plan`
  exists and passes; the GitHub Actions snapshot is unmodified.
- **Verification:** V1–V8 discharged with the evidence named above.
- **Lint/typecheck:** `make lint` and `make typecheck` green.
- **Formatting:** `make check-fmt` green, including `ruff format --check`; note
  that a clean `ruff check` is not evidence for formatting.
- **Markdown:** `make markdownlint` and `make spelling` green.
- **Security:** no new dependency, no new trust boundary, no argv or exception
  text reaches the workflow log. The stop-commands lease continues to shield
  child output.

## Idempotence and recovery

Every step is re-runnable. `make fmt` applies formatting fixes;
`make check-fmt` only reports. If a gate fails, read the log the gate wrote
under `/tmp` before re-running it.

Recovery per milestone: each phase is its own commit. To abandon a phase,
`git reset --hard <previous-commit>`. The `typos.toml` file is regenerated by
the spelling gate; commit the refreshed file as its own commit rather than
reverting it, or the gate dirties the tree again.

## Artefacts and notes

The ADR-013 excerpt this plan reconciles with (its rejected Option B):

```plaintext
### Option B: add `group`/`annotate` flags to `RunOutputOptions`

Teach the execution layer about workflow commands directly.

This spreads Actions-specific formatting across every terminal path of the
runner and pipeline implementations, couples the core to one CI vendor's log
format, and multiplies the places a future presentation change must touch.
```

The reconciliation, which the amendment section must state: the flags are
accepted, but they do not teach the execution layer anything. `__post_init__`
constructs the adapter and stores it in the existing `sink` field, so the
workflow-command strings remain in `cuprum/sinks/github_actions.py` alone and
the execution layer still sees only the protocol. Option B's stated objection
was architectural, not user-facing; the user-facing goal is met without
incurring it.

The adapter's current framing order, which must remain intact:

```plaintext
::group::<program args>
::stop-commands::<token>
<echoed child output>
::<token>::
::endgroup::
::error title=<bounded label>::<timeout|exit_nonzero|error|cancelled>
```

## Interfaces and dependencies

No new libraries. The change uses `cuprum.sinks.github_actions` (already
public), `dataclasses`, and the existing test tooling.

Signatures that exist at the end of `EP-M2`, in
`cuprum/sinks/github_actions.py`. They differ from this plan's first draft,
which was written before the `PLR0913` breach was measured; the
`Surprises & discoveries` entry records why:

```python
@dc.dataclass(frozen=True, slots=True)
class _Annotation:
    label: str
    emit_group: bool = True
    emit_annotation: bool = True


class GitHubActionsSink:
    def __init__(  # ruff: ignore[too-many-arguments] - keyword-only throughout
        self,
        destination: typ.IO[str] | None = None,
        *,
        title: str | None = None,
        force: bool = False,
        emit_group: bool = True,
        emit_annotation: bool = True,
    ) -> None: ...

    def open_session(self, start: SessionStart) -> GitHubActionsSession | None: ...


class GitHubActionsSession:
    def __init__(
        self,
        log: typ.IO[str],
        label: str,
        *,
        annotation_label: str | _Annotation,
    ) -> None: ...

    @property
    def log(self) -> typ.IO[str]: ...

    @property
    def stop_token(self) -> str: ...

    def close(self, outcome: SessionOutcome) -> None: ...
```

Signatures that must exist at the end of `EP-M3`, in `cuprum/sh.py`:

```python
@dc.dataclass(frozen=True, slots=True)
class RunOutputOptions:
    capture: bool = True
    echo: bool = False
    echo_stdout: bool | None = None
    echo_stderr: bool | None = None
    max_echo_line_bytes: int | None = DEFAULT_ECHO_MAX_LINE_BYTES
    on_line: LineHook | None = None
    idle_after: float | None = None
    on_idle: cabc.Callable[[float, float], None] | None = None
    sink: sinks.OutputSink | None = None
    group: bool = False
    annotate_failure: bool = False

    def __post_init__(self) -> None: ...
```

`__post_init__` post-condition, which the tests assert directly: after
construction, `self.sink is None` if and only if no explicit sink was supplied
*and* neither flag was set.

## Milestones and plateaus

- **EP-M0 (complete).** Branch renamed, session titled, confirmation pass done.
  Acceptance evidence: this document's `Surprises & discoveries` names the
  three corrections with file-and-line citations. Conformance check: no code
  changed. Recovery: n/a. Remaining gaps: all production work. Compatibility
  decision: none.

- **EP-M1 (Phase 2+3 combined, because the flags are unobservable until both
  land).** Outcome: `GitHubActionsSink` has independent group/annotation
  toggles defaulting to the current behaviour, and
  `RunOutputOptions(group=True, annotate_failure=True)` synthesizes an
  env-gated adapter into `sink`. Requirements and gaps: ISSUE-375-flags,
  ISSUE-375-aggregation, ISSUE-375-unchanged-defaults. Acceptance evidence:
  V1–V3 and V8 discharged; `make test` green with the existing snapshot
  unmodified. Conformance check: ADR-013 still holds — no workflow-command
  string outside `cuprum/sinks/github_actions.py`; no public signature changed;
  no new dependency, trust boundary, or persisted format. Recovery:
  `git reset --hard` to the Phase-1 commit. Remaining gaps: end-to-end coverage
  and documentation. Compatibility decision: none needed — this is a pre-1.0
  API and every added parameter defaults to the existing behaviour.

- **EP-M2 (Phase 4 tests).** Outcome: V4–V7 discharged with real subprocess
  evidence. Acceptance evidence: the eight new end-to-end tests pass, and
  `test_flags_leave_default_output_unchanged` is the negative control.
  Conformance check: the negative control fails under the intended mutation
  (synthesize unconditionally). Recovery: revert the test commit. Remaining
  gaps: documentation.

- **EP-M3 (Phase 4 documentation).** Outcome: users' guide subsection,
  CHANGELOG bullet, ADR-013 amendment. Acceptance evidence: `make markdownlint`
  and `make spelling` green; `make nixie` unaffected (no new diagrams).
  Conformance check: ADR-013's accepted text is unedited (append-only), and the
  amendment names the Option B reconciliation. Recovery: revert the docs
  commit. Remaining gaps: none.

- **EP-M4 (gates and review).** Outcome: all seven local gates pass after the
  review remediation; CodeRabbit review is pending. Acceptance evidence:
  `scrutineer`'s `-reviewfix2` gate report and the upcoming CodeRabbit verdict.
  Conformance check: annotation-only echo retains the caller's original
  destinations, while `emit_group=False` still writes no group, lease, or
  endgroup as explicitly required. Recovery: address any in-scope finding, then
  rerun the full gates and review. Remaining gaps: CodeRabbit review and
  publication.

## Outcomes & retrospective

To be completed at `EP-M4`.
