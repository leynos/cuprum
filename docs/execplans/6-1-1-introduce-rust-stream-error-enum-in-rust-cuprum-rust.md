# Centralize native stream errors (6.1.1)

Status: IN PROGRESS — M1 implementation. Approved 2026-09-26; the publishing
draft-PR and approval checkboxes below are recorded as done by that approval.

This ExecPlan is a living document. Keep Constraints, Tolerances, Risks,
Progress, Surprises & discoveries, Decision log, Outcomes & retrospective,
Conformance basis, and Verification plan current at every milestone.

## Purpose / big picture

Introduce one typed error boundary for the optional native pump and consume
functions. Callers must continue to receive `ValueError` for invalid buffer
sizes and `OSError` for fatal stream failures, including the existing native
error codes and subclasses. Success is observable through the real compiled
extension as well as through Rust tests of the underlying error values.

This implements only roadmap item 6.1.1. It establishes a foundation for 6.1.2
and the later capture-only dispatcher; it does not integrate native consume,
claim decoding parity over the full domain, or claim a performance win. The 20%
wall-time gate against the tuned phase 5 baseline remains future work.

## Constraints

- Obtain explicit approval of this plan before changing implementation or
  tests. This planning pull request contains documentation only.
- Define `RustStreamError` in `rust/cuprum-rust/src/lib.rs`. Keep safe stream
  policy and its existing `PumpError` in `cuprum-streams`; keep native resource
  operations in `cuprum-native-io`.
- Preserve Python names, argument signatures, defaults, returned values,
  exception messages, subclasses, `errno`, and Windows `winerror`.
- Preserve native validation order: buffer, reader conversion, writer
  conversion for pumping, writer adoption, then detached I/O. The Python
  Windows shim has its own earlier descriptor preparation; do not reorder it.
- Preserve borrowed-reader and consumed-writer ownership, cleanup, cancellation,
  Global Interpreter Lock (GIL) release, and non-fatal downstream closure.
- Keep `rust_consume_stream` implemented but not integrated, including its
  production-use guards. Do not alter dispatch, observation events, metrics,
  UTF-8 decoding, allocation limits, or the safe/native crate boundary.
- Keep Rust 1.85 compatibility, caret dependency requirements, strict lints,
  and the 400-line code-file limit. Do not introduce unsafe code or
  suppressions.
- Run gates sequentially through Makefile targets. Use the shared Cargo cache;
  use `/tmp` only for logs and scratch, never as a build target.

## Tolerances (exception triggers)

Stop implementation, record a proposed deviation, set status to BLOCKED, and
request direction if the work needs a public Python signature or exception
contract change, altered ownership or dispatch, a new production abstraction
outside the integration crate, or changes to more than 18 files excluding
lockfiles and this plan. The planned `thiserror` dependency and Rust
behavioural-test dependencies are allowed; any further dependency requires
review. A Rust behavioural-test release incompatible with Rust 1.85 or the
current `rstest` is a decision gate, not permission to upgrade the toolchain.
There is no time limit. Tool failures do not justify lowering acceptance.

## Risks

- High impact, medium likelihood: replacing the existing OS conversion loses
  machine-readable codes. Retain the existing platform-specific helpers and
  exercise actual Python exception attributes on Unix and Windows.
- High impact, low likelihood: moving validation across writer adoption changes
  resource ownership. Retain operation order and run existing hand-off and
  ownership regressions, including validation-before-adoption witnesses.
- Medium impact, medium likelihood: Cargo tests attempt to link Python symbols.
  The crate enables `pyo3/extension-module`; keep Rust tests interpreter-free
  and assert Python exception objects in extension-required Python tests.
- Medium impact, medium likelihood: an optional-extension test silently skips.
  Rebuild the extension and require `make test-extension`; ordinary pytest
  success without the extension does not discharge the boundary contract.
- Medium impact, low likelihood: a broad refactor consumes later roadmap work.
  Retain the existing stream error taxonomy and limit properties to this
  conversion; full decoding parity remains 6.1.2.

## Progress

- [x] (2026-09-19) Read repository instructions and requested skills; inspected
  the current boundary and roadmap at the revision recorded below.
- [x] (2026-09-19) Created the requested local branch; the remote branch did not
  exist at initial inspection. Publication will establish upstream tracking.
- [x] (2026-09-19) Requested two Wyvern reconnaissance passes and an expert
  community review covering all six Logisphere perspectives.
- [x] (2026-09-19) Reconciled expert findings on OS codes, validation order,
  native versus shim behaviour, and Rust behavioural-test compatibility.
- [x] (2026-09-26) Rebased onto `main` (`991dee64`); branch head `ab58a3d6`.
  Zero conflicts — the branch's only file is one `main` never touched. All five
  referenced Rust files had moved under the rebase, so every plan claim was
  re-verified at the new base: symbol counts unchanged, `checked_buffer_size`
  byte-identical, MSRV still 1.85.0, all 22 referenced paths and 10 relative
  links resolve. `main` bumped `cuprum-streams`'s `rstest` 0.26.1 → 0.27.0, so
  the integration crate's 0.27.0 is now uniform across the workspace.
- [x] (2026-09-26) Validated and published this plan as a draft pull request
  (PR #432).
- [x] (2026-09-26) Obtained explicit implementation approval, with the
  `thiserror` dependency authorized (see Decision log).
- [ ] M1: implement and validate the typed boundary and its tests.
  - [x] (2026-09-26) Typed boundary implemented across `lib.rs`
    (`RustStreamError`, typed `validate_buffer_size`/`convert_fd`),
    `errors.rs` (`impl From<RustStreamError> for PyErr`, old converter demoted),
    and `stream_pyfunctions.rs` (single-conversion boundary).
  - [x] (2026-09-26) V1/V2 red evidence captured: `E0432 unresolved import
    crate::RustStreamError` at three sites.
  - [x] (2026-09-26) V1/V2 green: `make test-rust TEST_FLAGS='-p cuprum-rust
    --all-targets --all-features'` → **51 tests run, 51 passed, 0 skipped**,
    including all four `rstest-bdd` scenarios
    (`stream_error_behaviour::typed_native_stream_failures` and
    `accepts_a_valid_buffer_size::case_1`..`case_3`) and clean doctests.
    Log: `/tmp/611-rust-focused-1.out`.
  - [x] (2026-09-26) `make msrv-check` equivalent green on Rust 1.85.0
    (`cargo +1.85.0 check --workspace --all-targets --all-features`, exit 0,
    warning-free). Logs: `/tmp/611-msrv-check-2.out`.
  - [x] (2026-09-26) V3: extend the Python unit, behavioural, and Hypothesis
    suites. Hypothesis negative bound widened to `i64::MIN`; a new
    `test_buffer_validation_precedes_descriptor_conversion` pins native
    ordering; `test_out_of_i64_buffer_size_stays_an_extraction_error` pins the
    extraction boundary; consume gained `empty_input` and
    `single_byte_minimum_buffer` rows; the new
    `cuprum/unittests/test_rust_stream_native_order.py` calls
    `cuprum._rust_backend_native` **directly** (the shim validates first, so it
    cannot witness native order) and both new modules were added to
    `EXTENSION_TEST_TARGETS`, which `test_extension_build_contract.py` requires.
    Green: **50 passed, 1 skipped** (Windows-only). Log:
    `/tmp/611-v3-green-final.out`.
  - [x] (2026-09-26) V3 mutation evidence. `InvalidBufferSize` was remapped to
    `PyOSError` in `errors.rs`, the extension rebuilt via `make develop`, and
    the suite re-run: **6 tests failed across 4 modules**
    (`test_rust_stream_native_order.py` ×2,
    `test_rust_streams_boundary_property.py` ×2,
    `test_rust_consume_stream.py` ×1,
    `test_rust_streams_errors_behaviour.py` ×1), all reporting
    `expected ValueError, found OSError`. The source was then restored to a
    byte-identical copy and `git diff` confirmed the tree clean. Logs:
    `/tmp/611-mutation-red.out`, `/tmp/611-restore-build.out`, green re-run
    `/tmp/611-v3-green-final.out`.
  - [x] (2026-09-26) V4: ownership and non-fatal write policy confirmed via the
    native gates. `make boundary-test` green — **13 passed** in
    `cuprum-native-io --lib` (including
    `ownership_tests::transferred_writer_closes_and_delivers_eof::case_3_real_unwind`)
    and **116 passed** in `scripts/tests/test_boundary_*.py`. Log:
    `/tmp/611-boundary-test.out`. `make test-extension` green — **101 passed,
    1 skipped** (the Windows-only winerror case), covering the whole
    `EXTENSION_TEST_TARGETS` list including all four new outline rows and the
    new native-order module. Log: `/tmp/611-test-extension.out`. Direct
    ownership witnesses were also observed while probing the compiled module:
    a rejected buffer size leaves the writer **open**, a completed pump leaves
    it **closed**. Windows runtime evidence is still outstanding and must come
    from Windows; `make lint-windows` is a cross-target compile and counts as
    supplementary only.
  - [x] (2026-09-27) Documentation: design guide, developers' guide, users'
    guide. Four edits, each verified against the tree rather than against the
    prose it replaced:
    1. `docs/cuprum-design.md` — new `#### Native error classification`
       subsection closing 13.3, covering the enum's three variants, the single
       `From<RustStreamError> for PyErr` conversion point reached once through
       `run_stream_operation`'s `map_err(PyErr::from)`, and why `Stream` wraps
       `PumpError` rather than copying its variants.
    2. `docs/developers-guide.md` — three paragraphs closing "Building the
       extension for tests", giving the complementary-covers rationale and the
       shim's resource-ownership guarantee.
    3. `docs/users-guide.md` — the buffer/error contract under "Checking the
       native extension", keeping consume described as not integrated.
    4. `docs/developers-guide.md` — five stale references corrected, four of
       which this milestone made stale:
       - `validate_buffer_size(i64) -> PyResult<BufferSize>` "which maps the
         message to `PyValueError`" → it returns
         `Result<BufferSize, RustStreamError>` and the mapping happens in the
         `From` impl (verified at `rust/cuprum-rust/src/lib.rs:49`).
       - "conversion happens in exactly one place, `pump_error_to_py_err`
         … (called from `stream_pyfunctions.rs`)" → the one place is now the
         `From` impl; `pump_error_to_py_err` is private and reached only from
         its `Stream` arm (verified: only two hits repo-wide, both in
         `errors.rs`).
       - Both CI job rows "13 gated modules" → **15**, matching
         `EXTENSION_TEST_TARGETS` exactly (counted, not estimated).
       - Table 1 gained the two new modules.
       - "four scenarios report `Rust extension is not installed`" → **ten**.
         Measured by moving `cuprum/_rust_backend_native.abi3.so` aside and
         running the four gated behaviour modules: 10 skipped, 5 passed, and
         the sha256 was confirmed identical after restoring. The old "four" was
         already wrong before this branch — six skipped at the merge-base —
         and `test_rust_extension_availability` deliberately *passes* either
         way, since it asserts the absent case rather than skipping.
  - [x] (2026-09-27) Post-documentation gate run surfaced two defects of mine,
    both introduced by `727484e2` and both traced to
    `rust/cuprum-rust/src/stream_error_behaviour.rs`. `make test-rust` had been
    run after that commit but `make lint` and the Python formatter ratchet had
    not, so neither defect had been observed. Both are fixed:
    1. **`lint-whitaker` denied 9 × `no_expect_outside_tests`.** The nine
       `.expect(...)` calls were all on `Slot::get()`. Diagnosed against the
       installed lint rather than by inference:
       `crates/no_expect_outside_tests/src/driver/mod.rs` gates
       `is_in_cfg_test_module` behind `is_test_harness`, and whitaker runs
       `cargo check --all-targets`, so the file-backed
       `#[cfg(test)] mod stream_error_behaviour;` declaration never supplies the
       ancestry. `collect_context` reaches only ancestors that are `Node::Item`
       and re-parses the declaration as its own `Mod` item, so the steps read as
       production code. This contradicts the lint's own
       `ui/pass_expect_in_file_backed_test_module.rs`. The skill at
       `whitaker/skills/addressing-whitaker-findings/SKILL.md` names the
       category ("cucumber step functions … `additional_test_attributes` cannot
       help because the macros consume their attributes") and prescribes making
       the function fallible rather than suppressing it. Every step now returns
       `StepResult<(), String>` and the nine `expect` calls are gone; the
       `assert!`/`assert_eq!` in the `Then` steps became `Err` returns because
       `panic_in_result_fn` is denied workspace-wide. A single `recorded`
       accessor replaced the repeated slot read, so an out-of-order scenario
       names the missing value. Same as the sibling `stream_error_tests.rs`,
       which passes this lint with zero `expect` calls.
    2. **The formatter ratchet.** `test_formatter_skips_are_limited_to_known_rstest_fixtures`
       found a fourth `#[rustfmt::skip]` and asserts the set is exactly three,
       with the message "add a mutation proof before extending the formatter
       exception set". The skip was load-bearing, measured rather than assumed:
       removing it makes the pinned formatter collapse the fixture to
       `fn context() -> StreamErrorContext { StreamErrorContext::default() }`,
       and `cargo +1.85.0 check` on that exact form emits
       `unused_braces` at 53:36, which `-D warnings` turns into an error. The
       skip was removed **without extending the ratchet**: giving the fixture
       body a line comment of its own keeps it multi-line, so the formatter and
       Rust 1.85 are both satisfied and the exception set stays at three. This
       is the outcome the ratchet's assertion message is asking for — the
       preferred fix is not to grow the set.
    3. **`make test`'s 13 failures are environmental, not mine.** They come from
       Lody's `BASH_ENV=/home/leynos/.lody/bashenv`, which re-prepends
       `~/.lody/bin` inside every `bash -c`, so the fake `gh` stand-in in
       `tmp_path/tools` is shadowed by the real `gh` and the release tests fail
       with `failed to run git: fatal: not a git repository`. The fix
       (`ccd9d1df`, "Scrub BASH_ENV from the workflow-step helpers") is **not an
       ancestor of this branch and not on `origin/main`**. Re-running with
       `env -u BASH_ENV make test` is the workaround; no tracked test helper is
       changed for it.
  - [x] (2026-09-27) Pure-Python gate block green at head `6a28ff95`, tree
    `bdc25f22` — both identical before and after every gate, and the tree clean
    when the gates finished (`typos.toml` unchanged, so no Stop-hook churn to
    commit). **Provenance caveat, raised by the gate runner and correct:** these
    two ledger entries were written at 01:31:28, *after* the five-gate block
    ended at 01:24 and *during* the three-gate follow-up, so this document was
    itself the working-tree dirt visible to the second block. The probe is
    meaningful and is kept: the modification is docs-only and the affected doc
    contains zero Mermaid blocks, so neither block's verdict changed and both
    tree hashes held. But the consequence is that **the logs under
    `/tmp/gate-611-*.out`, not this document, are the stable citation** for
    those verdicts. Five of the plan's eight required gates were run; logs under
    `/tmp/gate-611-*.out`:
    `make check-fmt` PASS (675 formatted, 78 unchanged by mdtablefix),
    `make markdownlint` PASS (78 files, 0 errors), `make spelling` PASS (no
    findings), `env -u BASH_ENV make test` PASS (exit 0), and `make lint`
    PASS except its last sub-check. `make lint` is
    `python-lint rust-lint github-actions-lint`; `python-lint` passed in full
    (ruff, interrogate 100%, pylint 10.00/10, df12 lints, ambrleaks, skylos) and
    `rust-lint` passed in full (rustdoc, `clippy --all-targets --all-features
    -D warnings`, **`lint-whitaker`**, yamllint), so the whitaker fix this
    milestone turns on is green under the repository's own invocation, not only
    under the scoped probe. The abort is the final `actionlint` step, which
    deadlocked in the known local shellcheck/stdin race; the bounded read-only
    diagnostic `timeout 90s actionlint -shellcheck= -config-file
    .github/actionlint.yaml` returned **exit 0 with zero output**, and `.github`
    is byte-identical to `origin/main`, so no workflow finding is being masked.
    Treated as locally unobservable, not failed (see the Surprises entry).
  - [x] (2026-09-27) Test counts from that run, quoted per toolchain rather than
    summed: nextest `Summary [ 6.776s] 154 tests run: 154 passed, 0 skipped`,
    including trybuild's `compile_tests::compile_time_ui`; doctests
    `0 passed; 0 failed; 3 ignored`; pytest targets `2467 passed, 77 skipped`,
    `638 passed`, `2 passed`, `116 passed`, `4 passed`, `123 passed`,
    `12 passed, 7 skipped`, `21 passed, 13 skipped`, `22 passed`. The 77 skips
    are the parked-extension skips implied by this stage's isolation, 33 of them
    this branch's own Python boundary tests — which is precisely why the native
    stage below is required and why those Python-side behaviours are recorded as
    unobserved in this block.
  - [x] (2026-09-27) The plan's remaining three pure-Python gates green at the
    same head and tree, run as a separate block after the two entries above were
    already on disk: `make typecheck` PASS (`ty 0.0.74`, "All checks passed!",
    exit 0), `make nixie` PASS ("All diagrams validated successfully!", exit 0,
    all 78 docs scanned), `make msrv-check` PASS
    (`RUSTUP_TOOLCHAIN=1.85.0 cargo check --workspace --all-targets
    --all-features`, exit 0). Logs: `/tmp/gate-611-typecheck.out`,
    `/tmp/gate-611-nixie.out`, `/tmp/gate-611-msrv-check.out`. That completes
    all eight gates of the plan's pure-Python sequence at one head.
    `make msrv-check` returned in 1.26s, which is fast enough to be worth
    refusing on exit code alone, so the pass was corroborated: the verbose run
    reports all three workspace crates **`Fresh`** under `RUSTUP_TOOLCHAIN=1.85.0`
    and `textwrap v0.16.2`, and cargo folds the rustc version into its
    fingerprints, so a nightly-built artefact could not have been reused here.
    The preconditions behind the lock decision also still hold — `rustc 1.85.0`
    installed, workspace `rust-version = "1.85.0"`, zero `icu_*` crates in
    `rust/Cargo.lock` — so the "requires rustc 1.88" failure mode did not arise
    and the `textwrap` pin was not disturbed. Diagnostic log:
    `/tmp/gate-611-msrv-check-verbose-diagnostic.out`.
  - [x] (2026-09-27) Native extension stage green, at head `6a28ff95` (ledger
    commit `9e01003c` adds no code, so the head that matters is unchanged).
    `make develop` exit 0 — rebuilt rather than reinstated, because a stale
    fallback is the hazard the milestone exists to remove. The build reports
    `abi3-py3.12` and installs `cuprum._rust_backend_native`; the import check
    resolves to `cuprum/_rust_backend_native.abi3.so` in the worktree and the
    module exports exactly `is_available`, `rust_consume_stream`, and
    `rust_pump_stream`. The parked copy was provably the same production code
    (only the `#[cfg(test)]` behaviour module changed after it was built), but
    it is now superseded and is retained only as the pre-build fallback.
    `make test-extension` exit 0 — **101 passed, 1 skipped** (the Windows-only
    winerror case), which is V4's recorded native-boundary result and, crucially,
    the moment the 33 Python tests skipped in the pure-Python block actually
    execute: the four new outline rows and
    `cuprum/unittests/test_rust_stream_native_order.py` are no longer unobserved.
    `make boundary-test` exit 0 — **13 passed** in `cuprum-native-io --lib` plus
    **116 passed** in `scripts/tests/test_boundary_*.py`, both unchanged from
    V4, so the diff did not disturb ownership or the native boundary. Logs:
    `/tmp/611-develop.out`, `/tmp/611-test-extension.out`,
    `/tmp/611-boundary-test.out`.
  - [x] (2026-09-27) `make lint-windows` exit 0 — the Windows `cfg` branches of
    the typed boundary compile for `x86_64-pc-windows-msvc` (`PYO3_CROSS_PYTHON_VERSION=3.13`,
    4m25s, "Finished dev profile"). Recorded with the plan's own qualifier
    attached: this is a cross-target **compile** and counts as supplementary
    evidence only. It proves the `InvalidDescriptor` path and the Windows
    descriptor conversion compile under `cfg(windows)`; it does **not** prove
    Windows runtime behaviour, and the winerror case remains skipped. Windows
    runtime evidence is still outstanding and must come from Windows.
    Log: `/tmp/611-lint-windows.out`.
  - [ ] Full gate sequence plus native extension stage; one gated atomic commit.
- [ ] M2: reconcile documentation, complete platform evidence, and mark 6.1.1
      done.

## Surprises & discoveries

**Red evidence was captured after the fact, not before the edit.** The plan
requires the new Rust tests' *initial failure* to identify the absent enum or
typed result. The first build attempt was blocked by the lockfile/MSRV problem
recorded in the Decision log, so the tests were first compiled only once the
implementation already existed. The red state was therefore reconstructed
deliberately: HEAD's three production files were restored while keeping the new
test modules declared, and the failure was captured. It reported exactly
`error[E0432]: unresolved import crate::RustStreamError` (three sites:
`stream_error_behaviour.rs:19`, `stream_error_tests.rs:13` and `:243`) — the
absent typed contract, not a missing dependency. The green files were then
restored and re-verified. This is recorded as a deviation in method, not in
outcome: the evidence is real and reproducible, but it does not prove the tests
were written against a genuinely unimplemented API, because they were not.

The task's original location predates the current three-crate split. At the
planning revision, `lib.rs` contains argument validation, while
`stream_pyfunctions.rs::run_stream_operation` already centralizes preparation,
detachment, and the `PumpError` conversion. `errors.rs` already preserves
native OS codes. The missing piece is a typed error unifying argument and
stream failures, not a new stream engine.

`cuprum/unittests/test_rust_streams_boundary_property.py` already contains
Hypothesis boundary coverage. Reuse it instead of creating a second generator
suite. Its Windows exclusions reflect shim preparation order and must not be
removed on the assumption that native and shim ordering are identical.

Two attempts to create a context pack failed because the server rejected an
existing pack larger than its 524288-byte limit. Planning agents exchanged
bounded repository paths and evidence instead; no shared pack was deleted.

**A new extension-gated test module is not merely uncovered — the suite fails
until it is declared.** `cuprum/unittests/test_extension_build_contract.py`
derives the gated modules from the test tree itself (scanning for the
`rust_streams` fixture, the shared skip reason, and the literal
`_rust_backend_native`) and asserts each one appears in the Makefile's
`EXTENSION_TEST_TARGETS`. Adding the new behaviour module without touching the
Makefile therefore failed a *pre-existing* gate with "these test modules gate
on the compiled extension but are not in the Makefile's
EXTENSION_TEST_TARGETS". This is a useful property and was not anticipated by
the plan: it means the "skip silently unless the extension is present" hazard
the plan warns about is already mechanically guarded for any new module, rather
than depending on the author to remember. Both
`test_rust_stream_native_order.py` and `test_rust_streams_errors_behaviour.py`
were added to that variable.

**Prose counts decay, and two of the guide's were not merely stale but wrong
about a claim they were supposed to make checkable.** The plan requires
"accurate documentation", which for a numeric claim means re-measuring it. Two
were corrected against the tree: the gated-module count (13 → 15, matching
`EXTENSION_TEST_TARGETS`) and the absent-extension scenario count, which the
guide offers as a verification recipe — "confirm with `pytest -rs` … four
scenarios report `Rust extension is not installed`". That recipe reported ten.
The old figure was already wrong at the merge-base, where six skip, so this was
a pre-existing documentation defect rather than drift this branch introduced.
`test_rust_extension_availability` is the interesting case: it *passes* without
the extension, because its `Then` step returns early on a missing module
instead of skipping, so a naive grep of skip output undercounts the modules
that genuinely exercise the absent path.

**The users' guide's own buffer-size sentence was falsified by measurement.**
The first draft read "anything outside that window raises `ValueError`". A
probe of the public shim across the domain shows that is false at the
boundaries: `i64::MAX + 1` and `2**64` raise `OverflowError`, because
`_validate_buffer_size_before_writer_transfer` checks the `i64` range *before*
the positivity and cap checks, and those values never reach the size window at
all. The measured table is: `0` and `-1` → `ValueError`; `1` and `1 GiB` →
accepted; `cap + 1` and `i64::MAX` → `ValueError`; `i64::MAX + 1` and `2**64` →
`OverflowError`. The guide now states the `i64` boundary separately. This
matches the two tests added in V3
(`test_out_of_i64_buffer_size_stays_an_extraction_error` in the boundary
property and `test_out_of_i64_buffer_keeps_pyo3_overflow_error` in the
native-order module), so the prose and the pinned contract now agree at all
four edges rather than only the interior ones.

**Separating the behaviour module was necessary for a line-count constraint,
not a design preference.** The four new outline rows pushed
`tests/behaviour/test_rust_streams_behaviour.py` to 411 lines, past the
400-line ceiling in AGENTS.md, so the failure-contract scenario moved to
`test_rust_streams_errors_behaviour.py`. The split is by subject — transport
behaviour stays, failure classification moves — rather than by line number.

**A local tool deadlock must be classified, not either retried or believed.**
`make lint` ends in `github-actions-lint`, and on this host `actionlint 1.7.12`
invoked without `-shellcheck=` hangs indefinitely in the shellcheck stdin write
— a pipe-buffer race, not a deterministic failure. The gate therefore *aborts*
on a step that has nothing to do with the diff. Two pieces of separation
evidence let it be classified rather than guessed at: the branch touches no
path under `.github/` (the directory is byte-identical to `origin/main`), and a
bounded read-only
`actionlint -shellcheck= -config-file .github/actionlint.yaml` returns exit 0
with zero output in under a second. The rule this suggests for future runs:
when a gate aborts in a step outside the change surface, measure the step in
isolation and record it as *unobserved locally* — do not record it as passed on
the strength of the diagnostic, and do not re-run the form that hangs. The
abort also has an ordering consequence worth stating plainly: because
`github-actions-lint` is the last prerequisite of `lint`, the sub-checks that
carry this milestone's risk — `lint-whitaker` above all — had already completed
when the abort happened, so their pass is real evidence rather than something
the abort left unobserved. The converse is the trap
`aborting-gate-leaves-later-checks-unobserved` warns about, and it does not
apply here only because the aborting step is last.

## Decision log

- (2026-09-26) **The `rstest-bdd` dev-dependency must be locked at
  `textwrap 0.16.2`, not re-resolved freely.** Adding `rstest-bdd` pulls in
  `gherkin`, which depends on `textwrap`; `textwrap 0.16.4` newly depends on
  `icu_segmenter`, whose 2.3.0 release requires Rust 1.88. The workspace
  declares `resolver = "2"`, which is **not** MSRV-aware, so any re-resolution
  that is free to move `textwrap` picks `0.16.4` and breaks `make msrv-check`
  with eleven "requires rustc 1.88" errors (`icu_collections`,
  `icu_locale_core`, `icu_locale_fallback`, `icu_locale_fallback_data`,
  `icu_provider`, `icu_segmenter`, `icu_segmenter_data`, `textwrap`, and
  duplicates). The fix is `cargo +1.85.0 update textwrap --precise 0.16.2`,
  which cascades the entire `icu_*` subtree back out of the lock. This is not a
  workaround but the correct resolution: a scratch crate on `edition = "2024"`
  (which implies resolver 3, MSRV-aware) independently selects
  `textwrap 0.16.2` for the same edge. Contributors who regenerate the lock for
  any reason must keep `textwrap` pinned, and `make msrv-check` is the gate
  that catches it. The verdict on the plan's tolerance clause is that no
  behavioural-test release is incompatible: `rstest-bdd` 0.5.0 itself compiles
  and runs on 1.85.0, and the toolchain stays untouched.
- (2026-09-26) **A single-expression `#[fixture]` body trips
  `unused_braces` under `-D warnings`; use the multi-line block form.** The
  `rstest` `#[fixture]` macro re-emits a single-expression function body as a
  nested block, so `fn context() -> Ctx { Ctx::default() }` becomes redundant
  inner braces and fails the workspace's `RUST_FLAGS = -D warnings`. Plain
  `rustc` accepts the same one-liner, so the failure is macro-induced and does
  not reproduce without `rstest`. Verified minimally: single-expression bodies
  fail, multi-line blocks pass. The repository's existing fixtures already use
  the multi-line form (`cuprum-native-io/src/ownership_tests.rs`,
  `cuprum-streams/src/io_utils/tests.rs`), so this is house style rather than a
  new constraint. Note that cargo's own suggested fix for this lint is
  syntactically invalid; ignore it. `#[allow]` would also silence the lint but
  the plan forbids new suppressions.
- (2026-09-26) **`thiserror` is an authorized dependency.** The plan's
  Interfaces section said to "reuse the existing `thiserror = "2.0.18"`
  requirement in the integration crate's manifest". That requirement does
  **not** exist: `thiserror` is declared only in `cuprum-streams`, the
  workspace has no `[workspace.dependencies]` table, and `cuprum-streams` does
  not re-export it. The inaccuracy is **not** rebase-induced — it is equally
  false at the plan's own baseline `861fe2f0`. The dependency is therefore
  **added** to `rust/cuprum-rust/Cargo.toml`, at the same `"2.0.18"` caret
  requirement the streams crate uses, and the user has authorized it explicitly.
  `rust/Cargo.lock` already pins `thiserror` 2.0.20, so the new declaration
  resolves to a version already in the graph and adds no new crate, no new
  code, and no version change.
- (2026-09-26) **`rstest-bdd` 0.5.0 is verified compatible with Rust 1.85 and
  `rstest` 0.27.0, resolving the plan's last stated implementation-time
  uncertainty.** This was verified by experiment in a scratch crate outside the
  repository, not inferred: `cargo generate-lockfile` on
  `RUSTUP_TOOLCHAIN=1.85.0` resolved and locked `rstest-bdd` and
  `rstest-bdd-macros` at 0.5.0 (reporting 0.6.0 as "requires Rust 1.88"),
  `cargo build --all-targets` compiled the whole tree in 11.6 s, and a real
  scenario — an internal `#[cfg(test)]` module driving a `features/*.feature`
  file via `#[scenario]` — ran green under `rstest` 0.27.0. The compatibility
  concern in the plan and in the "Expert review reconciliation" section is
  therefore discharged; the toolchain is not touched.
- (2026-09-26) **`rstest-bdd` step patterns use brace placeholders.** A step
  defined as `#[given("a buffer size of <size>")]` compiles but never matches;
  the correct form is `#[given("a buffer size of {size}")]`. The angle-bracket
  form is the *feature-file* placeholder, and the value is substituted into the
  step text before matching, so the pattern must carry the brace form. Verified
  by experiment: the angle-bracket pattern failed with "Step not found at index
  0: Given a buffer size of 1".
- (2026-09-26) **Outline columns bind to the `#[scenario]` function's
  parameters, not the step functions'.**
  `#[scenario] fn probe(ctx: Ctx, size: isize)` is what makes an `<size>`
  column resolvable; declaring it only on the step function fails with
  "parameter `size` not found for scenario outline column. Available
  parameters: [ctx]". `isize` is the placeholder type to prefer for buffer
  sizes, since `1073741824` (the 1 GiB cap) exceeds `i32`.

- (2026-09-19) Place a crate-private integration enum in `lib.rs`, wrapping
  `PumpError` rather than copying its variants. This preserves ADR-011's
  ownership of safe policy and avoids parallel taxonomies.
- (2026-09-19) Use one `From<RustStreamError> for PyErr` implementation in
  `errors.rs` and one invocation at the common stream-operation boundary.
  Existing OS conversion helpers remain private implementation details.
- (2026-09-19) Retain Python exception construction in the host interpreter.
  Pure Rust tests verify typed values; Python tests verify conversion through
  real entry points. No test-only Python export or embedded interpreter is
  required.
- (2026-09-19) Treat this as a behaviour-preserving boundary consolidation.
  Amend the relevant design section during implementation; a new ADR is not
  needed unless evidence changes the accepted architecture.
- (2026-09-26) **Treat `thiserror` as an authorized dependency for this
  work.** The user authorized it explicitly, so `cuprum-rust` adds
  `thiserror = "2.0.18"` rather than hand-writing the `Display` and `Error`
  impls. This is a scope decision, not a supply-chain one: `thiserror` is
  already in the workspace via `cuprum-streams`, the lockfile is unchanged by
  the addition, and no new transitive crate enters the graph. The derived
  messages are load-bearing — the Python boundary matches on the variant rather
  than the text, but the text is what a caller reads, and `#[error("{0}")]`
  keeps the validator's own stable message as the single source.
- (2026-09-26) **The pump shim, not the native boundary, answers a buffer
  failure in normal use — and that is correct, not a defect.** `make develop`
  builds the extension into the dev venv, and `cuprum._streams_rs` validates
  `buffer_size` before calling Rust precisely so that a writer never reaches
  the native ownership boundary on a rejected size. The consequence found while
  writing V3 is that a *pump* mutation remapping `InvalidBufferSize` to
  `OSError` is invisible through the shim: the shim raises `ValueError` first
  and the mutated conversion never runs. Only direct native calls and the
  consume path (which has no writer and so no pre-adoption check) surface it.
  This is why `cuprum/unittests/test_rust_stream_native_order.py` calls
  `cuprum._rust_backend_native` directly: the plan requires native-order checks
  to call the compiled module, and the mutation evidence above confirms the
  distinction is real rather than stylistic. The shim's check is a
  resource-ownership guarantee; the native check is the typed-boundary
  guarantee. Both are wanted, and neither test can stand in for the other.
- (2026-09-26) **A pre-existing `os.close` overflow hazard was found and left
  alone.** While adding the precedence property, the pump arm failed with
  `OverflowError: Python int too large to convert to C int` — not from the
  boundary, but from `_close_writer_after_pre_native_failure` calling
  `os.close(writer_fd)` on a descriptor outside the C `int` range. `os.close`
  raises `OverflowError`, which the helper's `contextlib.suppress(OSError)`
  does not catch, so the real `ValueError` is masked. The hazard is
  **pre-existing**: `git show origin/main:cuprum/_streams_rs.py` contains the
  same helper unchanged, and
  `git diff origin/main...HEAD -- cuprum/_streams_rs.py` is empty. It is out of
  6.1.1's scope — this plan changes the Rust boundary, not the shim's
  descriptor handling — so it is recorded here rather than fixed here. The test
  that tripped it was corrected instead: it had passed the out-of-range value
  as the pump's *writer* as well as its reader, which is not a realistic call
  shape. It now opens a genuinely valid writer, matching the existing
  `test_pump_rejects_invalid_reader_descriptor`.

## Outcomes & retrospective

Implementation has not begun. The plan targets one focused boundary change; no
roadmap checkbox is completed by publishing it. Record actual red and green
test evidence, gate logs, platform results, and deviations here during delivery.

## Context and orientation

`rust/cuprum-rust/src/lib.rs` defines `validate_buffer_size`, `convert_fd`, and
platform-specific integer-to-descriptor conversion. Both public native exports
live in `rust/cuprum-rust/src/stream_pyfunctions.rs` and use
`run_stream_operation`. That helper validates before transferring the writer,
releases the GIL with `py.detach`, then translates the safe operation's error.

`rust/cuprum-rust/src/errors.rs` translates `PumpError`, retaining raw
operating system codes and stripping Rust's duplicate OS-code suffix. It also
contains pure tests. `rust/cuprum-streams/src/errors.rs` owns `PumpError` and
its non-fatal-write predicate. `BufferSize::new` in that crate's `src/lib.rs`
accepts 1 through 1073741824 bytes and returns stable validation messages.

`cuprum/_streams_rs.py` is the Python shim. Existing unit coverage includes
`cuprum/unittests/test_rust_streams.py`, `test_rust_consume_stream.py`,
`test_rust_streams_boundary_property.py`, `test_rust_errno.py`, and
`test_rust_errno_windows.py`. Behavioural coverage uses
`tests/features/rust_streams.feature` and
`tests/behaviour/test_rust_streams_behaviour.py`. Native consume's inert status
is protected by `cuprum/unittests/test_rust_consume_integration_guard.py`.

## Conformance basis

The repository baseline is commit `861fe2f053645311482141f155baeaa70dca0299`.
There is no separate terms of reference for this task. The governing sources at
that revision are:

- [Roadmap](../roadmap.md), item 6.1.1: typed error and single Python
  conversion.
- [Cuprum design](../cuprum-design.md), section 13: native error propagation,
  backend selection, and descriptor lifecycle.
- [ADR-002](../adr-002-additional-rust-components.md): behavioural reference,
  decoding and descriptor risks; its overall status remains Proposed.
- [ADR-008](../adr-008-rust-pump-observation-channel.md): separate observation
  channel, bounded labels, and cancellation cleanup contracts.
- [ADR-011](../adr-011-audited-rust-boundaries.md) and
  [boundary verification](../rust-boundary-verification.md): the current safe
  policy, native resource, and PyO3 integration boundaries.
- [Profiling baseline](../tee-hotpath-profiling-baseline-2026-06-12.md),
  hypothesis verdicts 3–4: motivation, not performance evidence for this change.
- [Developers' guide](../developers-guide.md), [users' guide](../users-guide.md),
  [documentation style](../documentation-style-guide.md), `AGENTS.md`, and
  `.rules/python-*.md`, especially exception handling, typing, and core style.

Trace R1 (6.1.1 error categories and centralization) through M1 to V1–V3 below.
Trace R2 (ADR-002/011 ownership and native error fidelity) through M1 to V3–V4.
Trace R3 (scope and documentation accuracy) through M2 to the native-consume
integration guard, unchanged observation contracts, and the documentation diff.
Recheck these links at both milestone boundaries.

## Interfaces and dependencies

Define a crate-private `RustStreamError` in `lib.rs`, deriving `Debug` and
`thiserror::Error`, with these variants:

```rust
enum RustStreamError {
    InvalidBufferSize(&'static str),
    InvalidDescriptor(&'static str),
    Stream(PumpError),
}
```

The snippet shows the shape; add `#[error("{0}")]` to the argument variants,
`#[error(transparent)]` to `Stream`, and `#[from]` to its payload. Reuse the
existing `thiserror = "2.0.18"` requirement in the integration crate's manifest
and update `rust/Cargo.lock` without unrelated upgrades.

Change `validate_buffer_size` and `convert_fd` to return
`Result<_, RustStreamError>`, preserving `convert_platform_fd` and its string
contract. Convert messages to the appropriate enum variant. The preparation
closure returns `Result<Operation, RustStreamError>`. Keep the detached
operation's `Result<T, PumpError>` and convert it to the wrapper after detach.
A typed inner closure in `run_stream_operation` performs the existing sequence;
its result is converted once with `map_err(PyErr::from)`. Public pyfunctions
retain `PyResult<u64>` and `PyResult<String>`.

Implement `From<RustStreamError> for PyErr` in `errors.rs`: argument variants
become `PyValueError`; `Stream` exhaustively matches existing `PumpError`
variants and reuses the raw-code/synthetic-I/O conversion logic. Remove the old
`pump_error_to_py_err` entry point rather than retain an alias. Keep the
module-initialization errors and PyO3 argument-extraction errors outside this
stream-domain conversion. No new Python exception class is introduced.

Add `rstest-bdd = "0.5.0"` and `rstest-bdd-macros = "0.5.0"` as dev
dependencies. Their declared minimum Rust version is 1.85; version 0.6.0
requires 1.88 and is excluded. Version 0.5.0 uses `rstest` 0.26.1 in its own
tests, so verify compatibility with this repository's 0.27 using the focused
Rust command and `make msrv-check` before completing M1. If incompatible, stop
at the dependency tolerance rather than upgrade the toolchain. Keep behavioural
tests in an internal test module to access the private enum without widening
its visibility. Record the selected versions and evidence before M1.

## Verification plan

V1: argument validation retains its full signed-64-bit input classification.
Extend pure `rstest` tests in a new
`rust/cuprum-rust/src/stream_error_tests.rs`, registered under `cfg(test)`.
Assert typed variants and existing platform-specific messages for zero,
negative sizes, cap plus one, `i64::MIN`, and `i64::MAX`; use pure validation
for accepted cap values to avoid allocating a gigabyte. Preserve the 32-bit
`usize` overflow message rather than assuming every oversized value reaches the
cap check. Extend proptest across `any::<i64>()` and explicit boundary
witnesses. Preserve existing descriptor properties in `fd_tests.rs`. Every
invalid buffer maps to `InvalidBufferSize`, and descriptor conversion failures
map to `InvalidDescriptor`. A seeded wrong variant must fail the tests. This
checks a forwarding invariant, not a new size policy.

V2: `Stream(PumpError)` preserves its source and existing semantic variants. Use
`rstest` cases for all four `PumpError` variants, synthesized I/O kinds, and a
real raw OS code; inspect the wrapped source without an interpreter. Add Rust
behavioural scenarios in
`rust/cuprum-rust/tests/features/stream_errors.feature`, bound by an internal
`src/stream_error_behaviour.rs` module using `rstest-bdd`. Reuse production
validators and conversions, not a test-only classifier. A mutation discarding
an OS code must fail a source-retention assertion.

V3: the real Python boundary raises the required classes and retains codes.
Extend the existing pytest unit and pytest-bdd modules named above, and reuse
the existing Hypothesis suite, extending its negative lower bound from
`-(1 << 62)` to `i64::MIN`. Check both entry points, native validation
precedence, valid empty input and a short payload, fatal reader I/O, and
existing errno/winerror/subclass assertions. Call the compiled native module
for native-order checks; use platform-aware wrapper fixtures for shim checks.
An invalid buffer and invalid reader together must report the buffer error
without adopting a writer. Integer extraction outside `i64` and wrong Python
types retain PyO3's existing `OverflowError`/`TypeError` behaviour and are not
reclassified as stream validation errors. The native suite must run without
extension-absence skips. A mutation mapping invalid buffers to `OSError` must
fail a real-extension assertion after rebuilding.

V4: resource ownership and non-fatal write policy remain intact. Reuse existing
writer-transfer, reader-validation, debug-abort, and native boundary tests. Use
owned test pipes and explicit transfer bookkeeping, never arbitrary positive
descriptor numbers or a double-close cleanup. Broken pipe/reset on the
downstream write side must still drain and succeed; a fatal read failure must
still raise. Exercise native cleanup with `make boundary-test` and the
extension-required suite. Windows runtime evidence must come from Windows; a
cross-target compile is supplementary evidence only.

The trusted external assumptions are PyO3's `From<E> for PyErr` contract,
CPython's platform-specific `OSError` construction, OS descriptor semantics,
and the existing safe stream engine. Exercise integration with those
interfaces; do not claim to prove their internals. No new arithmetic, decoding,
ownership algorithm, axiom, or business-logic lemma is introduced. Therefore no
new Verus or Kani model is justified for this forwarding change. Retain
existing proofs; if implementation introduces such logic, revise the plan
before proceeding and require a substantive production-linked proof rather than
a duplicate model.

No new output format is introduced: exact stable message assertions and
semantic exception-attribute checks are sufficient. Retain existing consume
snapshots; do not snapshot platform-localized OS messages or whole tracebacks.
If review uncovers a new multivariant output contract, add focused `insta` or
`syrupy` snapshots with semantic assertions and nondeterministic data redacted.
Properties provide sampled evidence, not exhaustive proofs; record seeds and
ensure invalid/valid classes have explicit witnesses without heavy filtering.

## Behavioural specification

The Rust feature describes typed errors without creating Python objects:

```gherkin
Feature: Typed native stream failures
  Scenario: Reject an invalid buffer before stream preparation
    Given a buffer size of zero
    When the native buffer validator checks the size
    Then the error is InvalidBufferSize
    And its message is "buffer_size must be greater than zero"

  Scenario: Retain a native I/O failure
    Given a stream I/O error with a platform error code
    When it becomes a RustStreamError
    Then the stream error retains the original platform error code
```

Extend `tests/features/rust_streams.feature` with the following outline, bound
in `tests/behaviour/test_rust_streams_behaviour.py`. Build fixtures from
existing pipe ownership helpers and keep tests bounded so they cannot block on
a writer left open.

```gherkin
Scenario Outline: Preserve native stream exception categories
  Given the compiled Rust backend is required
  When the <operation> native helper receives <failure>
  Then it raises <exception>

  Examples:
    | operation | failure             | exception  |
    | pump      | a zero buffer size  | ValueError |
    | consume   | a zero buffer size  | ValueError |
    | pump      | a fatal reader error | OSError   |
    | consume   | a fatal reader error | OSError   |
```

## Plan of work and milestones

### M1: deliver the typed boundary and executable contract

After approval, first run focused existing tests for a baseline. Add the pure
Rust tests and behavioural scenarios before production edits. Their initial
failure must identify the absent enum or typed result, not missing
dependencies. Existing Python behaviour may already pass: record it as
characterization, not invented red evidence. Add any missing native contract
regressions and record their baseline outcomes.

Implement the enum, typed validators, common boundary, and converter together.
Remove the obsolete converter and update all affected callers atomically. Run
pure focused tests and their negative controls first. Follow the ordered
full-gate and native-build sequence below; exercise native negative controls
only in the final extension stage, restore them, rebuild, and rerun affected
native checks before committing. Keep test files below 400 lines by moving
cohesive existing test sections if necessary; no unrelated refactor.

Document the internal enum and its reuse scope in the error-propagation section
of `docs/cuprum-design.md`. Explain in `docs/developers-guide.md` why pure Rust
checks and extension-required Python checks are complementary. Clarify the
existing buffer/error contract in the native-backend section of
`docs/users-guide.md`, keeping consume described as not integrated.

The M1 plateau is a passing implementation, tests, and accurate documentation
in one gated atomic commit. R1 and R2 are discharged by V1–V4 on available
platforms; outstanding Windows evidence must remain explicit. Review the diff
against ADR-011 and the ownership sequence before committing. No compatibility
shim or additional public interface is needed. Failed red tests are transient
working-tree evidence, never committed as a broken plateau.

### M2: close evidence and reconcile the roadmap

Obtain remaining platform results, review changed and adjacent code for
cohesion, and reconcile every upstream reference and decision. Run final gates
on the final implementation state. Only after all required evidence passes, use
the `mapsplice` skill to mark exactly 6.1.1 `[x]` in `docs/roadmap.md`. Leave
6.1.2 and later items open. Update this plan's progress, outcome, and status to
COMPLETE, record evidence and remaining phase-6 scope, then gate and commit the
documentation closeout. If design changes prove necessary, record a deviation
and obtain approval rather than silently widening scope.

## Concrete steps and acceptance

Run from the repository root. Load `codegraph-mcp`, `python-router`,
`rust-router`, and `execplans`; the relevant routed skills are `rust-errors`,
`python-errors-and-logging`, `rust-unit-testing`, and `python-testing`. Use
`firecrawl-mcp` for external documentation gaps, `logisphere-experts` for
design review, and `commit-message`/`pr-creation` for publication. Read
`AGENTS.md`, the documentation index, and `.rules/python-00.md` plus the
exception and typing rules before edits. Use `codegraph` for symbol relations.

Scrutineer runs gates sequentially and logs each with `set -o pipefail` and
`tee` under `/tmp`; use a branch-specific log name. For example:

```bash
set -o pipefail
make test-rust TEST_FLAGS='-p cuprum-rust --all-targets --all-features' 2>&1 | tee /tmp/611-rust-focused.out
make test-python PYTEST_TARGETS='cuprum/unittests/test_rust_consume_integration_guard.py' 2>&1 | tee /tmp/611-guard.out
```

The focused Rust command includes the internal unit, property, and behavioural
modules. Before green implementation it must fail because the new typed
contract is absent; afterwards it must pass. Record the actual test names and
counts rather than predicting them.

Before each implementation commit, run the full sequence below in this order.
The first block must use a pure-Python environment without the extension. If
the current environment already contains it, create a task-owned environment
under the worktree, outside `/tmp`, and set `UV_PROJECT_ENVIRONMENT` to that
absolute path for the whole sequence. Do not clear another session's
environment. Verify absence by calling `importlib.util.find_spec` for
`cuprum._rust_backend_native`; it must return `None` before broad tests. If a
source-tree shared library remains visible, stop and resolve the environment
isolation before running broad tests; do not delete an unowned library.

```bash
make fmt
make check-fmt
make lint
make typecheck
make test
make markdownlint
make nixie
make msrv-check
```

Only after the pure-Python gates pass, build and validate native integration:

```bash
set -o pipefail
make develop 2>&1 | tee /tmp/611-develop.out
make test-extension 2>&1 | tee /tmp/611-extension.out
make boundary-test 2>&1 | tee /tmp/611-boundary.out
make lint-windows 2>&1 | tee /tmp/611-windows-compile.out
```

A successful extension build and passing, non-skipped relevant native tests are
required. Rebuild after every native change. Do not subsequently run the broad
Python suite in this native environment; start the sequence with a fresh
pure-Python environment when necessary. Preserve the standard Rust toolchain
for release and verification; use dev-fast only after its prerequisite check.

Capture each command separately with the logging pattern above and preserve its
exit status. Inspect formatter/generated-file changes immediately; retain only
intended changes. Native build and extension checks follow the full pure-Python
sequence. Documentation-only commits need `make fmt`, `make markdownlint`
(including spelling), and `make nixie`, plus diff review.

Acceptance requires one stream-domain conversion entry point in `errors.rs`, no
PyErr construction in argument validators or stream call sites, preserved
exception and ownership evidence, unchanged consume guards, passing gates, and
a roadmap update only at implementation completion. Ordinary PyO3 module
initialization and argument extraction are explicitly outside that count.

## Idempotence and recovery

Tests, builds, and documentation gates can be repeated. Keep failing logs,
inspect the cited failure, fix it, then rerun the affected gate; do not rerun
unchanged passing suites without reason. Retain property regressions discovered
by shrinking. Never reset another contributor's changes. Undo an uncommitted
experiment by restoring only its known patch; undo a published implementation
with a reviewed revert commit and rerun gates. Rebuild the native extension
after a revert so Python does not load stale code.

## Expert review reconciliation

The six-perspective review found no architectural blocker. Pandalump required
one invocation as well as one conversion implementation; Wafflecat preferred
wrapping the existing taxonomy; Telefono required extraction-error exclusions
and platform-specific messages; Doggylump required native/shim ordering tests;
Buzzy Bee required ownership witnesses without performance claims; Dinolump
required pure Rust tests and a verified dependency baseline. These findings are
incorporated above. The remaining implementation-time uncertainty is compiling
Rust behavioural tests with the existing `rstest` version on Rust 1.85.

## Artefacts and notes

The primary external reference, checked through Firecrawl on 2026-09-19, is
[PyO3 0.29 error handling](https://pyo3.rs/v0.29.0/function/error-handling). It
documents custom `From<E> for PyErr` conversion and `Result` propagation. The
repository's existing OS-code conversion remains authoritative for the stronger
Cuprum contract; the generic PyO3 conversion is not a replacement.

Firecrawl also checked the published manifests for
[rstest-bdd 0.5.0][bdd-manifest], [its macros][bdd-macros-manifest], and
[0.6.0](https://docs.rs/crate/rstest-bdd/0.6.0/source/Cargo.toml). This
supports the minimum-version decision, not a claim that the planned test suite
already compiles.

Revision note (2026-09-19): initial draft adapts the roadmap wording to the
current three-crate architecture and distinguishes native validation order from
the Windows shim. Implementation remains pending approval.

[bdd-manifest]: https://docs.rs/crate/rstest-bdd/0.5.0/source/Cargo.toml
[bdd-macros-manifest]: https://docs.rs/crate/rstest-bdd-macros/0.5.0/source/Cargo.toml
