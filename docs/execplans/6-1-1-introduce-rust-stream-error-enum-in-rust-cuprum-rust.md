# Centralize native stream errors (6.1.1)

Status: COMPLETE — M1 and M2 both closed. Approved 2026-09-26; the publishing
draft-PR and approval checkboxes below are recorded as done by that approval.
M1's plateau is reached at head `6a28ff95`: the full pure-Python gate sequence
and the native extension stage both pass, with `actionlint` locally
unobservable (recorded in Progress and Surprises). Windows runtime evidence has
since been obtained: it found two branch defects, both fixed at `dafbfa4e`, and
the Windows job at that head succeeded (job `108519345532`; evidence in
Progress). M2 closed the remaining evidence, reconciled the roadmap, and ticked
6.1.1. The closing head is `11c6cb7f`, whose CI run `36290878443` is
`completed|success` with **zero** non-success jobs, including the required
`coverage` check that this work existed to fix; `7dba35cf` is the head where
that check first went green (job `108538239040`) and where the closing
documentation was then built on top. A CodeRabbit pass over `11c6cb7f` then
found one further defect — a behavioural row that passed vacuously on Windows —
which is fixed and recorded in Progress. The branch was then rebased onto `main`
(`7f762870`) as a pure replay, and the head is now `b7d1b109`, whose CI run
`36356062517` is likewise `completed|success` with **17 of 17** jobs green and
zero non-success, the required `coverage` check included. That rebase moved the
Rust coverage figure from 87.92% to **88.11%**, because `main` added
`cuprum-streams` code to the measured workspace; both measurements are recorded
in Progress, each bound to the revision it measured. A final review pass then
split `test_rust_streams_boundary_property.py`, which the branch's own Windows
fixes had grown to 431 lines, back under the 400-line cap; both resulting
modules are registered in `EXTENSION_TEST_TARGETS`, and the split's third stale
claim — a developers'-guide table row still credited with payload fuzzing that
had moved away — was corrected with it. Three further stale figures in this
plan were then found and corrected, and the gates re-run at the resulting head
`9096c804`. Five pass there (`make check-fmt`, `make test`, `make typecheck`,
`make markdownlint`, `make nixie`), each log recording that head; the sixth,
`make lint`, reaches ten of its eleven sub-checks and then aborts in the final
`actionlint` step — a host-only, non-reproducible-on-demand wedge in the
shellcheck handshake, so `actionlint` is **unobserved locally, not failed**,
and CI covers it. The PR was then marked ready for review, which un-blocked
CodeRabbit (it had been reporting `skipped` while the PR was a draft); its one
finding — deduplicate a local `_safe_close` onto the shared helper — was
correct and was applied in `b3ae9f20`, disproving in the process a file-count
premise this plan had asserted without testing. The branch stands at **19**
files excluding the lockfile and this plan — one over the tolerance, on a single
`typos.toml` line `main` already carries, with the merge ref back at eighteen.
The 2026-10-01 entry below measures all three counts. At `1714ac0d` the required
`coverage` job failed twice on a test this branch does not touch,
`test_doctest_warning_contract.py::test_pinned_doctest_route_rejects_a_warning`.
The failure is **environmental and measured, not a branch defect**: the
coverage job is the only lane that runs that test without provisioning
`nightly-2026-08-23`, so rustup downloads the 595 MB toolchain *inside* the
test's own `subprocess.run`, under the global 30 s `pytest-timeout`. Reproduced
locally by pointing `RUSTUP_HOME` at an empty directory: **11.5–12.7 s cold
versus 0.49 s warm**, a 23–26× differential, on a 6-core idle host — the same
download on a loaded 2-vCPU runner exceeds the bound. The test file is
byte-identical to base `7f762870` (`b8f96ae4`), is outside the branch's change
surface, and the job passed at `b7d1b109` (`36356062517`) with the same bytes.

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
- [x] M1: implement and validate the typed boundary and its tests. Complete
  2026-09-27: implementation, tests, and documentation are in and gated, and
  every child item below is done.
  - [x] (2026-09-26) Typed boundary implemented across `lib.rs`
    (`RustStreamError`, typed `validate_buffer_size`/`convert_fd`),
    `errors.rs` (`impl From<RustStreamError> for PyErr`, old converter demoted),
    and `stream_pyfunctions.rs` (single-conversion boundary).
  - [x] (2026-09-26) V1/V2 red evidence captured: `E0432 unresolved import
    crate::RustStreamError` at three sites.
  - [x] (2026-09-26) V1/V2 green: `make test-rust TEST_FLAGS='-p cuprum-rust
    --all-targets --all-features'` → **51 tests run, 51 passed, 0 skipped**,
    including four `rstest-bdd` test entries
    (`stream_error_behaviour::typed_native_stream_failures` and
    `accepts_a_valid_buffer_size::case_1`..`case_3`) and clean doctests.
    Log: `/tmp/611-rust-focused-1.out`. **This entry originally read "all four
    `rstest-bdd` scenarios", which was wrong**: four test *entries* are not
    four scenarios. Only two scenario bindings existed, and three of the five
    declared scenarios never ran — see Surprises. The count of four was read as
    confirmation that the feature file was fully exercised.
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
  - [x] (2026-09-27) Post-documentation gate run surfaced two self-inflicted
    defects, both introduced by `727484e2` and both traced to
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
    3. **`make test`'s 13 failures are environmental, not branch defects.**
       They come from
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
  - [x] (2026-09-27) Full gate sequence plus native extension stage complete at
    head `6a28ff95` — all eight planned pure-Python gates and all four native
    gates green, each logged under `/tmp/gate-611-*.out` and `/tmp/611-*.out`.
    The plateau this milestone targets — a passing implementation, tests, and
    accurate documentation, each change gated before its commit — is reached.
    Two qualifications carry forward into M2 rather than being closed here:
    `actionlint` is locally unobservable (see the Surprises entry), and Windows
    *runtime* evidence is still outstanding. A draft-PR CodeRabbit app check
    reports "Review skipped: draft pull request", which is why the plan calls for
    the `coderabbit review --agent` CLI pass instead; the app's verdict is not a
    review.
  - [x] (2026-09-27) `coderabbit review --agent` completed on PR #432
    (4 findings, 3 distinct). One was already fixed by `bcf72f52`; the other two
    are actioned in the two entries below and the fix is re-gated. Logs:
    `/tmp/coderabbit-74fbe974-b8fa-43c7-9beb-901c9d163da0-6-1-1-introduce-rust-stream-error-enum-in-rust-cuprum-rust.out`,
    `...-findings.json`.
  - [x] (2026-09-27) **Windows runtime evidence obtained, and it was red.** The
    `Extension-gated tests (Windows Python/Rust boundary)` job failed
    deterministically at both `6a28ff95` and `bcf72f52`:
    `test_out_of_i64_buffer_size_stays_an_extraction_error[consume]` and
    `test_native_stream_exception_categories[consume-a zero buffer size-ValueError]`,
    both `OSError(9, 'Bad file descriptor')` from `cuprum/_streams_rs.py:103`.
    The same job is green on `main` (`991dee64`), so this was a branch defect,
    not a platform flake. See the Surprises entry for the causal chain: the two
    failing rows are marked with `_buffer_validation_before_descriptor` on the
    *push* path, but the *consume* counterpart of each was missing the same
    mark, and both reached the reader-preparation step through the shim's
    Windows-only `get_osfhandle` conversion.
  - [x] (2026-09-27) Both failures fixed in the **tests**, not the shim, which
    was not the failing component. `_streams_rs.py` is byte-identical to `main`
    (`git diff origin/main...HEAD -- cuprum/_streams_rs.py` is empty), the plan
    forbids reordering it (`## Constraints`: "The Python Windows shim has its
    own earlier descriptor preparation; do not reorder it"), and the failing
    consume rows had no `_buffer_validation_before_descriptor` mark, so the
    push-path skip never reached them. Three changes:
    1. `test_native_stream_exception_categories`'s zero-buffer step now opens
       `os.devnull` for the reader instead of passing `-1`. The reader is never
       dereferenced — the buffer check raises first — but on Windows `-1` fails
       in the *wrapper's* preparation, so the row was observing the wrong layer.
       The pump row hands a *separate* write end, because the wrapper closes a
       rejected writer and reusing the reader would double-close it.
    2. Every buffer-window property now opens a real read end rather than
       passing the `-1` throwaway: `test_rejects_out_of_range_buffer` and
       `test_out_of_i64_buffer_size_stays_an_extraction_error` were retargeted
       onto the open-reader entry points, and the throwaway variants of both
       were removed rather than kept. An earlier revision of this fix kept them
       alongside the new rows, which would have left each entry point with two
       properties asserting the same window — and would have left both
       originally-failing nodeids still failing on Windows, since the
       extraction-error property's consume row was the other Windows failure
       and handover 1 above did not touch it. The `_UNUSED_FD` constant and
       `_pump_with_buffer_size` helper are gone with them; the
       `_buffer_validation_before_descriptor` mark survives on the one property
       that supplies its own invalid descriptor as the subject of the assertion.
       Net row change: `test_rejects_out_of_range_buffer_with_open_reader` is
       new, two throwaway rows are removed, so the module's collected count is
       unchanged at 10. The collected count is not the figure that moved on
       Windows, though: on `main` the two removed rows were *skipped* there, so
       the Windows job's status totals shift by both of them. That is recorded
       under the Windows entry below, because it is the part of this change the
       platform actually observes.
    3. The Rust oracle is corrected — see the Surprises entry.
    Committed as `dafbfa4e` and re-gated on the frozen commit. All eleven gates
    green, sequentially: `make fmt`, `make check-fmt`, `make lint`,
    `make typecheck`, `env -u BASH_ENV make test`, `make markdownlint`,
    `make nixie`, `make msrv-check`, `make develop`, `make test-extension`,
    `make boundary-test`. Blob hashes of the change surface were identical
    before and after, so the run is citable against `dafbfa4e` — the two known
    tree-dirtying tripwires (`typos.toml` regeneration, `uv.lock`) did not fire.
    Counts, quoted from the logs: nextest **154 tests run: 154 passed, 0
    skipped** — which includes the corrected
    `stream_error_tests::properties::validation_matches_the_size_window`, the
    test the previous gate run never executed; `make test-extension`
    **101 passed, 1 skipped**, exactly the M1 figure (103 − the two rows this
    change removes); `make boundary-test` **13 passed** Rust plus **116 passed**
    Python. Logs: `/tmp/regate-611-*.out`, frozen-state captures in
    `/tmp/regate-611-state/{pre,post}.txt`.
    `env -u BASH_ENV` is required on this branch: the host sets `BASH_ENV` to
    `/home/leynos/.lody/bashenv`, which re-prepends `~/.lody/bin` inside every
    `bash -c` so the release tests' recorded `gh`/`git` stand-ins are shadowed.
    The fix for that lives on another branch (`ccd9d1df`, PR #466) and is not
    an ancestor here. The unscrubbed `make test` fails 13 tests for that reason
    and for no other; see the Surprises entry, which records that an aborted
    gate leaves its later checks unobserved.
- [x] (2026-09-27) **Windows runtime evidence obtained and green at `dafbfa4e`
      .**
  CI run `36283355903` (event `pull_request`, head
  `dafbfa4e0ad604ca75dca65131af841fc50b2431`, attempt 1); job
  `Extension-gated tests (Windows Python/Rust boundary)`, job id `108519345532`,
  `completed` / `success`, every step successful including *Build the native
  extension* and *Run extension-gated tests*. **The claim is job-level, not
  run-level:** the parent run reads `cancelled`, because `ci.yml` sets
  `concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: … }` and
  every later push cancels the previous head's run. Fourteen of that run's
  eighteen jobs reached `success` — including the Linux extension-gated job and
  the Windows/Linux/macOS wheel builds — and the remaining four
  (`Typecheck and test (Python 3.12)`, `lint-test`, `coverage`,
  `benchmark-ratchet`) read `cancelled`, superseded by the next push rather
  than failed. No non-head commit's *run* can ever be cited as green; its
  *jobs* can, which is what is cited here and below. Log:
  `/tmp/611-win-ci-job.log` (de-ANSI'd from the raw job log). The suite is
  extension-required — the job's pytest line is prefixed
  `CUPRUM_REQUIRE_RUST_EXTENSION=1` — so this is compiled-boundary evidence,
  not a shim run. Verbatim PASS lines at `dafbfa4e`:

  ```text
  test_rejects_out_of_range_buffer_with_open_reader[consume]     PASSED [ 17%]
  test_rejects_out_of_range_buffer_with_open_reader[pump]        PASSED [ 18%]
  test_out_of_i64_buffer_size_stays_an_extraction_error[consume] PASSED [ 25%]
  test_out_of_i64_buffer_size_stays_an_extraction_error[pump]    PASSED [ 26%]
  test_native_stream_exception_categories[consume-a zero buffer size-ValueError] PASSED [ 90%]
  ======================= 89 passed, 13 skipped in 2.83s ========================
  ```

  The RED run this replaces is CI run `36280857796` at head `bcf72f52`, job
  `108512365806`, whose verbatim failures were:

  ```text
  FAILED .../test_out_of_i64_buffer_size_stays_an_extraction_error[consume]
    - OSError: [Errno 9] Bad file descriptor
  FAILED .../test_native_stream_exception_categories[consume-a zero buffer size-ValueError]
    - AssertionError: expected ValueError, found OSError: [Errno 9] Bad file descriptor
  ================== 2 failed, 85 passed, 15 skipped in 4.99s ===================
  ```

  (The two `FAILED` entries are wrapped here to fit the 120-character
  code-block limit; pytest prints each as one line. No text is omitted, and the
  summary line is unwrapped.)

  Both failures carry the predicted signature: `[Errno 9]`, the `get_osfhandle`
  refusal, and both are on the **consume** path, which is the asymmetry the
  diagnosis named. Log: `/tmp/611-win-ci-RED.log`. Comparing the two runs'
  status totals accounts for every entry that moved between them — 85 passed /
  2 failed / 15 skipped becoming 89 passed / 0 failed / 13 skipped, and the
  collected count is 102 in both. Three rows changed failure into success: the
  two `FAILED` nodeids above plus
  `test_out_of_i64_buffer_size_stays_an_extraction_error[pump]`, which passed
  in the RED run but belongs to the same retargeted set. Three rows changed
  skipped into passing: both `test_rejects_out_of_range_buffer[consume|pump]`,
  which the RED run skipped on `win32` via
  `_buffer_validation_before_descriptor` and which this change replaces with
  the open-reader property; plus
  `test_native_stream_exception_categories[consume-a zero buffer size-ValueError]`,
  which the RED run *reached and failed* (pytest tags a failing test `F`, not
  `s`, so it counts as a failure in that run and as a pass in this one).
  Nothing was dropped, disabled, or made to skip to obtain the green; the
  change *removed* two Windows skips and added none.

  Two findings this evidence adds, neither visible before it:

  1. **The RED run under-reported the breakage, and the skip is why.** It showed
     two failures, not three. `test_pump_rejects_invalid_reader_descriptor`
     already passed a genuinely valid writer, so the writer-conversion arm was
     covered; the uncovered arm was the *consume* counterpart of the
     buffer-window path, and it was hidden behind a skip rather than absent. A
     skip that conceals a defect is indistinguishable in the summary line from
     a skip that is merely inapplicable — both print `s`. The status totals, not
     the failure list, are what exposed it.
  2. **The consume buffer path was never observed on Windows, even on `main`.**
     On `main` (`991dee64`, CI run `36195887840`, Windows job `108271499312`,
     `success`) the boundary-property module reports
     `test_rejects_out_of_range_buffer[consume] SKIPPED`, `[pump] SKIPPED`, and
     `74 passed, 10 skipped`. The asymmetry this branch hit was therefore
     long-standing and latent on `main`: the `-1`-throwaway window property had
     been skipping on Windows all along, and only this branch — routing the same
     window through an open reader — makes it execute there. The branch did not
     introduce the gap; it is the first change to close it. Established by
     downloading that job's log rather than inferring it from the run's
     success; log `/tmp/611-win-main.log`.
- [x] (2026-09-27) **The required `coverage` check was failing, and fixing it
      found a real test defect.** It had never run on this branch — the
      concurrency group cancelled it on three prior pushes — and its first
      observation failed at 86.05% against a baseline of 87.35% (±1.00 pp).
      Root cause: `stream_error_behaviour.rs` bound only 2 of the 5 declared
      scenarios, because a `#[scenario]` omitting both `name` and `index`
      silently takes the first. Fixed by naming every binding and adding a
      descriptor scenario; Rust coverage then measured **87.92%** at the
      pre-rebase head, above the baseline. Details and arithmetic in Surprises;
      the mutation V2 requires was run. See also the false measurements this
      produced and corrected, below. The figure was re-measured after the
      rebase onto `main` and is now **88.11%** — see the entry at the end of
      Progress.
- [x] (2026-09-27) Two self-corrections, both recorded rather than dropped: the
      scenario's error code was moved 9 → 8 on a justification not present in
      the code, and reverted; and the module docstring was rewritten to claim
      the lint sees these functions as test code, which is wrong —
      `no_expect_outside_tests` runs under `cargo check`, so a file-backed
      `#[cfg(test)] mod` gives no test ancestry. Original text restored. Both
      are in Surprises.
- [x] (2026-09-27) **`make check-fmt` cannot see markdownlint, so the plan's own
      CI evidence block failed `lint-test` in CI.** The logged `coverage`
      transcript was fenced without a language (`MD040`). `check-fmt` runs
      `ruff format --check`, `rustfmt --check`, and `mdtablefix --check`; `lint`
      runs Ruff/interrogate/pylint/df12/Skylos, clippy, Whitaker, spelling,
      yamllint, and actionlint. Neither runs markdownlint. `lint-test` runs it
      as its own step — the upstream `DavidAnson/markdownlint-cli2-action` over
      `**/*.md`, `**/*.markdown`, `**/*.mdx`, reading `.markdownlint-cli2.jsonc`,
      which the workflow notes is the same config `make markdownlint` uses. The
      block now opens with `text`, matching the plan's two other output
      excerpts, and `make markdownlint` reports 0 errors over 78 files. The
      plan already said in "Concrete steps" that documentation-only commits
      need `make markdownlint`; the step was omitted anyway, and the gates run
      before the commit were `check-fmt` and `lint`, neither of which covers it.
- [x] (2026-09-27) **M2's required evidence is green on `7dba35cf`, and the
      roadmap tick is done.** The CI run at that head is `completed|success`
      with **no** non-success job: `lint-test` passes at the step that had
      failed (`Lint Markdown`), and the required `coverage` check passes as job
      `108538239040`, which is the check whose failure started this work. Both
      extension-gated jobs pass, including Windows. `docs/roadmap.md` now
      carries `[x]` on 6.1.1 and leaves 6.1.2 and all later items open
      (verified by grep: phase 6 shows 1 ticked, 6 open). The tick is a
      one-line hand edit because `mapsplice` cannot parse the roadmap; the
      deviation, its proof, and the unrelated churn mapsplice also produced are
      recorded in the Decision log, with the parser defect itself in Surprises.
      This entry was written while `7dba35cf` was the head, so the evidence it
      cites is that head's; the tick itself landed one commit later in
      `11c6cb7f`, whose own run is green — see the entry below.
- [x] (2026-09-27) **The closing head `11c6cb7f` is green, and the documentation
      gates were re-run over it.** CI run `36290878443` (event `pull_request`,
      head `11c6cb7f3540d9ac1ab16f4ffa48cc9bd153301b`) is
      `completed|success` with **zero** non-success jobs — including the
      required `coverage` check this work existed to fix, `benchmark-ratchet`,
      `lint-test` (green at `Lint Markdown`, the step that had failed), and both
      extension-gated jobs including Windows. Unlike the intermediate heads,
      this run is the head's own and is not superseded, so it is citable at
      run level and not only at job level.

  The four documentation gates that ran over the frozen head, head unchanged
  before and after, tree clean, were `make fmt`, `make check-fmt` (675
  formatted, 78 unchanged), `make markdownlint` (78 files, 0 errors, spelling
  clean) and `make nixie`; their logs are
  `/tmp/611-scrutineer-{fmt,check-fmt,markdownlint,nixie}-11c6cb7f.out`.
  `make lint` was **not observed at that head** — it stalls locally at
  `github-actions-lint` in the actionlint/shellcheck stdin deadlock recorded
  under Surprises, and that sub-check is local-only, so this is an unobserved
  check rather than a pass. Fence pairing in this plan was confirmed
  independently by a balanced scan (22 fences, 11 blocks, zero unlabelled
  openers), which is what cleared the `MD040` failure mechanically rather than
  by inspection.

  **Scope caveat, carried at the strength it actually has:** these gates
  covered the documentation-only commit range `7dba35cf..11c6cb7f`, not the
  whole branch. The branch against `origin/main` is 19 files and roughly 3,817
  insertions, whose Rust and Python changes are gated by CI rather than by this
  local run. This run must not be cited as whole-PR coverage.
- [x] (2026-09-27) **The CodeRabbit slot was rate-limited, and the review was
      re-run once the limit reset.** `coderabbit review --usage` reported
      `Remaining: 1 of 10`; the attempt then consumed the last slot and the
      service returned a rate-limit payload (`10 of 10` used, `waitTime` about
      six minutes). The rate limit is a property of the review seat, not of this
      branch, and a request that is delivered can still yield zero review, so a
      consumed slot is not evidence of a review having happened. The re-run was
      launched against the frozen closing head `11c6cb7f` after `--usage`
      reported `Remaining: 10 of 10` again. It completed in 281 s and returned
      **two** findings, both naming the same lines
      (`tests/behaviour/test_rust_streams_errors_behaviour.py`, around 169-175)
      at different severities — one `major`, one `trivial`. They are one defect,
      not two, and are actioned as the single change in the next entry.
- [x] (2026-09-28) **`a fatal reader error` passed vacuously on Windows, and
      now does not.** CodeRabbit's re-run flagged the descriptor the
      `a fatal reader error` rows passed as their reader: a **closed** pipe read
      end. A closed descriptor never reaches Rust on Windows — the wrapper
      resolves it through `msvcrt.get_osfhandle` while preparing the call and
      raises `OSError(EBADF)` there — so the row observed the *wrapper's*
      descriptor failure while claiming to pin the native read path. That is the
      same vacuity the `a zero buffer size` rows had already been fixed for, and
      it was **provable from this branch's own evidence before the fix**: in the
      RED run at `bcf72f52` (log `/tmp/611-win-ci-RED.log`) the two
      `a fatal reader error` rows read `PASSED` on Windows at the same moment the
      sibling `consume-a zero buffer size` row read `FAILED` with
      `AssertionError: expected ValueError, found OSError: [Errno 9] Bad file
      descriptor` — the `get_osfhandle` refusal — proving the wrapper, not Rust,
      was answering. The rows are now green in CI (`36290878443`), so the
      `PASSED` lines alone could not have revealed it; only the contrast with
      the failing sibling does. The reader is now an **open but unreadable**
      descriptor — a file under `tmp_path` opened `O_WRONLY | O_CREAT` — which
      is open on every platform and still fails the first read, and it is
      registered in a `contextlib.ExitStack` so it is closed even when
      `_capture` raises. This is the same device
      `test_rust_errno_windows.py` already uses to put a native code on the
      error the conversion must retain. Verified locally: all four rows in the
      module pass with `CUPRUM_REQUIRE_RUST_EXTENSION=1`, and the fix is
      non-vacuous on POSIX because an `O_WRONLY` descriptor there also fails
      `read(2)` with `EBADF`.
- [x] (2026-09-28) **The PR description was brought up to date with the
      implementation.** The body was still the pre-approval text — it described
      the branch as carrying "the pre-implementation plan", asserted "This
      branch changes no runtime code and leaves roadmap item 6.1.1 open", and
      listed only documentation gates under Validation. Every one of those
      claims is now false. The replacement states the implementation, links the
      conversion point, quotes the test counts from the logs that contain them
      (`cargo nextest` 154/154 *at the pre-rebase head*; extension-gated 101
      passed/1 skipped on Linux, 89 passed/13 skipped on Windows; Rust ratchet
      997/1134 = 87.92% against 87.35% *also at the pre-rebase head*), and
      carries the two traps a reader of the diff would otherwise
      have to rediscover — the unbound `#[scenario]` bindings behind the
      coverage failure, and the Windows wrapper masking the native read
      failure. It also records the `mapsplice` deviation and why `CHANGELOG.md`
      is untouched. The bot-generated "Summary by Sourcery" section was
      preserved rather than dropped, and the attribution line moved to the end
      where it belongs. Verified by an independent read-back rather than the
      `gh api -X PATCH` echo, because a read-back taken from the same call can
      show the value it was sent rather than the value the server stored.
- [x] (2026-09-28) **The one remaining documentation gate failure was an
      `MD046` this plan had introduced itself, and it is fixed.** `make fmt`
      and `make markdownlint` had been failing with a single error,
      `docs/execplans/6-1-1-…-cuprum-rust.md:562 error MD046/code-block-style
      Code block style [Expected: fenced; Actual: indented]`. The trigger was
      not the line's own content (§562 is 70 characters, well inside the
      limit): within a list item, a **blank line followed by a continuation
      line indented four or more spaces** is read as an indented code block.
      That is why the earlier attempt to fix it — removing a blank line —
      could only move the report to the next blank-line-separated
      sub-paragraph rather than clear it. Four variants were measured against
      the repository's own `.markdownlint-cli2.jsonc` before choosing one:
      the unmodified blob reported 1 issue at 562; deleting the blank line
      reported 1 issue at 574; re-indenting the paragraph to two spaces but
      leaving the later paragraphs alone reported 1 issue at 575; re-indenting
      the whole remainder of the list item to two spaces reported **0
      issues**. Only the last is a fix, so the item's continuation paragraphs
      were re-indented from six spaces to two. `mdtablefix` does not revert
      that indent, which was the open question: `make fmt` runs mdtablefix
      before `markdownlint --fix`, and a reflow that undid the change would
      have made the fix invisible to the gate that required it.
- [x] (2026-09-28) **The documentation gates were re-run over the frozen tree,
      and their earlier results are superseded.** The prior run's
      `markdownlint` verdict was pinned to blob `1913074f` and the four gates
      before it to earlier revisions still, because the plan was being edited
      throughout that window — so those results did not vouch for the Markdown
      they were later cited against. Over the frozen tree, head unchanged and
      both edited files' blobs identical before and after every gate (plan
      `64913648`, test module `a1015c40`): `make fmt` exit 0 with markdownlint
      reporting `Summary: 0 error(s)` over 78 files; `make check-fmt` exit 0
      (`675 files already formatted`, `78 files left unchanged`);
      `make markdownlint` exit 0, 0 errors; `make nixie` exit 0,
      `All diagrams validated successfully!`. Logs:
      `/tmp/611-{fmt,check-fmt,markdownlint,nixie}-md046fix.out`. As before,
      `make lint` is **unobserved on this tree** — it is green in CI at the
      closing head and green locally on an earlier run of this branch's
      Python change (`/tmp/611-scrutineer-lint-fatalreader.out`, all 11
      sub-checks observed), but it was not re-run after this Markdown edit.
      The test module's blob `a1015c40` is unchanged from the run that gated
      it (`4 passed` in the extension-gated module, `3 passed` in its
      neighbour), so that evidence still stands. This recording entry is itself
      the next edit, so these verdicts pin the commit that precedes it, not this
      file's final revision.
- [x] (2026-09-28) **Rebased onto `main` (`7f762870`) as a pure replay, and the
      gates were re-run at the new head.** The boundary was `991dee64`,
      corroborated three ways rather than assumed: `ab58a3d6^` is that commit,
      the Progress entry above records the earlier rebase onto it, and PR #432's
      base is `main` rather than a stacked parent. All 19 commits replayed with
      **no conflict**, but a clean exit is not the evidence — `range-diff` reports
      19 unchanged, 0 modified, 0 added, 0 removed; the branch-owned diffstat is
      identical (19 files, 4000+/87-) on both sides; every key blob
      (`errors.rs`, `lib.rs`, `Cargo.lock`, this plan, the behavioural module) is
      byte-identical; and `git diff 7f762870 b7d1b109` produces a tree equal to
      `git merge-tree 0ff37532 7f762870` **exactly**, which is the strongest
      available proof that the result is `main` plus this branch and nothing
      else. `main`'s two changes arrived intact and were checked for presence
      rather than inferred from the clean exit: the `cuprum-streams` Miri
      invocation in `boundary-miri`, and the rewritten Miri section of
      `docs/developers-guide.md`. Neither overlapped this branch's hunks
      (Makefile 508 vs 171; guide 3647 vs 3369). `rust/Cargo.lock` needed no
      rebuild: `main` did not touch it, its `Cargo.toml` change (`cfg(miri)`)
      added no dependency, the branch's lock is carried byte-identical, and
      `cargo metadata --locked` exits 0.
- [x] (2026-09-28) **The coverage figure moved, and the plan now records both
      measurements.** CI at the rebased head `b7d1b109` is `completed|success`
      with **17 of 17** jobs successful and **zero** non-success — including the
      required `coverage` check. That check's ratchet step prints two pairs,
      Rust then Python, and names neither; the pairing is nonetheless
      unambiguous from the log, which prints them adjacent in order: Rust
      **88.11%** against a 87.35% baseline, and Python 89.65% against 89.82%,
      both inside the `±1.00` pp tolerance. The Rust figure was **87.92%**
      before the rebase, so `main`'s added `cuprum-streams` code moved it. This
      is worth recording precisely because the earlier number was not an error:
      `997/1134 = 87.92%` was a correct measurement of a fixed revision, and it
      went stale because the workspace it measured changed underneath it. The
      sites asserting *current* state were updated to 88.11%; the ones recording
      *historical* measurement (the 86.05% failure, the Surprises arithmetic)
      were left at their measured values and given forward pointers, because
      rewriting a dated observation to match today's number would falsify it.
      CI prints percentages only, so no exact post-rebase fraction is
      re-derivable by hand — the percentage is quoted as CI printed it rather
      than reconstructed.
- [x] (2026-09-28) **All four post-rebase gates passed on the frozen tree.**
      `make check-fmt` (675 formatted, 78 unchanged, mdtablefix clean),
      `make test` (`cargo nextest` **160/160**, and nine pytest invocations all
      green — 2543 passed / 1 skipped, 638, 116, 123, 34, 22, 19, 4, 2 — with
      the single skip being the Windows-only
      `test_rust_errno_windows.py::test_windows_failures_carry_the_native_code_as_winerror`
      case on a Linux host), `make typecheck` (`ty` reports all checks passed),
      and
      `make lint` (all leaves observed, actionlint included). Each log records
      the head it ran at, and all four name `b7d1b109`; `git status` was clean
      before and after every gate and no gate mutated a tracked file. Two known
      local traps were probed and did **not** reproduce: the `BASH_ENV`
      `gh`-stand-in shadowing (`0` occurrences of `failed to run git`) and the
      host-only actionlint 1.7.12 deadlock (`make lint` finished in 52 s inside
      a 900 s bound). The nextest total is **160** where the pre-rebase head
      measured **154** — `main` added six tests — which is a second, independent
      signal that the coverage scope grew.
- [x] (2026-09-29) **The boundary property module was split back under the
      400-line limit, and the split exposed two stale claims.** Adding the
      Windows buffer-window and i64-extraction properties grew
      `cuprum/unittests/test_rust_streams_boundary_property.py` from 246 to
      **431** lines, over the cap in `AGENTS.md` and in this plan. The two
      round-trip properties and their pipe helpers (`_feed_pipe`,
      `_consume_via_pipe`, `_pump_via_pipes`, `test_default_buffer_matches_explicit`,
      `test_pump_default_buffer_matches_explicit`) moved to the new
      `cuprum/unittests/test_rust_streams_roundtrip_property.py`; both modules
      are now under the cap at **349** and **123** lines (they stand at 345 and
      119 after the later `_safe_close` deduplication), and the split is
      purely a move — the moved code is byte-identical and `_I32_MAX` stays
      where it is used. The new module is registered in
      `EXTENSION_TEST_TARGETS`, which is **required** rather than tidy: it
      requests the root conftest's `rust_streams` fixture, which is one of the
      three signals `test_extension_build_contract.py` derives the gated set
      from, so omitting it fails
      `test_every_extension_gated_module_is_a_declared_target`. Confirmed by
      querying the contract test's own derivation (`_gated_modules()` reports
      the new module with that reason; gated-but-undeclared is empty). The
      registration is also what keeps the new module's two properties — the
      only ones that assert the default really applies to a completed transfer
      — executing in the guarded job instead of skipping wherever the extension
      is absent.

  The move corrected two claims that no gate can see. First, the local
  `_safe_close` justified itself as wheel-local ("the packaged test module
  stays importable from an installed wheel, which ships `cuprum/unittests`");
  that is **false**. Both build backends exclude the directory —
  `[tool.uv.build-backend] source-exclude` and `[tool.maturin] exclude` both
  name `cuprum/unittests/**` — and the wheel-manifest snapshot agrees, listing
  125 entries with none under `unittests/`. The rationale was simply dropped at
  that point; see the entry below for why the duplicate itself was deduplicated
  two commits later rather than here. Second, the `_MAX_BUFFER_SIZE` mirror
  comment named `rust/cuprum-rust/src/lib.rs`, but `MAX_BUFFER_SIZE` is defined
  in `rust/cuprum-streams/src/lib.rs`. The const is private to that crate and
  is not re-exported anywhere; `cuprum-rust` inherits the same 1 GiB cap
  indirectly, through `cuprum_streams::BufferSize::new`, which is why the
  comment looked plausible where it was. Corrected to name the defining file.
  Both were pre-existing on `main`, not introduced by this branch.

  The change lands the PR at **18 of 18** files excluding the lockfile and this
  plan, exactly at the tolerance rather than over it. `make lint` regenerated
  `typos.toml` from the live shared estate dictionary (one entry reworded,
  unrelated to this branch); it was reverted here, on the reading that the
  branch had carried no generated file in any of its commits —
  `git log 7f762870..HEAD -- typos.toml` was empty at this head. **That reading
  did not survive the branch.** `11039493` later committed the line, so the
  command is no longer empty and the count is **19**. The 2026-10-01 entry
  below measures both frames and dispositions the file.

  The record above first read "that budget is why the fix is a split and two
  comment corrections rather than a wider deduplication: touching a shared
  support module would have taken the count to 19 and tripped a stop-and-ask
  trigger." **That premise was wrong, and it is disproved by inspection.** Both
  files the deduplication touches —
  `cuprum/unittests/test_rust_streams_roundtrip_property.py` and the module
  holding the local copy — are *already* in the branch's changed set, so
  neither adds a file. `_rust_stream_test_support.py` needs no edit at all: it
  already imports `contextlib` and `os`, and its `_safe_close` is identical in
  behaviour to the copies being removed. The count therefore stays at **18**
  either way. The real cost was zero and had been all along; the file-count
  argument was a premise asserted from memory and never tested until
  CodeRabbit's review forced the question. That is the same class of error the
  three stale figures above belong to, and it is worth naming as one: a
  *constraint* claim deserves the same evidence as a *result* claim, and this
  one was never measured.

  All four commit gates pass on the frozen tree at `bd41d2c4` and each log
  records that head: `make check-fmt` (676 formatted, 78 unchanged), `make test`
  (`cargo nextest` **160/160**, and `2543 passed, 1 skipped`), `make typecheck`
  (`ty` all checks passed), and `make lint` (52 s, all eleven sub-checks
  reached, actionlint included). The first `make test` attempt failed 13
  release-workflow tests on the `BASH_ENV` git-shim trap — a host artefact, not
  a code defect; the remedied re-run `env -u BASH_ENV make test` is the green
  one, and both logs are kept so the failed attempt is not quietly discarded.
  The split's own properties were also run directly with
  `CUPRUM_REQUIRE_RUST_EXTENSION=1` (10 passed), which is what shows they
  execute rather than skip.
- [x] (2026-09-29) **The split's third stale claim was in the developers'
      guide, and the four commit gates were re-run at the final head.** The
      extension-gated module table still described
      `test_rust_streams_boundary_property.py` as "randomized payloads across
      the boundary", but every payload-fuzzing property had just moved out of
      it; the row now reads "the rejection boundary: invalid buffer sizes and
      descriptors", and the row for the new module names the payload fuzzing
      and the default-equivalence claim it actually carries. The row was
      already inaccurate before the split — it was written when the module held
      both halves — but the split is what made it a misdescription of the
      present tense, so it is repaired here rather than deferred. No gate can
      see this class of defect: `markdownlint` checks the table's shape, not
      its truth, and the prose is neither a link nor an identifier. The column
      widths are fixed-width per table, so both rows were rewritten to the
      existing 57/166 split (226 columns) and verified row by row.

  Because the earlier green run was recorded at `72595015` and two commits
  followed it, the gates were re-run at the new head `49dae298`; a commit after
  a gate run invalidates that run as a citation. All six pass on the frozen
  tree and each log records the head: `make check-fmt` (676 formatted, 78
  unchanged), `make test` (`cargo nextest` **160/160**, and
  `2543 passed, 1 skipped`, the sole skip being a Windows-only case),
  `make typecheck` (`ty`, 60 packages), `make lint` (51 s, all **eleven**
  sub-checks reached through to `actionlint`, so nothing is unobserved),
  `make markdownlint` (0 errors across 78 files, spelling included), and
  `make nixie` (all diagrams validated). The new module's two properties are
  quoted `PASSED`, not skipped, in the test log. `make lint` again regenerated
  `typos.toml` from the shared estate dictionary and it was again reverted, for
  the same reason as before.
- [x] (2026-09-29) **Three further stale figures in this plan were corrected,
      and the gates were re-run at the resulting head `9096c804`.** Reviewing
      the record against the artefacts rather than against personal notes turned
      up three claims that had never been true: the plan called `MAX_BUFFER_SIZE`
      "merely re-exported" when it is private to `cuprum-streams`
      (`rust/cuprum-streams/src/lib.rs:38`) and never re-exported at all, with
      `cuprum-rust` inheriting the cap only indirectly through
      `cuprum_streams::BufferSize::new`; it counted "all eight sub-checks" in
      `make lint` where every local log enumerates **eleven**; and it dated the
      `typos.toml` regeneration "across its seven commits" — a count that was 22
      when written and 24 by the time it was read, so it had never been right in
      any revision. The last of these is now stated as the fact it was standing
      in for: at this head `git log 7f762870..HEAD -- typos.toml` was empty, so
      the branch had carried no estate churn in a commit. (`11039493` later
      committed one line of it; the 2026-10-01 entry below measures that.) Each
      correction is its own
      commit (`59f326ad`, `999880dd`, `9096c804`) so the history shows what was
      believed and when.

  Because those commits followed the `49dae298` gate run, the run stopped being
  a valid citation and the gates were re-run at `9096c804`. Five pass on the
  frozen tree, each log recording the head: `make check-fmt` (676 formatted, 78
  unchanged), `make test` (`cargo nextest` **160/160**;
  `2543 passed, 1 skipped`, the skip a Windows-only case; both moved properties
  `PASSED`, not skipped), `make typecheck` (`ty` 0.0.74, `All checks passed!`),
  `make markdownlint` (0 errors across 78 files, spelling included), and
  `make nixie` (all diagrams validated). **`make lint` reached ten of its
  eleven sub-checks and then aborted**: ruff, interrogate, pylint,
  df12-python-lints, ambrleaks, Skylos, Rustdoc+clippy, Whitaker, typos and
  yamllint all pass, and the final `actionlint` sub-check hung until the outer
  540 s bound terminated the recipe
  (`make: *** [Makefile:387: github-actions-lint] Terminated`). The honest
  reading is **actionlint UNOBSERVED, not passed** — it died before emitting a
  verdict, so no pass may be claimed for it. This is the same host-only
  deadlock M1 hit, not a regression. It was measured rather than inherited: a
  SIGQUIT goroutine dump of a reproducing run shows the wedge as
  `RuleShellcheck.VisitWorkflowPost` → `externalCommand.wait`, with the writer
  goroutine parked in `os.File.Write` and **no child process spawned at all**.
  Seventy runs of the identical command over identical bytes hung 11 times, in
  batches ranging from 6-of-6 to 0-of-10, and adding eight CPU spinners changed
  nothing, so host load is not the trigger. `ci.yml` is the workload: every
  other workflow file returns in 0 s, and the hang disappears entirely
  (`7 of 7`, exit 0, sub-second) when the shellcheck integration is disabled
  with `-shellcheck=`. It is environmental on two grounds. First, that
  contrast: the hung path is the shellcheck handshake specifically, not the
  analysis. Second, and decisively, this branch cannot affect it at all:
  `git diff --name-only 7f762870 HEAD -- .github/` is empty, so all ten
  `.github/workflows/*.yml` files and `.github/actionlint.yaml` are
  byte-identical to the base and every byte `actionlint` reads is `main`'s.
  `make lint` again regenerated `typos.toml` and it was again reverted.
- [x] (2026-09-29) **CodeRabbit's first review of this branch raised one
      finding, and it disproved a constraint this plan had asserted without
      testing.** The PR was marked ready for review at `e7d4fa45` and the review
      that had been `skipped` as a draft immediately ran and returned
      `CHANGES_REQUESTED` with a single actionable comment: the new
      `test_rust_streams_roundtrip_property.py` should import the shared
      `_safe_close` from `cuprum.unittests._rust_stream_test_support` rather
      than defining its own copy. The finding is correct, and acting on it
      showed the plan's stated reason for declining it was false — both
      affected files were already in the change surface and the shared module
      needed no edit, so the cost was zero rather than the 19th file the plan
      claimed. Deduplicated in `b3ae9f20`, which removes the copy from the
      round-trip module **and** the identical one left in the boundary module
      (the same duplication the split had carried across two files), and brings
      the boundary module to 345 lines and the round-trip module to 119. The
      file count stays at 18. `ruff check` and `ruff format --check` are clean
      on all three modules, and the two property modules run `10 passed` under
      `CUPRUM_REQUIRE_RUST_EXTENSION=1`, as does
      `test_extension_build_contract.py`, so the gated registration still
      holds. This is a case where a reviewer found something a deterministic
      gate could not: the duplication was invisible to every lint here, and the
      false premise was invisible to everything, because nothing measures a
      claim until someone acts on it.

  The finding is **fixed after the review, not stale**, and the distinction
  matters because it is what the standing verdict turns on. The review is
  `5354955377`, submitted `2026-09-29T15:42:06Z` against commit `e7d4fa45`
  (`2026-09-29T17:31:05+02:00` = `15:31:05Z`), and the fix `b3ae9f20` lands at
  `15:47:23Z` — five minutes *after* the reviewed head. So the comment was
  accurate when written and describes a tree that no longer exists;
  `git merge-base --is-ancestor b3ae9f20 e7d4fa45` returns false. Re-verified
  against the current head: exactly one `_safe_close` definition remains in
  `cuprum/` (in `_rust_stream_test_support.py:29`), and all six stream-test
  modules import it rather than defining their own, so the requested change is
  fully applied. The thread `PRRT_kwDOQgt8686nLngt` is `isResolved=true` and
  `isOutdated=true`, the latter being GitHub's own record that the line it was
  anchored to has moved. What remains is the reviewer's **decision**, not its
  factual claim: `reviewDecision` stays `CHANGES_REQUESTED` and
  `mergeStateStatus` `BLOCKED` until a fresh pass re-decides, which is why
      a second review was queued rather than the first one argued with.
- [x] (2026-09-29) **CodeRabbit's walkthrough carried one error and one
      warning, and both were still valid against the current head — they are a
      different surface from the inline finding above, so clearing one proves
      nothing about the other.** The walkthrough is issue comment `5745365959`,
      `updated=2026-09-29T17:23:54Z`, and its own markers name the evaluated
      commit: `change_assessment_commit` and `final_review_risk_coverage`
      both read `e7d4fa45`. So its rows describe the same pre-fix tree the
      inline finding did, and each had to be re-checked rather than assumed
      either stale or live.

  **Finding 1 — `Testing (Overall)`, ❌ Error.** *"…changing only the
  `InvalidDescriptor` arm to produce `OSError` would remain undetected by these
  tests."* **Valid, and the best finding of the round.** Every direct-native
  descriptor test paired the invalid descriptor with `buffer_size=0`, so the
  buffer validator answered first and `convert_fd` was never reached; the Rust
  unit tests pin the `RustStreamError::InvalidDescriptor` variant but never
  exercise `From<RustStreamError> for PyErr`. The claim was verified by
  construction rather than by reading: flipping that one arm to
  `PyOSError::new_err` and rebuilding left **every** existing test green. Fixed
  by adding two tests to the already-registered, already-in-surface
  `cuprum/unittests/test_rust_stream_native_order.py` — one per export, each
  using the valid `_VALID_BUFFER_SIZE = 65536` so descriptor conversion is
  genuinely the failing step. Both then fail under the same mutation with
  `OSError: file descriptor must be non-negative`, and pass once it is
  reverted, so the gap is closed and demonstrably so. The suite went from 8 to
  10 tests (`10 passed in 0.06s`) and the file from 226 to 275 lines, inside
  the 400-line limit. The pump case is included because the two exports reach
  the conversion through different call paths, so pinning only
  `rust_consume_stream` would leave the pump's mapping unwitnessed.

  **Finding 2 — `Developer Documentation`, ⚠️ Warning.** *"…
  `docs/ developers-guide.md` still states that
  `stream_pyfunctions:: run_stream_operation` owns '`PumpError` conversion'."*
  **Valid and purely textual.** Line 3501 still named the superseded boundary.
  The error-taxonomy section (`## Rust error taxonomy`) had already been
  updated to describe `From<RustStreamError> for PyErr`, so the guide
  contradicted itself — accurate in one section, stale in another. The
  paragraph now names the typed `RustStreamError` conversion and the single
  `From<RustStreamError> for PyErr` impl, keeping the existing do-not-reuse
  guidance intact.

  Both repairs land in files already inside the 18-file change surface
  (`git diff --name-only 7f762870 HEAD` lists both), so the count holds at 18
  and neither repair needed a new file.

  Committed as `c07671e8` and pushed; the inline thread `4135406026` was
  answered at the same time, so all three of CodeRabbit's surfaces — the inline
  finding and both walkthrough rows — now carry an explicit disposition. Note
  the walkthrough was **paused** when it produced these rows, and its
  `change_assessment_commit` markers still read `e7d4fa45`. A paused
  walkthrough is not a fresh assessment of the current candidate, which is why
  every row had to be re-derived against the live tree rather than read as a
  verdict on this head.
- [x] (2026-09-29) **The required `coverage` job failed twice at `1714ac0d`, on
  a test this branch does not touch, for a reason that is environmental and now
  measured rather than assumed.** The failing test is
  `cuprum/unittests/test_doctest_warning_contract.py::test_pinned_doctest_route_rejects_a_warning`
  and the verdict both times is
  `Failed: Timeout (>30.0s) from pytest-timeout` — the global bound at
  `pyproject.toml:370`. Everything else in that attempt is green: the other
  **16 of 17** jobs succeed, including `lint-test`, all four
  `Typecheck and test` interpreters, both extension-gated lanes,
  `benchmark-ratchet` and every wheel build, and the coverage job is the only
  failure. The verdict is pinned to the **job**, not the run: job
  `109509268252` still reports `conclusion=failure` for the window
  `16:32:40Z–16:45:10Z`, while the run's own aggregate later became
  `completed/cancelled` when the rerun was superseded — the concurrency group
  cancels the run, but a job's first attempt keeps the conclusion it reached.
  The first question is ownership, and it is answered by three independent
  facts. The file is **byte-identical to base `7f762870`** — `b8f96ae4` is its
  blob at both `b7d1b109` and `1714ac0d` — it is absent from the branch's
  18-file change surface (`git diff --name-only b7d1b109 HEAD` lists only the
  Makefile, the two property modules, the developers' guide and this plan), and
  `main` has not touched it since the base either
  (`git log 7f762870..origin/main -- <path>` is empty). It also passed in the
  coverage job at `b7d1b109` (`36356062517`) on the same bytes.

  The second question is mechanism. The coverage job is the **only** lane that
  runs this test without `setup-dev-fast`:
  `grep -n setup-dev-fast .github/workflows/ci.yml` returns lines 290 and 791,
  inside `lint-test` and `extension-tests`, and the coverage job's own 29-step
  list contains no such step — it installs Rust `1.85.0` and, on a tool-cache
  miss, the `nightly-2026-05-28` makeutil toolchain, but never
  `nightly-2026-08-23`. (Both the failing job and the green one report 29 steps
  and an empty dev-fast selection, so the gap is structural rather than a
  property of one run.) The test hard-codes that toolchain as
  `RUSTUP_TOOLCHAIN`, and rustup **auto-installs an uninstalled toolchain on
  demand** (confirmed directly:
  `RUSTUP_TOOLCHAIN=nightly-1999-01-01 cargo --version` begins
  `syncing channel updates` rather than failing). So when the nightly is
  absent, the download happens inside the test's own `subprocess.run`, under
  the 30 s pytest bound, and the download is **595 MB**. Reproduced locally by
  pointing `RUSTUP_HOME` at an empty directory: the test takes **11.52 s and
  12.71 s cold** against **0.49 s warm**, a 23–26× differential measured on an
  idle 6-core host. That is already a third of the bound before the runner is
  loaded at all; on the job's 2-vCPU Ubicloud runner with a concurrent compile,
  the same download crosses it. The passing run is consistent with this rather
  than against it: in `36356062517` the test took **7.4 s** (22:52:34.10 →
  22:52:41.49), fifteen times its warm local time but inside the bound — a
  warm-but-slow path, not a cold download. What falsifies "this is a branch
  regression" is the combination: unchanged bytes, an unchanged toolchain pin,
  and a cost that is purely a download. The correct fix — provisioning the
  nightly in the coverage job, or raising that test's timeout — belongs to
  `main` and not to this branch, which is why it is recorded here rather than
  patched. One nearby failure was checked and is **not** the same defect: the
  coverage job on `d7037cc41` (a different branch) fails with
  `sccache: error: Timed out waiting for server startup`, an unrelated cause.

  The rerun settles it. `gh run rerun --failed` over the same job re-executed
  the same test on the same bytes at the same head and it **passed in 8.7 s**
  (17:14:22.54 → 17:14:31.27, job `109524590630`) — matching the earlier green
  run's 7.4 s rather than the 30 s that killed it, which is the signature of a
  cost that varies with the runner rather than of a deterministic failure. That
  rerun is logged `cancelled` because pushing the plan commit superseded it
  through the `ci-${{ github.ref }}` concurrency group; the test had already
  passed by then, so the cancellation is not a verdict on it.

  A later head settles it without relying on that cancelled attempt. Run
  `36607134592` at `3dec047b` is `completed/success` with **17 of 17** jobs
  green and **zero** non-success, `coverage` included. This is the cleanest
  evidence of the three, because the run itself is not cancelled and the job
  completed inside its own `timeout-minutes: 65`, so the pass is a verdict
  rather than an interrupted attempt. The `coverage` job failing twice at
  `1714ac0d` and passing at both `b7d1b109` and `3dec047b` — three heads, two
  of them this branch's own — is the pattern a wall-clock-sensitive cost
  produces, not the pattern a branch defect produces.

  The current head repeats it, so no green run in this record is stale. Run
  `36610325211` at `58ce2f66` is `completed/success`, **17 of 17**, **zero**
  non-success. Its `coverage` job runs `18:21:06Z → 18:32:16Z`, i.e. **11 m 10
  s** wall clock against its own `timeout-minutes: 65` bound — a fifth of the
  budget, so that lane is nowhere near its ceiling either.

  Four heads now bracket the question. The one failure is `1714ac0d`, where the
  test hit the 30 s bound twice. The three passes are `b7d1b109` (the test at
  **7.4 s**), the cancelled rerun at `1714ac0d` (**8.7 s**, the same head and
  bytes, so it cannot be cited as a head-level pass even though the test itself
  completed), and `58ce2f66` (the whole coverage job at 11 m 10 s — a job-level
  figure covering far more than this one test, so it bounds the cost rather
  than measuring it). One head failed, two completed cleanly, and no passing
  observation is anywhere near the bound.
- [x] (2026-09-29) **A second CodeRabbit review was requested at `c99807ce`.**
  The round-1 review (`5354955377`) stands `CHANGES_REQUESTED` and
  `mergeStateStatus` is `BLOCKED`, so a fresh review is required to clear the
  decision rather than merely to re-read the fix. Its single inline thread
  (`PRRT_kwDOQgt8686nLngt`, on `test_rust_streams_roundtrip_property.py`)
  already reads `isResolved=true` / `isOutdated=true`, so the disposition is
  recorded on the thread as well as in the fix commit. The request was queued
  through the managed route — `comenq put leynos/cuprum 432 …` → **`ac1c5052`,
  ETA ~7h 57m** — after confirming no request for this PR was already pending
  and that the round-1 rate limit (1 review/hour) had cleared. The queue is the
  mechanism, so delivery of the comment is not the review and the review is not
  an approval; the commit CodeRabbit inspects must be verified after it runs.
  Only the execplan changed since the `1714ac0d` gate run, so the code gates
  from that run still hold; the docs gates were re-run at `c99807ce` and pass.

  **The round-1 surface count was three, and it is four.** The inline finding,
  the walkthrough's two rows and the second review `5356273356` were already
  dispositioned, but a fourth surface existed and was missed: issue comment
  `5890588184`, the **focused reply** to `5890555008` ("assess the
  implementation in this PR for completeness and correctness"). That reply is
  where the file-size finding first appeared — *"the boundary property module
  grew from 246 to **431 lines**"* — and it is a **different surface from the
  review walkthrough**, which is why the walkthrough's rows never carried it.
  It is also the origin of the first sentence of the governing request, so its
  disposition matters beyond bookkeeping.

  Its verdict is **valid when written, and superseded by construction.** The
  comment was last edited `2026-09-29T13:53:10Z`; the split landed in
  `bd41d2c4` at `14:31:42Z` (**+38 minutes**) and the deduplication in
  `b3ae9f20`. At the current head the two modules are **345** and **119**
  lines, both well under the cap, and the new module is registered in
  `EXTENSION_TEST_TARGETS` as the reply required — so all three of its
  instructions (split a cohesive group, register if extension-required, rerun
  the gates) were carried out. The file-count half of the reply is *also*
  correct and no longer live: it said "the PR currently changes 17 files … so
  one new file remains within the plan's 18-file tolerance", and the surface
  then stood at exactly **18 of 18** (19 on the branch and 18 at the merge ref
  once `11039493` committed `typos.toml`; see 2026-10-01). Two of the reply's
  three forward-looking numbers therefore describe the pre-split tree; the
  third, the destination, was reached exactly.

  **CodeRabbit has since re-confirmed the inline half of that reporting and
  withdrawn the other half's warrant.** Reply `4136525485` (`17:42:16Z`, posted
  against `c07671e8`) re-ran the `_safe_close` search at the current head,
  reported the one definition in `cuprum/`, the two module sizes at 119 and 345
  lines, and retracted its own earlier claim that verification was unavailable:
  *"I was wrong to say verification was unavailable in my previous reply."* It
  then marked the thread **✅ Review thread resolved.** That is the
  builder-satisfied condition the recovery guide names for a documentation/row
  dispute, so no further tagged request is outstanding for it. The walkthrough
  rows remain the only unresolved surface, and they are addressed by the queued
  review `ac1c5052` rather than by argument, because a row's disposition is the
  reviewer's to reissue.
- [x] (2026-09-29) **The branch is green at `778f8e67`, and Sourcery is the one
  reviewer that never rendered the diff.** Run `36613396061` at the current
  head is `completed/success`: **17 of 17** jobs and **zero** non-success,
  taken from the `gh run watch --exit-status` log (`EXIT=0`, no failure marker
  anywhere in it) rather than from a summary. The `coverage` job this work
  existed to fix ran **11 m 6 s** against its `timeout-minutes: 65`, and both
  extension-gated lanes, Windows included, passed inside 1 m 25 s. That is the
  third green head at which the coverage job has run, against the single
  failing head `1714ac0d`. (This read "fourth" until 2026-09-30, when the
  ordinals in this plan were re-derived together and found to be three
  different counts; see the seventh-observation entry below for the
  enumeration.)

  **"Four surfaces" was a CodeRabbit count, and this PR has other reviewers
  besides CodeRabbit.** Every entry above, including the correction that raised
  the count from three to four, is scoped to CodeRabbit's own surfaces. Five
  further reviewer identities post to this PR and none was recorded:

  | Reviewer  | Surface                                 | State at `778f8e67`                   |
  | --------- | --------------------------------------- | ------------------------------------- |
  | CodeScene | 28 `APPROVED` reviews, plus a check-run | `APPROVED` at `778f8e67`, `18:38:24Z` |
  | Gecko     | `Gecko Security Review` check-run       | `success`, "No vulnerabilities found" |
  | Loom      | `Loom model smoke test` check-run       | `success`                             |
  | Sourcery  | `Sourcery review` check-run             | `skipped` — **never reviewed**        |
  | Codex     | summary issue comment `5893419759`      | assessed `e7d4fa4`; stale (see below) |

  One is adverse, and it stops short of being a review. Sourcery declined with
  *"your pull request is larger than the review limit of 150,000 diff
  characters"* (`15:33:10Z`, against `e7d4fa45`). The limit is real and the
  diagnosis is measurable: this branch's diff from its merge base is
  **293,899** characters, of which the execplan alone is **162,889** — 55% of
  the whole surface from one file. The PR body's claim that CI is green "at
  each head" is true of CI and silent about *this* reviewer, which never
  rendered the diff at all.

  A later revision of this entry said trimming the execplan "would not lift the
  gate", because the branch was "86% over a limit no rearrangement of its own
  text can reach". The figures have moved against that sentence and the
  sentence should have been re-derived rather than carried. The non-plan
  remainder is now **131,010** characters — **87% of the 150,000 cap, and under
  it by 18,990**. So it is no longer true that no trimming could help; a
  sufficiently aggressive rewrite of the execplan could bring this branch
  inside a limit it currently exceeds by 96%. That said, the conclusion does
  not change, for a reason worth stating precisely: the trimmable text is the
  mandated living artefact of this plan, and cutting 163 KB of it to satisfy a
  reviewer that has never read this branch would destroy the record the plan
  exists to keep. Sourcery remains **out of scope for this branch** — now as a
  measured budget statement rather than as an impossibility claim.

  Re-derived again at committed head `97f51d41` on 2026-09-30, and the
  interesting result is *which* figures moved. Total is now **295,517** and
  this plan's share is **164,507** — both up by exactly **1,618**, which is the
  plan growth since the previous reading. The non-plan remainder is
  **131,010**, *unchanged*. That is the self-growth principle demonstrated
  cleanly rather than asserted: the figure derived from the growing artefact
  drifts one-for-one with it, while the load-bearing figure — the one the
  budget argument actually rests on — is a property of the code and does not
  move at all. So the conclusion above is stable, and only the two numbers that
  describe the plan's own bulk are revision-dependent and need re-deriving
  whenever they are quoted.

  Codex is the mirror case: it did run — *"Code Review ✅ Completed,
  `2026-09-29T15:36:37Z`, `e7d4fa4`"* — and it did signal clean, with the 👍
  its own help text reserves for "all reviews finish with no findings". The
  first version of this entry said its summary carried **no reaction at all**.
  That was wrong, and wrong in the way this plan has been wrong before: the
  reaction is real and is simply not where the sentence looked. It sits on the
  **pull request** (`+1`, `chatgpt-codex-connector[bot]`,
  `2026-09-29T15:36:40Z`), not on Codex's summary comment, whose own reaction
  list is genuinely empty. A search that returns nothing is only evidence of
  absence when it is a search of the right thing, and "no reaction at all" is a
  negative claim across surfaces rather than a measurement of one.

  The corrected verdict is narrower but still adverse, and the distinction
  matters. Codex *did* finish clean. It finished clean **at `e7d4fa4`**, and
  the share of this branch that postdates that head includes `c07671e8`, which
  closed both of the walkthrough's rows, and `b3ae9f20`, the `_safe_close`
  deduplication the inline thread raised. A clean signal at a stale head is a
  statement about that head, so nothing is claimed here from it about the
  current tree in either direction.

  The gap was written as "ten commits" and re-measured on 2026-09-30 as
  `git rev-list --count e7d4fa45..HEAD` = **22**, because every push since
  widened it. The count is deliberately no longer stated as a number: an
  interval that grows with each commit is the same self-invalidating figure as
  this plan's own size
  (`[[plan-self-growth-invalidates-its-own-budget-claims]]`), and what the
  argument actually needs is the *set* of commits that postdate the reviewed
  head, not its cardinality. `git rev-list --count e7d4fa45..<head>` reproduces
  the current value for any head that cares to ask.

  CodeScene is the opposite again, and it is the only current-head approval on
  the PR: it approved **every** head from `c07671e8` through `778f8e67`, five
  consecutive heads, and its check-run at the head is `success`. That does not
  clear `BLOCKED`. A CodeScene `APPROVED` and a CodeRabbit `CHANGES_REQUESTED`
  are separate reviewer decisions, and a merge gate consults the one that is
  adverse, so the CodeRabbit re-decision remains the single outstanding item.
- [x] (2026-09-29) **The PR body now carries the same reviewer model, and the
  commit that recorded it was pushed after the green run rather than before.**
  The body's Review state section listed CodeRabbit alone; it now adds the four
  other reviewers with their measured states, and its Validation section leads
  with `778f8e67` / run `36613396061` and the coverage job's 11 m 6 s. The
  update was sent as a JSON `--input` payload because `gh api -f body=@file`
  posts the literal path, and read back non-vacuously: both sides asserted over
  12,000 characters before comparison, and they match modulo the single
  trailing newline GitHub appends (15,662 against 15,663). Seven probes of the
  new claims were each confirmed present in the live body.

  Pushing `3bd057d1` supersedes run `36613396061` through the concurrency
  group, so the green run now describes the parent of the head rather than the
  head. That is deliberate and recorded rather than left implicit: `3bd057d1`
  changes only this execplan, which no test, lint or build target reads, and CI
  will re-run at the new head anyway. The alternative — never recording an
  observed result because recording it makes the record one commit stale —
  would leave the plan permanently describing a tree `main` can no longer reach.
- [x] (2026-09-29) **A walkthrough edit timestamp is not a walkthrough
  re-assessment, and the two failed rows survived one.** Issue comment
  `5745365959` was edited at `19:25:51Z`, 35 seconds after the PR body update
  and long after the run that had been cited as the queue's trigger. Read as a
  timestamp it looks like the walkthrough re-examining the branch. Read as
  content it is nothing of the kind: the edit changed **exactly two lines**,
  both `✅ Passed` rows. The Title check moved from "clearly describes the main
  change" to "accurately describes the central RustStreamError change", and the
  Description check from "tests, and validation results" to "testing, and
  validation" — the prose a body rewrite would move, and only that.

  Everything that would have to change for the edit to mean a re-assessment did
  not change. `change_assessment_commit` and `final_review_risk_coverage` both
  still name `e7d4fa457f73a63750bc23c8c7da2d1a9f77c588`, the branch is still
  marked "review paused by coderabbit.ai", and the pre-merge table still reads
  "❌ Failed checks (1 error, 1 warning)" with `Testing (Overall)` ❌ and
  `Developer Documentation` ⚠️. A diff of the whole comment against the copy
  taken before the edit is six lines long, all of it inside that one table.

  This is the general shape of a trap this plan has hit from several
  directions: an artefact's *mtime* is not its *content*, and a surface that
  moves is not a surface that re-decided. It also settles a question the queue
  left open. A body edit **can** provoke a walkthrough re-render without
  provoking a review, so the re-render is not evidence that `ac1c5052` has been
  consumed; the failed rows are refreshed by a review and by nothing else,
  which is exactly why they are still waiting.
- [x] (2026-09-29) **The head after the correction is green, so the two
  substance changes are covered by a run rather than resting on the argument
  that they could not matter.** Run `36619125194` at `7c5059eb` is
  `completed/success`: **17 of 17** jobs, **zero** failure markers in the
  `gh run watch --exit-status` log, `EXIT=0`. The `coverage` job this work
  existed to fix ran **11 m 4 s** against its `timeout-minutes: 65`, the fourth
  consecutive green observation of it against the single failing head
  `1714ac0d`. That run carries the Codex correction and the PR-body change,
  where `36613396061` predated both; it does not carry this paragraph, which
  was written after it and will be covered by the next run, in the same way as
  every recording commit before it.

  Two claims made in the plan's own prose were re-verified against the
  committed tree rather than against the copy they were written from, and both
  hold at `HEAD`: `docs/developers-guide.md` contains no "`PumpError`
  conversion" attribution and three `From<RustStreamError> for PyErr`
  references, the two property modules are **345** and **119** lines, the
  roundtrip module is in `EXTENSION_TEST_TARGETS`, and `def _safe_close`
  appears in exactly one file under `cuprum/`. The plan had asserted each of
  these before; asserting them a second time against a different artefact is
  what turns them from a record of what was done into a measurement of what is
  there.
- [x] (2026-09-29) **A second artefact corroborates the paused state without
  reading the comment's own text.** The check-run rollup at the head lists **
  `Kody Code Review` as `skipped`** — CodeRabbit's own check declining to run,
  which is the same fact the paused banner asserts, reached from a different
  API. A skipped check is a verdict the reviewer publishes about itself, and it
  agrees with the paused banner.

  The 👀 reaction is a weaker witness than it looks, because its timestamp is a
  mutable field that this plan caught moving twice. The reaction first read
  `19:22:34Z`, then `20:27:19Z` (id `534438116`), and now reads `20:45:35Z` (id
  `534479908`). A reaction is a toggle — one row per identity and content,
  never two — so each id is a fresh row replacing the last, and the field can
  only ever record the *latest* time the identity looked. Each reading was
  accurate when taken; the plan keeps all three so a reader can tell a moved
  field from a changed one, and stops quoting it as though it were stable. The
  reaction is a heartbeat, not a ledger.

  The walkthrough edits are the opposite case, and the distinction is the whole
  point. Three `updated_at` values are now on record for comment `5745365959`:
  `19:25:51Z`, then `20:27:18Z`, then `20:45:35Z`. Four captures sit at or
  after the first of those — at `19:34:34Z`, `20:38:03Z`, `20:41:19Z` and
  `20:48:47Z` — and all four are **byte-identical at 14,729 bytes, SHA-256
  `8306c4cb…`**. So two recorded edits landed inside a span whose every sampled
  rendering is the same document: two writes, zero bytes of change, and the
  contrast with the `19:25:51Z` write, which did move two rows, is the finding.

  A caveat belongs here rather than in a footnote, because it is easy to get
  wrong in exactly this way. Comparing the two earliest captures, `17:32:35Z`
  and `17:50:20Z`, also yields a six-line diff — the same two `✅ Passed` rows,
  there reordered and reworded. That window has no `updated_at` of its own,
  since the field reports only the last write, so the claim cannot be "only one
  edit ever changed content": at least two writes did, and the later of them is
  the `19:25:51Z` edit recorded above.

  The rule this yields is worth stating, because a first draft of this entry
  broke it: **a diff witnesses an edit only when both captures sit outside that
  edit's window.** Straddle the edit and the diff reports the change but
  misnames its cause, which is how two moved rows came to be blamed on the
  `20:27:18Z` edit. Pin the window first; the finding follows or it does not.

  That is the whole edit history of this comment over the ten days it has
  existed (`created_at` `2026-09-19T21:17:11Z`, last write
  `2026-09-29T20:45:35Z`), as far as captures can witness it: writes that moved
  two passed-check descriptions, and writes that moved nothing. No write has
  touched an assessment field — not `change_assessment_commit`, not
  `final_review_risk_coverage`, not the failed-check table. The reviewers'
  dispositions have not been revisited; only the prose around them has.

  What survives is the point. Every assessment field still names
  `e7d4fa457f73a63750bc23c8c7da2d1a9f77c588`, the paused banner stands (twice,
  in the comment), and the pre-merge table still reads "❌ Failed checks (1
  error, 1 warning)" with `Testing (Overall)` ❌ and `Developer Documentation`
  ⚠️.

  That settles the queue question negatively for the fourth time. `ac1c5052` is
  **still pending** — `comenq list` shows it, and no new CodeRabbit review
  exists; the newest are still `15:42:06Z` and `17:42:16Z`. A watching reaction
  and a no-op edit are neither a review nor a verdict, and the failed rows are
  refreshed by a review and by nothing else.
- [x] (2026-09-29) **One figure in this plan's prose was wrong, and it was
  written where no gate looks.** Commit `3bd057d1`'s subject reads "the
  **four** non-CodeRabbit reviewers". The count is **five** — CodeScene, Gecko,
  Loom, Sourcery and Codex — and the table this plan commits carries five data
  rows. The plan body and the PR body both say five and both list five, so the
  error is confined to an immutable commit subject, where nothing checks it and
  where a future reader grepping the log would meet it. It is recorded here
  rather than rewritten, because the branch is shared and an amended subject
  would invalidate the hashes this plan's evidence cites throughout.

  The failure is the one this plan keeps rediscovering, in a new place: the
  number was composed while writing the commit message rather than read off the
  table that had just been written, and a commit subject is the one document in
  the change surface that no gate, no reviewer and no test will ever open. It
  is the latest figure in a session that has now needed four re-derivations,
  after the coverage-evidence split, the three stale plan figures, and the two
  coverage-job numbers.
- [x] (2026-09-29) **The run carrying these corrections is green, and the local
  gates were run against the same tree.** Run `36630471791` at `184c5b65` is
  `completed/success`: **17 of 17** jobs `success` and **zero** non-success,
  read from the jobs API rather than inferred from the run's own conclusion. The
  `coverage` job this work existed to fix ran `21:10:07Z`→`21:21:38Z`, or **11
  m 31 s** against its `timeout-minutes: 65` — the sixth consecutive green
  observation of it against the single failing head `1714ac0d`. (This read
  "fifth" until 2026-09-30; the increment had skipped the observation at
  `dbbe9ee6`, which sits between `7c5059eb` and this head.)

  Locally, `make check-fmt` and `make markdownlint` both exited `0` on the
  committed tree. `make lint` reached its final `actionlint` step and wedged
  there until its 1500-second bound killed it (`LINT_RC=124`) — the host-only
  wedge already recorded in this plan, not a finding. Two facts keep that from
  being a silent gap: the branch changes **no** `.github/` file at all
  (`git diff --name-only <base> HEAD -- .github/` is empty, so every byte
  `actionlint` reads is `main`'s), and re-running it with the shellcheck
  integration disabled — the workaround this plan documents — exits `0` with no
  findings in under a second. `actionlint` is therefore **unobserved rather
  than failed** locally, and covered by CI, where it passed inside the 17.
  Every sub-check before it — ruff, interrogate, pylint, df12-python-lints,
  ambrleaks, Skylos, Rustdoc+clippy, Whitaker, typos, yamllint — completed
  clean on this tree, which is the part carrying these edits' risk. Skylos is
  worth naming specifically: this branch rewrites a good deal of
  `docs/developers-guide.md`, a 363 KB file, and this plan records elsewhere
  that Skylos skips an oversized document and can then report a
  documentation-derived symbol as dead (`SKY-U001`). The scan ran, emitted
  **zero** `SKY-` findings, and the chain advanced past it to Rustdoc, which is
  what proves it ran rather than skipped. The hazard did not fire.
- [x] (2026-09-30) **The next head is green, and this time with a job rollup
  rather than a run verdict.** Run `36633831081` at `97f51d41` is
  `completed/success` with **17 of 17** jobs `success` and **zero**
  non-success, read from the jobs API. Its `coverage` job ran `21:42:23Z`→
  `21:53:32Z`, or **11 m 9 s** against its `timeout-minutes: 65`.

  **The size and surface claims were re-derived rather than carried.** At the
  committed head `git diff --name-only <base> HEAD` lists **18** files once the
  lockfile and this plan are excluded, so the plan's `18 of 18` tolerance claim
  holds. The Sourcery figures re-derive exactly too: total **303,426**
  characters, of which this plan is **171,885** and the non-plan remainder is
  **131,541** — but **531** of that remainder is the uncommitted `typos.toml`
  regeneration (gate churn, never drift-checked in CI), so the committed
  remainder is `131,541 − 531 =`**`131,010`**, precisely the figure the
  Sourcery entry quotes. Reading the working tree without subtracting that
  churn would have shown 19 files and a 131,541 remainder, and produced a
  "correction" that was itself the error. The lesson from
  `[[plan-self-growth-invalidates-its-own-budget-claims]]` cuts both ways: the
  plan's size does drift, but a re-derivation must be taken at the same
  revision as the claim, not across an uncommitted working tree.

  **Correction (2026-10-01): this entry's own commit falsified its
  "uncommitted" framing.** `11039493` staged the regenerated `typos.toml` line
  in the same commit that carries this text, so the file is committed from that
  point onward; `git diff --numstat 7f762870 HEAD -- typos.toml` is `1 1`. The
  `531` and `131,541` readings above were taken on the working tree as this
  entry was drafted and are left as that measurement. The count at the current
  head is re-measured in the 2026-10-01 entry below; the Sourcery figures are
  left as this entry's own reading.

  This entry was first drafted while the Lody GitHub credential broker was down
  session-wide
  (`Cannot verify GitHub identity preferences with Lody… no GitHub
  operation was attempted`;
  the failure reached even `gh auth status` and `gh api /rate_limit`). The
  draft asserted the rollup could not be obtained and offered the run verdict
  as a weaker substitute. The broker recovered before that draft was committed,
  both queued retries landed (`JOBS success:17`), and the assertion became
  false — so it is recorded here as a falsified draft rather than quietly
  replaced. The general form: a *transient tool outage* is not evidence about
  the artefact being measured, and an entry written from one ages into a false
  claim the moment the tool returns.

  **Re-deriving the coverage streak found three different counts in this
  plan.** The seven observations of the `coverage` job at or after the
  `1714ac0d` failures, in branch-history order, are: `3dec047b` (1, success),
  `58ce2f66` (2), `778f8e67` (3), `7c5059eb` (4), `dbbe9ee6` (5), `184c5b65`
  (6), `97f51d41` (7). Against that enumeration the earlier entries in this
  plan had `778f8e67` reading "fourth" when it is the third, `184c5b65` reading
  "fifth" when it is the sixth, and only `7c5059eb` reading "fourth" correctly
  — because the increment to "fifth" skipped `dbbe9ee6`, a green run this plan
  had never mentioned at all (`grep -n dbbe9ee6` returned nothing). A count
  incremented by hand across a hand-maintained list failed the same way the
  diff attributions did: the arithmetic was never re-derived from the source
  list. Both wrong ordinals are corrected above, with the correction dated in
  place rather than silently rewritten.

  The `1714ac0d` half of the phrase was checked too, because its `coverage` job
  reads `cancelled` in the jobs API and the plan calls it the failing head.
  Both are right: that run is at `run_attempt: 3`, the concurrency group
  cancelled the later attempt, and the jobs API reports the latest attempt —
  while the two cancelled-and-rerun attempts kept their own `failure`
  conclusions. This plan already documents exactly that semantics. The phrase
  survives; the numbers beside it did not.
- [x] (2026-09-30) **The re-derivation commit is gated and green, and the
  `nixie` failure from the previous pass was resource pressure rather than a
  defect.** Commit `11039493` carries every correction above. All three docs
  gates exit `0` against it, with the tree clean before and after and `HEAD`
  unchanged across the run: `make check-fmt` (ruff
  `676 files already formatted`, `cargo fmt` clean, mdtablefix
  `78 files left unchanged`), `make markdownlint` (markdownlint-cli2
  `0 issues in 0 files`, spelling sub-gate regenerating `typos.toml`
  **byte-identically**), and `make nixie` (all diagrams validated,
  `docs/cuprum-design.md` included). No gate aborted, so no sub-check was left
  unobserved.

  The gate citation is for `11039493` and does **not** carry this paragraph,
  which was written after it — the same self-recording regress this plan notes
  elsewhere, resolved the same way: the paragraph is covered by the next run,
  as every recording commit before it was. The `nixie` and `check-fmt` verdicts
  are unaffected by prose appended to a Markdown file that neither gate parses
  for diagram content; `markdownlint` re-reads it, and the reflow it demands is
  `make fmt`'s job.

  The earlier `can't start new thread` inside `docs/cuprum-design.md` did not
  recur, and the surrounding numbers say why it should not be recorded as a
  Mermaid finding. That failing run had the shared cgroup at
  `pids.current=8072` against `pids.max=8192` — within 120 slots of the ceiling
  — while the passing re-run saw ~1052 processes and `loadavg 3.46`. The nixie
  log carries no parse error in either pass, and the same file's diagrams
  validated both times. The one thing the counter does *not* do is cleanly
  separate pass from fail: the failing pass sampled 7963 and a *passing* pass
  sampled 8072, so a **sampled** PID count is not by itself the discriminator.
  It is recorded as a plausible mechanism, corroborated by the absence of any
  content error, rather than as a diagnosed cause.

  A pre-existing label detail is noted so it is not mistaken for a regression:
  the `stateDiagram-v2` block at `docs/cuprum-design.md:2555` is reported by
  nixie as `<unknown>`, meaning the fence carries no recognizable Mermaid type
  keyword. The identical pair appears in the prior green run and nixie exits
  `0` on it, so this predates the branch.
- [x] (2026-09-30) **Both walkthrough rows are stale, and the two commits that
  closed them are the proof.** The comment was captured again at
  `2026-09-29T22:23:31Z` (`/tmp/611-walkthrough-2131.md`, 14,729 bytes, SHA-256
  `3d9d50c1…`) — the **first content change** after four consecutive captures at
  `8306c4cb…` (`19:34:34Z`, `20:38:03Z`, `20:41:19Z`, `20:48:47Z`). The diff
  against the last of those, `/tmp/611-walkthrough-2045.md`, is six lines, both
  of them the `Title check` / `Description check` rows reworded; the
  failed-checks table, the paused banner and the `e7d4fa45` markers are
  untouched. So this was a body-driven re-render, not a review, exactly as the
  plan's own rule says: **a row's disposition is the reviewer's to reissue, and
  only a review reissues it.**

  The two rows it still shows both name a defect that has since been fixed, and
  the fix commit is the same one for each. `c07671e8` — *"Close CodeRabbit's
  two walkthrough findings"* — modifies exactly two tracked files:

  | Row                          | Claim                                                                                                                                                                               | State on this tree                                                                                                                                                                                                                                                                                               |
  | ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
  | `Testing (Overall)` ❌       | "they do not guard the native `InvalidDescriptor` to `ValueError` conversion… add an extension-required direct-native test that calls `rust_consume_stream(-1, buffer_size=65536)`" | The test exists: `test_rust_stream_native_order.py:236-253`, `pytest.raises(ValueError, match="file descriptor")` on `rust_consume_stream(-1, buffer_size=_VALID_BUFFER_SIZE)` — the row's own prescription, including the valid-buffer detail that makes the converter reachable. Registered at `Makefile:175`. |
  | `Developer Documentation` ⚠️ | "`docs/developers-guide.md` still states that `run_stream_operation` owns '`PumpError` conversion' (around line 3501)"                                                              | `git grep "PumpError conversion" -- docs/` returns **nothing**. The paragraph at 3499-3509 describes the typed conversion and the single `From<RustStreamError> for PyErr` impl in the reviewer's own terms, and says *"The conversion itself is not written here"*.                                             |

  Both were true of `e7d4fa45` and are false of `97f51d41`:
  `git merge-base --is-ancestor e7d4fa45 c07671e8` confirms the fix commit is
  19 commits downstream of the head the walkthrough still evaluates. This is the
  `change_assessment_commit` staleness the plan records elsewhere, reached now
  from the rows' own text rather than from the markers — and it is why the
  entries treating these rows as the "only unresolved surface" should be read
  as *unrefreshed*, not as *substantiated*.

  One caveat this does not discharge: a body edit can re-render a walkthrough
  without provoking a review, so nothing here demonstrates the queued review
  `ac1c5052` has been consumed. The rows will clear when a review runs against
  a commit at or after `c07671e8`, and by nothing else.
- [x] (2026-09-30) **The prediction above was borne out and the stale
  `CHANGES_REQUESTED` is what remains.** Review `5360266033` (`00:59:02Z`,
  `coderabbitai[bot]`, state `COMMENTED`) is the first review against a commit
  at or after `c07671e8` — it assesses the head `005d358f` — and the
  walkthrough re-rendered with it. `change_assessment_commit` is now
  `005d358fd26383c3be6c713c2c44af7a770e60e6`, **not** `e7d4fa45`, and the two
  rows the entries above proved stale are **gone**. They cleared by the
  mechanism those entries named, and by nothing else. The `Failed checks` table
  now carries a *different*, freshly-assessed row (below), which is the proof
  that this is a re-decision rather than the staleness it replaced.

  Two review-body findings arrived with it, both `🔵 Trivial`, both tagged
  `[type:docstyle]` / `[type:spelling]`, and both against this file:

  - `:1518` — use the en-GB-oxendict "recognizable". **This one was already a
    hard CI failure, not a style preference.** Run `36641187043` at `005d358f`
    failed `lint-test` at step 33, *"Run lint, including Skylos dead-code
    detection"*, erroring in the `spelling` sub-gate on exactly this token:

    ```text
    error: `recognisable` should be `recognizable`
       ╭▸ docs/execplans/6-1-1-…-cuprum-rust.md:1518:54
    make: *** [Makefile:408: spelling] Error 2
    ```

    The word entered in `843370d2`. Fixed by the mechanical substitution; the
    job log is `/tmp/611-linttest-005d.log` (105,092 bytes, 731 lines) fetched
    via `gh run view 36641187043 --log-failed`, after a direct
    `gh api …/jobs/<id>/logs` download returned **0 bytes** — worth recording,
    because an empty download is indistinguishable from a grep that matched
    nothing, and only the `--log-failed` route revealed the failure.

  - `:1224` — remove first- and second-person phrasing, "including the
    identified occurrence outside the diff hunk and the additional noted
    locations". One correction to the finding's own coordinates, recorded
    because it changes what "the additional noted locations" means: its worked
    example is right — `834` carried a first-person assertion of memory at the
    assessed head — but its two "also applies to" lines, `1197` and `1370`, have
    no first- or second-person pronoun at *any* revision checked (neither at the
    assessed base `e7d4fa45` nor at this head). The two extra sites the finding
    implies do not exist where it says they do; the real ones were found by
    sweeping. The rule is real and repo-local: *"Avoid first and second person
    personal pronouns outside the `README.md` file"*
    (`docs/documentation-style-guide.md:32`), with no execplan exemption. The
    review's own worked example was one site, and its two "also applies to"
    lines are not sites at all; a full-file sweep found **nine** occurrences
    (`grep -oE` over the assessed head, counted rather than hand-summed), of
    which **seven are authorial voice and are rewritten**, and **two are
    verbatim quotations deliberately left word-for-word**, because editing a
    quotation to satisfy a style rule would falsify the record. Those two are
    CodeRabbit's retraction of its own earlier claim (line 1155) and Sourcery's
    decline message quoting the 150,000-character review limit (line 1189);
    their text is quoted in place and deliberately not repeated here, so the
    sweep this entry describes stays reproducible.

    One of the seven is a *user's* quoted question — the "why did my throughput
    change" wording that `5.1.1`'s changelog entry answered — preserved by naming
    its speaker rather than deleted or reworded, since the voice is the
    user's, not the author's.

    The sweep also caught one site the review did **not** name: a
    second-person address to the reader in the 2026-09-28 rebase entry, since
    rewritten in the third person. That is the argument for sweeping rather
    than fixing a supplied list.

  The substantive third finding is not in the review body at all; it is the
  walkthrough's replacement row. It is assessed and dispositioned separately
  below.
- [x] (2026-09-30) **The walkthrough's new failed row is mostly refuted, and the
  part that survives is already covered and deliberate.** The row —
  `Testing (Unit And Behavioural)` ❌, now carried in `change_assessment_commit`
  `005d358f`'s walkthrough — claims that
  `rust/cuprum-rust/src/stream_error_behaviour.rs` "calls private
  `validate_buffer_size`, `convert_fd`, and `RustStreamError::from` directly.
  It never creates a `PyErr` or calls an exported PyO3 function", and that
  therefore it "can pass when `From<RustStreamError> for PyErr` is incorrect".

  | #   | Claim                                                        | Verdict                                                                                        |
  | --- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
  | 1   | Calls the three private symbols directly                     | **CONFIRMED** — `:30, :96, :101, :109, :120`; all three are crate-private at `lib.rs:34,49,79` |
  | 2   | Never constructs a `PyErr` / calls exported PyO3             | **CONFIRMED** — the only `PyErr` hits are prose at `:12, :49`                                  |
  | 3   | A repository rule forbids this                               | **REFUTED** — no such rule exists                                                              |
  | 4   | The Rust suite should be classified as unit, not behavioural | **REFUTED** — the plan mandates this shape                                                     |

  The *observation* is accurate. The *rule* it invokes is not a repository
  rule, and the suggested resolution contradicts a design the repo states
  explicitly and the plan adopted deliberately:

  - `docs/developers-guide.md:4213-4221`: the integration crate builds with
    `pyo3/extension-module`, "so no `cargo test` binary can link an
    interpreter. Rust tests therefore assert typed values … and anything
    asserting a `PyErr` or a Python exception class cannot live in them at
    all; it belongs in the extension-required Python suite."
  - Plan decision log (`2026-09-19`): "Retain Python exception construction in
    the host interpreter. Pure Rust tests verify typed values; Python tests
    verify conversion through real entry points."
  - Plan V2: "Reuse production validators and conversions, not a test-only
    classifier", with behavioural scenarios bound by an internal module — and
    the module must be in-crate to reach the crate-private enum "without
    widening its visibility".

  So asking the Rust suite to construct a `PyErr` asks for something the build
  makes structurally impossible, and asking it to move to a public boundary
  asks it to abandon the crate-private access the plan required. The gap the
  row describes — nothing in the Rust layer can witness the `PyErr` conversion
  — is real and *is* closed, but in Python:
  `cuprum/unittests/test_rust_stream_native_order.py:236-275` pins the
  `InvalidDescriptor` → `ValueError` arm through real entry points, and is in
  `EXTENSION_TEST_TARGETS` (`Makefile:175`). The plan records that module
  failing under the `errors.rs` mutation and passing on revert.

  **One claim is partly true and worth stating precisely rather than disputing
  wholesale.** The row's explanation continues: "the shared `rust_streams`
  fixture returns the `_streams_rs` shim. Its zero-buffer validation runs in
  Python before the native call, so those behavioural rows do not exercise the
  changed Rust boundary." The fixture does return the shim
  (`conftest.py:180-197`), and for the **pump** zero-buffer row this is exactly
  right — the shim raises first (`_streams_rs.py:164-170`) and the native call
  never runs. But for the **consume** row it is false: `rust_consume_stream`
  has no Python guard (`_streams_rs.py:383-387`) and delegates straight to
  native, where `run_stream_operation` runs
  `validate_buffer_size(buffer_size)?` and reaches Python through
  `result.map_err(PyErr::from)`
  (`rust/cuprum-rust/src/stream_pyfunctions.rs:40-47`). The consume zero-buffer
  row therefore traverses **both** changed code paths. As a statement about
  "those rows" collectively the sentence is wrong; the plan's own decision log
  already records the distinction — the consume path "has no writer and so no
  pre-adoption check" — and the V3 mutation evidence independently confirms it,
  producing exactly **one** failing row in this module under a native-only
  mutation.

  The remaining sub-claim, that the zero-buffer cases should "use valid open
  descriptors", is already satisfied: both rows open `os.devnull`
  (`test_rust_streams_errors_behaviour.py:158, :167`) — a real descriptor, not
  a placeholder — and the file is registered in `EXTENSION_TEST_TARGETS`
  (`Makefile:180`), so it runs under `CUPRUM_REQUIRE_RUST_EXTENSION=1` rather
  than skipping silently. No code change is warranted; the disposition is a
  reasoned reply.

- [x] (2026-10-01) **CodeRabbit withdrew the failed row and adopted both
  corrections, so the disposition above is closed by agreement rather than left
  standing.** The reply at `16:26:56Z` (comment `5935754498`) was answered at
  `16:27:52Z` (comment `5935771012`) with *"I withdraw the recommendation. No
  code change is required for this finding."* The reply confirms each of the
  four verdicts independently — in-crate scenarios stay, the two validation
  paths stay distinct, the existing boundary coverage is retained — and states
  the reason plainly: *"My recommendation relied on an unsupported repository
  rule and misclassified a deliberate behavioural test design."*

  Two specifics are worth keeping, because they are the parts a later reviewer
  is most likely to re-raise:

  - The pump/consume asymmetry was not merely accepted but **restated as a
    correction of the bot's own claim**: *"The consume shim does not validate
    the buffer; its zero-buffer scenario reaches native validation and
    `PyErr::from`. My claim about all behavioural rows was incorrect."* That
    was the one sub-claim this plan recorded as *partly true*, so the reply
    shows the correction landed rather than being talked past.
  - Two learnings were persisted against the repository, which makes this a
    durable fix to the review's model rather than a one-off concession. One
    records the `pyo3/extension-module` constraint and forbids demanding
    `PyErr` construction, boundary relocation, or reclassification on this
    basis; the other records the pump/consume distinction and the
    `EXTENSION_TEST_TARGETS` registration of both modules.

  Scope limit: the reply says *"I did not rerun the tests"* — it verified the
  structure and the recorded mutation evidence, not the runtime behaviour. That
  is adequate for a finding whose subject is where the code lives, and the
  extension-gated evidence for the behaviour itself is recorded elsewhere in
  this plan.

  The **walkthrough commentary** is a separate surface and did **not** change:
  comment `5745365959` still carries the ❌ row, its `change_assessment_commit`
  still reads `005d358f`, and it now opens with a *Reviews paused* banner —
  *"It looks like this branch is under active development"* — which is
  CodeRabbit's auto-pause after successive commits, not a rate limit. A paused
  walkthrough is not a verdict on this head, so it is not evidence either way;
  it clears only when a review runs at or after the commit that fixed the
  issue. The inline-thread surface is clean: PR 432 has exactly **one** review
  thread, on `test_rust_streams_roundtrip_property.py`, and it is both
  `isResolved` and `isOutdated`.

- [x] (2026-10-01) **`typos.toml` is committed, so two figures derived from
  its absence are stale — and the file cannot be dropped.** `11039493` staged
  the regenerated `typos.toml` line alongside its plan text, so
  `git log 7f762870..HEAD -- typos.toml` now names that commit where it was
  empty. The committed diff is one line (`git diff --numstat` → `1 1`), and it
  is **`main`'s own content**: `574ddee7` (`main`) carries the identical
  rephrase — a typo-correction pair that swaps the misspelled identifier for
  the correct one — so this branch re-asserts a line it would have inherited
  anyway. (Both halves are quoted verbatim in `typos.toml`, whose ignore entry
  matches the whole phrase; quoting only the misspelled half here would defeat
  that entry and fail `make spelling`.)

  Three measurements fix the disposition:

  - Excluding the lockfile and this plan, `git diff --name-only 7f762870 HEAD`
    lists **19** files, not 18.
  - `git merge-tree --write-tree origin/main HEAD` shows `typos.toml` clean
    against `main` — the branch's copy replays empty — so the landing surface
    is **18**. CI builds the merge ref.
  - Reverting is not stable: checked out to its `7f762870` content and re-run,
    `make spelling` writes the rephrase back (the pinned `v0.1.2` builder
    regenerates it from the live shared dictionary), leaving a dirty tree the
    Stop hook blocks on.

  The line therefore stays, and the count is stated per frame: **19** on the
  branch, **18** at the merge ref. In the branch frame that is one over the
  plan's tolerance; in substance it is 18 changes, because the nineteenth is a
  byte `main` owns. Recorded as a deviation rather than absorbed silently. The
  dated entries above keep the figures true at their own heads.

- [x] (2026-10-01) **`0eb1e666`'s CI failure is the third occurrence of the
  same `coverage`-job timeout, on the commit that fixed the previous failure.**
  **Attempt 1** of run `36892038283` reports `completed/failure`, and a grep of
  every `FAILED` line in its failed-job log finds exactly **one** failing test —
  `test_doctest_warning_contract.py::test_pinned_doctest_route_rejects_a_warning`,
  `Failed: Timeout (>30.0s) from pytest-timeout`. Test start `16:49:48.532` →
  verdict `16:50:18.660` is **30.13 s**: the bound fired, no assertion failed.
  This is the same class the 2026-09-29 entry above measured, and it is
  recorded as a third occurrence rather than folded into a running count,
  because the ordinal is what drifts.

  The mechanism is re-pinned from the workflow rather than inherited. The
  `coverage` job installs **only** toolchain `1.85.0`; the sole installer of
  `nightly-2026-08-23` is `setup-dev-fast`
  (`.github/actions/setup-dev-fast/action.yml:11, :22`), used at `ci.yml:290`
  (`lint-test`) and `ci.yml:791` (`extension-tests`) and nowhere in `coverage`.
  `~/.rustup` is in no cache path — the job restores only `~/.cargo/bin`,
  `~/.local/bin`, `~/.cache/uv`, `~/.local/share/uv`, `.uv-cache` and
  `.uv-tools` — so the 595 MB nightly is a cold download inside the test's own
  `subprocess.run`, whose `capture_output=True` keeps it out of the log.

  The control is stronger than the one recorded above, because it holds *within
  this branch* on identical bytes. Run `36633831081` ran the same job at
  `97f51d41` and the same test **passed in 9.619 s** (`21:46:27.226` →
  `21:46:36.845`). `Makefile` is blob
  `afc5158c518e676234892c1504cc21d15b8b7445` at `97f51d41`, `184c5b65`,
  `11039493` **and** `0eb1e666`, and `git diff 97f51d41 0eb1e666` over every
  `TOOL_HASH` input (`uv.lock`, `pyproject.toml`, `Makefile`, the setup-sccache
  and install-makeutil actions) is **empty** — so the tool cache key's apparent
  difference between the runs is not a branch effect, and both runs restored
  from a prefix key rather than the computed one. The branch also touches
  neither the test file nor `pyproject.toml`. The `Makefile` diff on the branch
  is three `EXTENSION_TEST_TARGETS` additions and does not touch the doctest
  recipe.

  What actually differed is the **Cargo registry cache**: restored from
  `cargo-v1-…9b8de305…` in the green run, and
  `Cache not found for input keys: cargo-v1-…64238430…` at `0eb1e666`. The
  whole job ran cold — 661 s green against 1362 s failing, 2.1× (both are
  *step* spans: first step start → last step end; the enclosing *job* spans are
  669 s and 1372 s, and the ratio is 2.06 and 2.05 respectively, so the
  conclusion does not depend on which convention is read). That is the
  environmental variance the 9.6 s → 30.1 s spread sits inside, and it is the
  reason this is a rerun rather than a patch. `gh run rerun --failed` was
  dispatched (`attempt=2`), which is the remedy the project's own notes record.

  **That rerun did not produce a verdict, and no pass may be claimed from it.**
  It reached `in_progress` with 15 of 16 jobs green and `coverage` still
  running, then went `completed/cancelled` at `17:16Z` — not because it failed,
  but because the concurrency group cancels a run once a newer commit arrives
  on the branch. The commit that superseded it is `10abccad`, whose push
  created run `36897918582`. So the flake's disposition is unchanged and its
  *confirmation* is deferred to that run: the cancellation is a scheduling
  artefact of fixing the two documentation gates, not new evidence either way.
  A cancelled attempt is the one outcome that cannot be read as a signal, which
  is why the distinction is recorded rather than the attempt simply dropped.

- [x] (2026-10-01) **The deferred confirmation arrived: run `36897918582` is
  green, all 17 jobs and all 28 reported checks, and the flake is therefore
  environmental on the same reasoning rather than on a rerun's luck.** The run
  is `completed/success` at head `10abccad`, and its coverage job completed
  `success` in 663 s — within 6 s of the 669 s green control at `97f51d41`, and
  against the 1372 s the same job took when it timed out. That is the
  prediction the entry above deferred to, now discharged by the run it named
  and by no other.

  The PR's check rollup is `28` entries: **24 `SUCCESS`, 4 `SKIPPED`, 0
  anything else**. The four skips are `automerge`, `extended`,
  `Kody Code Review` and `Sourcery review` — all repository-configured to skip
  on this PR, not failures wearing a neutral label. `mergeStateStatus` reads
  `CLEAN`.

  Derived from that run rather than from the prose above, the coverage job's
  two spans are **663 s** (job) and **653 s** (step), which is a useful
  cross-check on the previous entry: it records 661 s / 1362 s, and those are
  *step* spans, not job spans. Both conventions are legitimate and both
  reproduce exactly — the step span is first-step-start → last-step-end, the
  job span is the job's own `started_at` → `completed_at`, an ~8–10 s
  difference from runner setup and teardown. The entry above now names which
  convention it used, because the unnamed gap is exactly what reads as drift
  later; this was checked as a possible defect first, and the arithmetic (2.06
  and 2.05) shows the conclusion is unaffected either way.

  One claim in that entry **was** stale and is corrected above: it says run
  `36892038283` "reports `completed/failure`". At run level it now reports
  `completed/cancelled`, because the run-level rollup reflects the *latest*
  attempt and attempt 2 was cancelled. The failure is real and intact — it is
  **attempt 1** that carries `completed/failure`, with the coverage job at
  `2026-10-01T16:34:58Z` → `16:57:50Z` — so the correction is a scope
  qualification, not a retraction. The mechanism is worth recording: a
  `gh run view` of a re-run run answers about the newest attempt, so a bare
  run-level `conclusion` silently re-scopes as soon as a rerun is dispatched.

  The supersession chain is now pinned to the second rather than inferred,
  since the earlier entry asserted the causal order: `10abccad` was committed
  `19:14:01+02:00` (= `17:14:01Z`), run `36897918582` was created `17:14:30Z`,
  and the superseded attempt's coverage job went `cancelled` at `17:15:26Z`.
  Commit → new run → cancellation, in that order, 85 s end to end. The
  cancellation is therefore confirmed as a scheduling artefact of the fix
  commits, exactly as claimed, and not as a signal about the flake.

  Documentation gates were re-run at the local head `2fe6c2c8` (the
  execplan-only commit that follows the pushed head) and are green:
  `make check-fmt` `EXIT=0` including the `mdtablefix` and
  `ruff format --check` leaves, `make markdownlint` `0 error(s)` across 78
  files with its `make spelling` leaf passing, and `make nixie` reporting "All
  diagrams validated successfully!". `2fe6c2c8` touches only this file — 11
  insertions, 1 file — so the green run at `10abccad` remains the authoritative
  code-gate evidence for the pushed head, and no code byte differs between
  them. This paragraph is itself an edit to this file, so those verdicts pin
  `2fe6c2c8` and not the commit that carries the text being read — the same
  regress the 2026-09-28 entry above records, and the reason the pushed head's
  CI run is carried as the independent confirmation rather than this file's own
  say-so.

  **What remains is not a gate.** It is review `5354955377`
  (`CHANGES_REQUESTED`, `2026-09-29T15:42:06Z`, assessed against `e7d4fa45`),
  which holds `reviewDecision` at `CHANGES_REQUESTED` even though
  `mergeStateStatus` is `CLEAN`. Its sole finding was the `_safe_close`
  duplication fixed by `b3ae9f20`, and its inline thread is
  `resolved=true, outdated=true`. Nothing in the tree can clear it — only a
  fresh CodeRabbit review against a current commit can, because the decision is
  the bot's to withdraw. That request is enqueued separately rather than
  claimed here.

  **It is enqueued, and the ETA is the thing a reader needs to know.**
  `comenq put leynos/cuprum 432 …` returned identifier `f33ee725` with an
  estimated post time of **~23 h 09 m**, because the shared queue is **63**
  entries deep — not rate-limited, just busy, and the delay is the queue's. So
  "awaiting review" here means a real, tracked, ~1-day wait, not a request that
  silently failed. A future reader who finds the `CHANGES_REQUESTED` still
  standing should read that queue depth before concluding the request was lost.

  **A correction to the paragraph above, because its stated reason was wrong
  even though its conclusion survived.** That paragraph said `main` is "not
  branch-protected" because
  `gh api repos/leynos/cuprum/branches/main/protection` returns 404. The 404 is
  real but it only means the *legacy* branch-protection API has nothing
  configured. This repository protects `main` through a **ruleset** —
  `main-required-checks`, id `18427980`, `target=branch`,
  `enforcement=active` — visible via `gh api repos/leynos/cuprum/rulesets` and
  `gh api repos/leynos/cuprum/rules/branches/main`, a separate mechanism the
  legacy endpoint does not report. So the premise was false and the reasoning
  that rested on it was invalid.

  The conclusion nonetheless holds, for a different and now-measured reason:
  the ruleset's rules are exactly `required_status_checks` and `deletion`. It
  requires **12 status checks** — `lint-test`, three `Typecheck and test` legs,
  `coverage`, `benchmark-ratchet`, five `build-native-wheels` legs and
  `verify-wheel-install`, plus `Extension-gated tests (Python/Rust boundary)` —
  and carries **no `pull_request` review rule**. So a `CHANGES_REQUESTED`
  review genuinely does not gate the merge here, which is what the earlier
  paragraph claimed; it was right by accident and wrong in its evidence. This
  is worth recording precisely because the two mechanisms disagree: a reader
  who checks only the legacy API will conclude the branch is unprotected and be
  misled about *why* the merge button is or is not live.

  `mergeStateStatus` was also observed as **`CLEAN`** (at `10abccad`) and then
  as **`BLOCKED`** (at `6871ba35`), and the difference is not the review: at
  the later head the 12 required checks were still `in_progress`, so the merge
  was blocked by pending required checks and nothing else. `mergeable` stayed
  `MERGEABLE` throughout. An earlier reading of `BLOCKED` as review-driven
  would have been a misattribution, and the check-run inventory
  (`gh api repos/leynos/cuprum/commits/<sha>/check-runs`) is what distinguishes
  the two causes.

  The identifier is `67f60c91`, the third enqueued, and the two supersessions
  are the reusable lesson. `de71c44b` named head `89336b01` in its body and was
  invalidated when a later commit landed; `f33ee725` named `3de1c64b` and was
  invalidated the same way 20 minutes later. Both were deleted with
  `comenq del` and re-queued, because a queued comment is a *deferred* artefact
  with a ~1-day lag, so any head written into its body is ~23 hours stale by
  the time it posts. The fix is not to chase the head — that is a treadmill,
  since recording each correction moves the head again — but to write a body
  that names no SHA and states an invariant instead.

  **Naming no SHA is necessary but not sufficient, and `67f60c91` is the
  proof.** Its body claimed that "every commit after it is execplan-only …,
  touching no code, test, build-config or manifest byte". That claim was false,
  and false when it was written rather than decayed since. The entry was
  enqueued at `17:45:56Z` on 2026-10-01, when the head was `7f33b0be`, and
  `git diff --name-only b3ae9f20 7f33b0be` already returned
  `cuprum/unittests/test_rust_stream_native_order.py`,
  `docs/developers-guide.md`, `typos.toml` and the execplan itself. The words
  "execplan-only" carried an invariant the tree never satisfied. A SHA-free
  body does not decay because of the clock; it decays because of the tree, and
  this one was stale on arrival.

  The defect was not the deferral, so the remedy was not to re-time the
  enqueue. It was to state the invariant at a scope that is actually true. The
  claim that survives every check is the narrower one — *no production code has
  changed since the fix*:

  - `git diff --name-only b3ae9f20 HEAD -- cuprum rust ':!cuprum/unittests'`
    returns nothing, so no file outside `cuprum/unittests/` changed under either
    source tree.
  - `git diff --stat b3ae9f20 HEAD -- cuprum/unittests/test_rust_streams_roundtrip_property.py`
    is empty, so the file the finding named is untouched.
  - `git grep -c 'def _safe_close' -- .` reports exactly one definition in the
    package, `cuprum/unittests/_rust_stream_test_support.py`.
  - `git diff --numstat b3ae9f20 HEAD -- '*.py'` reports `49 0`, so the single
    Python edit since the fix is additive and no reviewed line was removed or
    rewritten.

  `67f60c91` was deleted and replaced by `11645ed5`, whose body states that
  narrower invariant *and* records the correction, so the supersession is
  visible to a reader of the posted comment and not only to a reader of this
  file. The reusable rule: an invariant is still a claim about the tree, so it
  must be verified against the tree at the head being left, and stated at the
  narrowest scope that is true — "execplan-only" asserts far more than "the
  reviewed code is unchanged", and is correspondingly easier to falsify.

  That rule is easy to trip even whilst documenting it. The first draft of this
  very passage addressed the reader in the second person, reintroducing the
  construction the `:1224` finding had removed. An independent sweep of the
  file for first- and second-person pronouns caught it; re-reading only the
  changed lines had not, which is the argument for sweeping the whole file
  after any prose edit rather than inspecting the hunks that were touched.
  Every remaining pronoun hit in this file sits inside quoted third-party
  speech or is the letter in "I/O".

  The gating record is now closed as far as the repository can close it. What
  the branch's own artefacts establish, each independently of this file's prose:
  `mergeable` is `MERGEABLE`; `main` is protected by ruleset
  `main-required-checks`, whose rules are `required_status_checks` and
  `deletion` only and which contains **no review rule**, so `CHANGES_REQUESTED`
  is a bot-held signal rather than a technical gate; and the review's sole
  finding is genuinely repaired — `b3ae9f20` deleted the local copy and
  `test_rust_streams_roundtrip_property.py:27` now imports the shared
  `_safe_close` from `cuprum.unittests._rust_stream_test_support`, where it is
  defined at `:29`. The absence of a review rule is the load-bearing fact: it
  is the difference between "cannot merge" and "will not merge without an
  updated review", and only the second is true. `mergeStateStatus` is
  deliberately not listed, because it is time-varying — it read `CLEAN` at
  `10abccad` and `BLOCKED` at `6871ba35` purely because the required checks
  were still running at the later head. Quoting it as a settled property is
  exactly the error the paragraphs above record.

  Two CI runs for `89336b01` were observed in flight and neither was a verdict:
  run `36901334814` (`CI`, 14 jobs, `in_progress`, 0 failing) and `36901334242`
  (`Rust boundary verification`, 5 jobs, `in_progress`, 0 failing, having
  advanced from `queued`). They are named so that a later reader can tell which
  runs a push created rather than having to reconstruct it, and they were
  explicitly **not** claimed as passing.

  The ETA commit `3de1c64b` then landed on top of them, and
  `.github/workflows/ci.yml:32-34` sets `cancel-in-progress` to true for
  `pull_request`, so both are expected to be cancelled rather than completed.
  That prediction is recorded as a prediction: **at the time of writing both
  were still `in_progress`**, and a first draft of this paragraph wrongly
  stated the cancellation as accomplished fact. It was not — the runs were
  mid-flight, and the push had in fact created two fresh ones, `36901601851`
  (`CI`) and `36901601193` (`Rust boundary verification`), both `pending`. The
  error is recorded rather than quietly fixed because it is the same class this
  plan keeps catching: reading an expected transition as an observed one.

  So the honest end state: every green CI verdict in this plan belongs to
  `10abccad` or earlier, and **no completed CI verdict exists for the final head
  `3de1c64b`**. The code-gate evidence that does hold is the `10abccad`-era CI
  run plus the local documentation gates at each later head. The commits after
  `10abccad` are execplan-only, which is what makes that substitution sound
  rather than convenient — and the claim is checkable with
  `git diff --stat 10abccad 3de1c64b`, which is the check a sceptical reader
  should run rather than trusting this sentence.

- [x] M2: documentation reconciled, platform evidence complete, 6.1.1 marked
      done.

- [x] (2026-10-01) Diagnosed and cleared a merge conflict that silently
  suppressed CI for the branch's last two commits. `6a17857c` had **no**
  `pull_request` runs at all — only the third-party review apps and a skipped
  `dependabot-automerge` (a `pull_request_target` workflow, which runs against
  the base ref and so is unaffected). The cause was not a trigger misfire: the
  branch genuinely conflicted with `main` in `typos.local.toml`, because both
  sides appended to the same `[patterns] ignore` anchor, `'\btools/mold/',`.

  The mechanism chain is worth recording, because the symptom names nothing. A
  conflicting pull request has no buildable synthetic merge commit, so GitHub
  creates no `refs/pull/432/merge` to run against and **no `pull_request`
  workflow starts**. The stale ref was direct evidence: `refs/pull/432/merge`
  still pointed at `f7e8e13a`, whose second parent was `6871ba35` — it had
  never been rebuilt for `6a17857c`. A controlled comparison in
  `git merge-tree` confirmed the causal step exactly: `6871ba35` exits 0 with
  zero conflicts and had CI runs; `6a17857c` exits 1 with one conflict and had
  none. The two heads differ only by three files, one of which is that
  `typos.local.toml` hunk.

  The fix needed no rebase. `main` had independently added
  `'[0-9][0-9a-f]{6,39}'`, a **general** rule for exactly the collision the
  narrow `'6871ba35'` entry was written for: typos tokenizes before matching, so
  `6871ba35` splits into letter runs, and the hex pair inside it is then read
  as a misspelling of "be" or "by". Main's comment records the deliberately
  general treatment of that collision on the grounds that it recurs. Taking
  main's `typos.local.toml` verbatim therefore both supersedes the narrow entry
  and removes the conflict — one change, not two. `make spelling` regenerated
  `typos.toml` (exit 0), and it is now byte-identical to main's.

  Committed as `e2846439`. The prediction was then confirmed by observation
  rather than assumed: `mergeable` went `CONFLICTING` → `MERGEABLE`,
  `refs/pull/432/merge` was rebuilt to `8f10d35d` (parents `7b86b904`,
  `e2846439`), and CI run `36904261529` started. No rebase was needed because
  the merge settings are squash-only (`allow_merge_commit` and
  `allow_rebase_merge` both false) and the ruleset sets
  `strict_required_status_checks_policy: false`, so the branch is not required
  to be up to date with `main`. Rebase-on-push would have been a fabricated
  requirement, and was avoided.

  Scope held throughout: `git diff --name-only 10abccad e2846439` lists no code
  file at all, only `.md` and typos config, so the CI verdict at `10abccad`
  still covers this branch's code. That check is the reason no further local
  gate run was needed for this change.

## Surprises & discoveries

**Three of the five behavioural scenarios never ran, and the only signal was a
coverage percentage.** `stream_error_behaviour.rs` carried two `#[scenario]`
bindings for the five scenarios `stream_errors.feature` declared. A binding
that omits both `name` and `index` silently takes the *first* scenario in the
file (`rstest-bdd-macros-0.5.0`, selector semantics in its `scenario` docs), so
the unnamed binding ran "Reject an invalid buffer before stream preparation"
and the second binding ran the outline. "Reject a buffer above the cap",
"Retain a native I/O failure" and "Retain a semantic stream failure" were
declared, parsed, never bound, and never executed. The suite was green; the
feature file read as complete coverage.

The result was reachable only from the `coverage` CI job, which is a *required*
check and which had been `cancelled` by the concurrency group on three earlier
pushes, so this was its first observation on the branch. It failed:

```text
Coverage decreased
Current coverage: 86.05%
Baseline coverage: 87.35%
Tolerance: +/-1.00 percentage points
```

The failing pair was identified by arithmetic rather than inference. The
ratchet step runs `ratchet_coverage.py` twice against two independent
baselines, Rust then Python, and the failure message names neither pair; the
reported `86.05` matches the Rust half exactly (975/1133 = 86.0547%, while the
Python half was 89.65 and passed). The per-file decomposition then names the
cause: `stream_error_behaviour.rs` contributes +21 uncovered lines (absent →
20/41). `cargo nextest list` confirms the mechanism — only
`typed_native_stream_failures` and `accepts_a_valid_buffer_size::case_{1,2,3}`
were registered.

Measuring the baseline took a second pass and one correction. Local
`cargo llvm-cov` reaches a *superset* of production lines, so its file line
totals are larger and its percentages differ from CI's; the two are not
comparable, and an earlier reading of the baseline as "87.53%, an exact match
to both printed numbers" was wrong — 87.53% is the local toolchain's figure. CI
instruments with **rustc 1.85.0**, and only at 1.85.0 does the baseline resolve
to 946/1083 = 87.35% exactly. Both printed numbers are then reproduced against
CI's own report: 86.05% and 87.35%.

The consequence was not the 1.48 pp dip but that **R2's OS-code-retention
behaviour was unverified**. `observe()` maps `PumpError::Io` through
`inner.raw_os_error()`, and *no bound scenario ever reached that arm*, so
nothing in the suite would have failed had the code been dropped. Fixed by
binding every scenario by name and adding a scenario for the descriptor path,
whose steps had been unreachable since they were written. Verified to have
teeth: setting `observe()`'s code to `None` fails `retains_a_native_io_failure`
with "a stream failure carrying an OS code was expected, found Stream { code:
None, … }".

The fix binds all five scenarios by name and adds a scenario for the descriptor
path, whose steps had been unreachable since they were written. Re-measured
with CI's own toolchain
(`cargo +1.85.0 llvm-cov … --all-targets --all-features --cobertura`), the same
`.rs` scope moves 975/1133 = 86.05% → 997/1134 = **87.92%**, above the 87.35%
baseline rather than merely inside tolerance, with `stream_error_behaviour.rs`
at 60/60 (the earlier 41-line figure was itself a newer-toolchain measurement).
That measurement is bound to the pre-rebase head. CI re-measured the same scope
at **88.11%** after the rebase onto `main`, which added `cuprum-streams`
coverage — see the closing Progress entry. The direction of the cause is worth
stating: `997/1134` was a *correct measurement of a fixed revision*, not an
error, and it went stale because the workspace it measured changed underneath
it, not because the arithmetic was wrong.

One change was made and then reverted, and the reversal is the more useful
record. The scenario's error code was moved from 9 to 8 on the stated grounds
that 9 reads as a negative errno on Windows — a claim that is not in the code:
`errors.rs` selects by `cfg(unix)`/`cfg(windows)`, not by sign, and it tests
with 9 throughout. The plan's own behavioural spec says only "a platform error
code", so 9 was free and the edit was gratuitous. Both the edit and its
justification are withdrawn; the feature diff is additive only.

Three lessons. A `#[scenario]` that binds the wrong scenario is
indistinguishable from a passing test — the macro has no diagnostic for "a
feature scenario no binding selects", so nothing but line coverage reports the
gap. A *required* check that the concurrency group keeps cancelling is not
being observed; three green-looking pushes had simply never run it. And a
coverage percentage is only comparable to CI's own number when it comes from
CI's own toolchain: the same commit measures differently under a newer rustc,
which nearly produced a false diagnosis of which file the ratchet was reading.

A fourth, smaller one: the first version of the fix passed `cargo nextest` but
failed `make check-fmt`. `rust/.rustfmt.toml` sets `fn_single_line = true`, so
four one-statement scenario bodies rustfmt wanted as `fn f(..) { body }` were
written across three lines. Running the focused test target is not a substitute
for the formatting gate, and the formatter here is the pinned nightly
(`nightly-2026-05-28`), not the default toolchain.

**A behavioural row passed on Windows for the wrong reason, and its `PASSED`
line was the only thing that looked like evidence.** The `a fatal reader error`
rows handed the native entry point a **closed** pipe read end. On Windows a
closed descriptor is refused by the *wrapper*, not by Rust: the wrapper
resolves the reader through `msvcrt.get_osfhandle` while preparing the call,
and that raises `OSError(EBADF)` before the native read path is reached. The
rows therefore asserted `OSError` — which the wrapper's own refusal already
satisfied — and never exercised the native failure they exist to pin. The trap
is sharper than the `#[scenario]` one above, because there the tests did not
run at all; here they ran, passed, and printed a green line while covering
nothing.

What makes it discoverable is a *contrast*, not the row itself. In the RED run
at `bcf72f52` (`/tmp/611-win-ci-RED.log`) the two `a fatal reader error` rows
read `PASSED` on Windows in the same session where the sibling
`consume-a zero buffer size` row read `FAILED` with
`AssertionError: expected
ValueError, found OSError: [Errno 9] Bad file descriptor` —
the same `get_osfhandle` refusal. One row's failure is what identifies the
other's pass as vacuous; neither row's own status is wrong on its face. The
lesson generalizes: when a platform's wrapper can raise the *same exception
class* the native path would, an assertion on that class cannot tell the two
apart, and the fix is to make the setup reach past the wrapper — as the
`zero buffer size` rows already did — rather than to add another assertion.

The remedy is an **open but unreadable** descriptor: a file opened
`O_WRONLY | O_CREAT`, which every platform lets the wrapper prepare and every
platform fails on the first read. `cuprum/unittests/test_rust_errno_windows.py`
already used exactly this device, so the correct idiom was in the tree and only
the behavioural module had missed it.

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
Hypothesis boundary coverage; its accepted-argument half now lives in
`cuprum/unittests/test_rust_streams_roundtrip_property.py` after the split.
Reuse both instead of creating a second generator suite. The boundary module's
Windows exclusions reflect shim preparation order and must not be removed on
the assumption that native and shim ordering are identical.

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

The mechanism was measured at `9096c804` rather than inferred.
`actionlint 1.7.12` hangs in `RuleShellcheck.VisitWorkflowPost` →
`externalCommand.wait` while its writer goroutine is parked in `os.File.Write`
of a 9305-byte body — a `write(2)` that never returns because the reader is not
draining. A SIGQUIT goroutine dump of a reproducing run, taken under this
session's own process, shows exactly that stack pair and **no child process at
all**: `pgrep` finds no `shellcheck` alongside the wedged `actionlint`, and the
only `wchan` is `futex_wait_queue` on the Go scheduler. So it is not "a slow
shellcheck" and not a large-workflow cost — the classifier's child is spawned,
written to, and the handshake never completes. `ci.yml` is the workload that
triggers it: run at file scope, the other nine workflow files return in 0 s
every time while `ci.yml` is the one that wedges.

Seventy probe runs of the same command over the same bytes, all at `9096c804`,
settle the rate. With `shellcheck 0.10.0` on `PATH` it hung **11 times** — and
in five separate batches the count varied from 6-of-6 to 0-of-10, so the
outcome is not a function of the input. Host contention is not what tips it:
ten runs under eight added CPU spinners and eight more at a load average of 2.7
produced zero hangs. The contrast is the solid result — with the shellcheck
integration disabled, **7 of 7** runs returned exit 0 in under a second, having
never spawned the child that wedges. The word this record needs is the one
`Surprises` keeps reaching for: the step is *not reproducible on demand*, which
is worse for a gate than a reliable failure, because a step that can hang or
succeed on identical input cannot be made to pass by re-running it — only by
bounding it.

**`mapsplice` cannot parse this repository's roadmap, and the failure is silent
until it is tried.** Every command fails, including one that would change
nothing, with a message that names the symptom and not the site:

```text
error="task list appeared without a current step" error_class="invalid_roadmap"
```

The site is phase `## 9.`, which has numbered tasks `9.1.1.`–`9.1.6.` sitting
directly under the phase heading with no `### 9.1.` step heading between them.
The grammar requires a step to own a task list, and the installer of that phase
never added one, so the whole file became uneditable by the tool. This is on
`main` and predates the branch. The diagnosis was confirmed by construction
rather than by reading: adding the missing heading to a scratch copy turns the
same failing command green. Anyone needing `mapsplice` on this repository must
add that heading first; anyone ticking a single checkbox does not need
`mapsplice` at all, since renumbering — its actual value — is not involved. See
the Decision log for why this task hand-edited instead of repairing it.

The related trap is that a tool which fails closed on a whole file gives no
partial signal. `mapsplice` is not "broken for phase 9"; it is inert for the
entire roadmap, so a later phase-6 or phase-8 edit would have failed the same
way, and would have looked like a problem with *that* edit.

**A pinned toolchain name in a test is a hidden network dependency, and the
failure it produces looks exactly like a flake.** The `coverage` job failed
twice at `1714ac0d` on
`test_doctest_warning_contract.py::test_pinned_doctest_route_rejects_a_warning`,
with `Failed: Timeout (>30.0s) from pytest-timeout` and no other diagnostic.
The test shells out to Cargo with `RUSTUP_TOOLCHAIN=nightly-2026-08-23`
hard-coded, and `coverage` is the one lane that never runs `setup-dev-fast` —
that action's `rustup toolchain install "${toolchain}" --profile minimal` is
what puts the nightly on disk, and it appears only at `ci.yml:290` and
`ci.yml:791`, in `lint-test` and `extension-tests`. rustup does not fail when
the named toolchain is missing; it silently **downloads** it, and the download
lands inside the test's own `subprocess.run`, where `capture_output=True` hides
it from the log entirely. Measured with `RUSTUP_HOME` pointed at an empty
directory, the same test goes from **0.49 s warm to 11.5–12.7 s cold** — a
23–26× differential, entirely network, on an idle 6-core host, for a 595 MB
payload. A reader seeing only the failure gets no clue that a toolchain
download is involved: the test's own output is captured, and the surrounding
lines in the log are ordinary `PASSED` entries. The general lesson is that a
timeout in a test whose body shells out to a pinned tool is a *provisioning*
symptom first and a performance symptom second, and the cheap discriminator is
to point the tool manager's home at an empty directory and re-time it.

The second surprise is that a fast lane can be green for a reason that makes
the slow lane's failure look inconsistent when it is not. The passing coverage
run took 7.4 s for this test against a 0.48 s local time — fifteen times
slower, already a third of the way to the bound — which is a warm-but-loaded
runner, not a cold download. The gap between 7.4 s and 30 s is small enough
that it is crossed by ordinary runner variance rather than by a code change,
which is precisely why the failure appeared and disappeared without the branch
changing anything between the runs that disagreed.

## Decision log

- (2026-09-29) **The `coverage` job's doctest-contract timeout is a `main`
  defect and is deliberately not patched here.** The branch was then at its
  18-file tolerance (re-measured 2026-10-01 as 19 on the branch and 18 at the
  merge ref; see Progress) and the failing test is byte-identical to base and
  outside the change surface, so a fix would (a) exceed the tolerance, (b)
  enlarge the branch into an unrelated CI concern, and (c) require editing
  `.github/workflows/ci.yml`, which this branch has deliberately never touched
  and whose every byte `actionlint` reads is `main`'s. Two fixes are available
  and both belong to a separate change: provision `nightly-2026-08-23` in the
  coverage job, or raise this single test's timeout above the global 30 s. The
  first is better — it removes the download rather than accommodating it —
  because the same latent cost is paid by any lane that runs the test without
  the prerequisite action. Recorded in Progress with the measurements, and left
  for `main`.

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

- (2026-09-27) **A skip written for one entry point was silently load-bearing
  for its sibling, and Windows found the gap.** The two Windows failures share
  one cause, and it is a bookkeeping error rather than a boundary error. Both
  failing nodeids pass `-1` as the reader together with `buffer_size=0` and
  expect the buffer rejection. On POSIX that holds even through the shim: the
  shim validates `buffer_size` before preparing the reader, and
  `_prepare_native_reader` passes a non-negative `-1` through
  `_convert_fd_for_platform` unchanged, so the buffer error wins. On Windows
  the same call reaches `msvcrt.get_osfhandle(-1)`, which raises `OSError(9)`
  before the buffer check ever runs. `test_rejects_out_of_range_buffer` had
  already been marked `_buffer_validation_before_descriptor` for exactly this
  reason — its comment states the mechanism — but the mark was applied to the
  *push* property only. Neither the consume property
  (`test_out_of_i64_buffer_size_stays_an_extraction_error`'s consume row) nor
  the consume row of the behaviour outline carried it. So the guarantee was
  written down for one entry point and assumed for the other, and the two
  consume rows were the only ones that could fail. The lesson is narrow and
  checkable: **a platform exclusion derived from an entry point's argument
  shape must be re-derived for every sibling entry point, because the shape —
  not the helper's name — is what determines whether the exclusion applies.**
  The fix observes the buffer window on both platforms through an open reader
  instead of widening the skip, so the Windows job now has a live test for it
  rather than one more exclusion.
- (2026-09-27) **First repair of the above was itself incomplete, and the
  failure list said so.** The first pass added an open-reader window property
  and fixed the behaviour outline's consume row, but left
  `test_out_of_i64_buffer_size_stays_an_extraction_error` — the *other* Windows
  failure — on the `-1` throwaway, and left the now-redundant throwaway rows in
  place beside their open-reader twins. The defect in the repair was the same
  shape as the defect it was repairing: fixing one entry point and assuming the
  sibling. What caught it was reading the recorded nodeids in Progress against
  the change surface rather than against the description of the change: the two
  failing ids were known, and only one of them had been retargeted. **Check a
  fix against the verbatim failure identifiers, not against the narrative of
  what the fix does** — a repair described accurately but incompletely still
  reads as a repair.
- (2026-09-27) **The Rust property oracle called `size == 0` accepted, and only
  the generator's domain hid it.** `validation_matches_the_size_window` computed
  `accepted` as
  `u64::try_from(size).is_ok_and(|magnitude| magnitude <= 1 << 30)` — an upper
  bound with no lower bound. `u64::try_from(0)` succeeds and `0 <= 1 << 30`, so
  the oracle said "accepted" while `validate_buffer_size(0)` returns `Err`,
  because `BufferSize::new` delegates to `checked_buffer_size`, whose first arm
  is `if buffer_size <= 0`. The property passed only because `any::<i64>()`
  draws through proptest's `supported_int_any`, so zero is a 2^-64 event per
  draw. This is the failure mode property tests are supposed to make impossible
  — a generator whose domain happens to exclude the defect — and it survived a
  green `make lint`, a green Rust suite and a CodeRabbit pass. Corrected to
  `magnitude > 0 && magnitude <= 1 << 30`, with the inclusive cap kept
  (`< 1 << 30` would have regressed the boundary case the plan pins). Verified
  by `cargo nextest run --package cuprum-rust`: **51 passed, 0 skipped**. The
  general point: an oracle that restates a validator's window must restate
  *every* arm of it, and a property that never fails is evidence about the
  generator before it is evidence about the code.
- (2026-09-27) **`make test` stopped at its first failing prerequisite, so the
  half that would have caught the Rust defect never ran.** `make test` is
  `test-python test-rust` as separate recipe lines, and `test-python` aborted
  on the host `BASH_ENV` trap (13 release tests, environmental, fixed by
  `ccd9d1df` on PR #466, which is not an ancestor of this branch). The abort
  meant `test-rust` never executed: `stream_error_tests` appears zero times in
  that run's log. The corrected property oracle had therefore been edited,
  reviewed, compiled by `make lint`'s `cargo doc`/`clippy`, and never
  *executed* by any gate in that session. This is the same failure mode the
  `actionlint` entry above describes — **a gate that stops early proves nothing
  about the sub-checks after it** — recurring one target lower. The remedy is
  the recorded one: re-run with `env -u BASH_ENV`, which restores the whole
  target and prints `154 tests run: 154 passed, 0 skipped`. Report passed,
  failed, and *unobserved* separately; an unobserved check is neither, and it
  is the one that propagates silently.
- (2026-09-27) **`CHANGELOG.md` is deliberately not touched by this branch, and
  the precedent is a real one rather than an omission.** Nothing in the repo
  enforces a changelog entry (`grep -i changelog` finds no CI workflow and no
  Makefile target naming it), so this is a judgement call, and the comparable
  landed work is the evidence for it. `5.1.1` (`b84a30b5`) did add entries,
  because it changed a *default* users can observe — the pure-Python read size
  — so the entry answered the user's question "why did my throughput change".
  `6.1.1` changes no default, no public signature, and no documented input. Its
  user-visible surface is a restatement of the contract the branch tested
  rather than a new contract: `rust_pump_stream`'s docstring in
  `cuprum/_streams_rs.py` (the `buffer_size` parameter section, and the
  `Raises` section that names `ValueError` for "not positive or exceeds 1 GiB")
  already documents rejection above `1 << 30`, and the wrapper validates
  `buffer_size` before the native call, so a refusal of `0` or `-1` is a shape
  the code already had. The one genuinely new observable — that an out-of-`i64`
  size raises `OverflowError`, not `ValueError` — is written into the users'
  guide instead, where a caller checking the exception contract will look.
  Revisit at release time if `0.2.0-beta1` ships without 6.1.2: a release note
  is the right home for "errors are now classified at one boundary point",
  which is an internal statement with no user-visible consequence today.
- (2026-09-27) **The roadmap tick is a one-line hand edit, not a `mapsplice`
  run — a deviation from M2's stated method, approved before it was taken.** M2
  says to "use the `mapsplice` skill to mark exactly 6.1.1 `[x]`". That is not
  possible on this roadmap: `mapsplice` cannot parse it at all. Any command
  fails, including a probe that changes nothing:

  ```text
  error="task list appeared without a current step" error_class="invalid_roadmap"
  ```

  The cause is pre-existing on `main`, not branch-induced: `docs/roadmap.md`
  here is byte-identical to `origin/main`, and phase `## 9.` carries numbered
  tasks `9.1.1.`–`9.1.6.` directly under the phase heading with no `### 9.1.`
  step heading above them. The defect arrived with `b63a0f21` ("Add an idle
  heartbeat for quiet children (#359) (#398)"). It was proven rather than
  inferred: injecting a synthetic `### 9.1.` heading into a scratch copy makes
  the same command parse and succeed (`EXIT=0`). Phase `0.2.0` is the roadmap's
  other heading-less task list, but its bullets are unnumbered release notes
  rather than addressed tasks, so it is not evidence of a second instance of
  this defect.

  A second, independent reason not to use `mapsplice` here: even with parsing
  unblocked, its output rewrote unrelated prose. On a scratch copy it escaped
  parentheses in three unrelated phase headings (`## 6. … (issues …)` →
  `## 6. … \(issues …\)`, and likewise 7 and 9), re-indented task continuation
  lines from two spaces to four, and inserted blank lines between each task and
  its sub-bullets — six hunks across phases 6, 7 and 9 for a single checkbox.
  That is the churn the skill's own "Known caveat" warns about.

  The tick is therefore a hand edit changing `- [ ]` to `- [x]` on the 6.1.1
  item: one line, `1 insertion(+), 1 deletion(-)`. Nothing about this edit needs
  `mapsplice`'s actual value-add, which is renumbering and `Requires`
  reference rewriting; ticking a checkbox renumbers nothing, and 6.1.1 is only
  ever a *referenced* anchor (`Requires 6.1.1` in 6.1.2 and three later items),
  never a reference site. 6.1.2 and every later item are left open, verified by
  grep. The parser defect itself is **not** repaired here: adding a `### 9.1.`
  heading to an unrelated completed phase would be scope creep in a PR about
  error classification, and it belongs in its own change against `main`. It is
  recorded under Surprises so the next user of `mapsplice` does not have to
  rediscover it.

## Outcomes & retrospective

Both milestones are complete as of 2026-09-27. The one focused boundary change
landed as intended: a crate-private `RustStreamError`, typed validators, and a
single `From<RustStreamError> for PyErr` conversion point reached once through
`run_stream_operation`'s `map_err(PyErr::from)`. The roadmap checkbox is ticked
at `7dba35cf`, and moved to its final state in `11c6cb7f`, whose own CI run is
fully green including the required `coverage` check. The clause that sentence
replaces — "the roadmap checkbox is *not* yet ticked" — was true when written
and is left in the record here rather than silently deleted, because it is the
reason 6.1.1's Success criterion was checked against the code before the tick
rather than assumed from the plan: `grep` shows no PyErr construction in the
validators or the stream call sites, and both halves of the criterion are
asserted against the compiled extension (`ValueError` for `buffer_size`;
`OSError` carrying `errno`/`winerror` for I/O).

What the evidence covers, stated at the strength it actually has:

- Error categories and centralization (R1) are observed end-to-end on Linux.
  The compiled extension is required by `make test-extension` (101 passed, 1
  skipped) rather than skipped, so the `ValueError`/`OSError` split and the
  retained `errno`/`winerror` attributes are asserted against the real module.
- Ownership and native error fidelity (R2) hold on Linux: `make boundary-test`
  is unchanged from V4 (13 + 116), and the mutation experiment recorded in
  Progress showed a real-extension assertion failing when the classification
  was deliberately broken.
- R2's source-retention half was *asserted* by V2 but *unexercised* until
  2026-09-27. The scenarios naming it were declared without a `#[scenario]`
  binding and never ran (see Surprises). They are now bound and pass, and the
  mutation V2 requires — discarding the OS code in the classifier — was run and
  fails `retains_a_native_io_failure` as specified. Before that fix, the R2
  claim rested on `stream_error_tests.rs` alone, which tests the conversion
  directly rather than through a scenario.
- Scope and documentation accuracy (R3) are recorded in Progress, including two
  guide counts re-measured against the tree rather than trusted as prose and
  one users'-guide sentence falsified by measurement and corrected.

- Platform evidence (R4) was obtained from Windows and **it failed first**,
  which is what the plan asked of it. The `extension-tests-windows` job runs
  `make develop` plus `make test-extension` and therefore exercises the typed
  boundary for real rather than cross-compiling it. It was red at two heads,
  identically, while green on `main`; the two failures were a missing platform
  mark on the consume counterparts of two push-path rows, plus the Rust oracle
  defect described in Surprises. Both are fixed at `dafbfa4e`, where all eleven
  local gates pass, and **the fix is confirmed on Windows**: the fresh CI run at
  `dafbfa4e` is green, with the previously failing consume rows now passing
  and the two substituted rows executing there instead of skipping. The Windows
  claim therefore rests on observation, not on the diagnosis that predicted it.
  Two caveats stay attached to that. The RED run exposed only two of the three
  affected rows, because the third was hidden behind a `win32` skip; and the
  consume buffer path had been unobserved on Windows since before this branch,
  so the green is the first observation of it rather than a restoration of an
  earlier one. Both are recorded in Progress with their log paths.

Deviations and limits, all recorded rather than smoothed over: red evidence was
reconstructed after the implementation existed (see Surprises); and
`actionlint` is locally unobservable, so the GitHub Actions lint gate is proven
by its sub-checks plus a bounded diagnostic rather than by the aggregate target
(see Surprises). A cross-target compile of the `cfg(windows)` branches
supplements — and does not replace — the Windows runtime job.

### Remaining phase-6 scope

What 6.1.1 hands forward, stated so the next item does not have to re-derive it:

- **6.1.2 (proptest decoding parity)** is unblocked and is the next item. It
  needs what this branch did not build: proptest coverage of Rust-versus-Python
  *consume* parity — empty, ASCII, 2/3/4-byte UTF-8, invalid bytes replaced
  with U+FFFD, payloads around 1 MiB — with shrinking enabled and regression
  seeds committed. 6.1.1's properties cover the argument-validation window, not
  the decoding domain, so nothing here is a substitute. The
  `#[error(transparent)]` on `Stream` and the single conversion point are what
  make a parity failure legible when it arrives: the exception class is now
  decided in one place.
- **6.2.1 (the 20% wall-time gate)** remains entirely future work. 6.1.1 makes
  no performance claim and changed no hot path — the diff touches validation
  and error conversion, both off the transfer loop. The tuned phase-5 baseline
  is untouched.
- **`rust_consume_stream` is still implemented but not integrated**, including
  its production-use guards, as the Constraints require. 6.1.1 pins its error
  classification at the boundary without changing where it is called from; the
  capture-only dispatcher still awaits 6.1.2 and 6.2.1.
- **A shim asymmetry is recorded but deliberately unfixed.**
  `cuprum/_streams_rs.py` is byte-identical to `main` on this branch, and its
  consume path has no `buffer_size` pre-validation where its pump path does, so
  the two entry points prepare descriptors in different orders before the
  native call. The plan forbids reordering the shim, and 6.1.1's scope is the
  Rust boundary. The consequence is now observed rather than theoretical: the
  two Windows failures were exactly this asymmetry, on the consume side. Fixing
  it would be a shim behaviour change and belongs to its own item, not to a
  boundary refactor.
- **The `-1`-throwaway window property is gone**, replaced by an open-reader
  form that runs on both platforms. If a future change reinstates a `-1`
  throwaway to avoid opening a descriptor, it will silently re-skip the Windows
  job — which is the trap recorded in Surprises, not a hypothetical.

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
`test_rust_streams_boundary_property.py`,
`test_rust_streams_roundtrip_property.py`, `test_rust_errno.py`, and
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

> **Superseded — the premise is false.** `cuprum-rust` declares no `thiserror`
> requirement to reuse: it is declared only in `cuprum-streams`. The line above
> is kept as the text that was approved; M1 instead *added* the dependency,
> as the Decision log entry of 2026-09-26 records.

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

### Coverage-job evidence sources

The coverage-job evidence is cited from saved logs rather than re-fetched HTML,
so the citations survive the run's later state change. The failing coverage
attempt is job `109509268252`, whose full log is at `/tmp/611-cov-fail2.log`
(1,145,741 bytes); the timeout banner is the
`Failed: Timeout (>30.0s) from pytest-timeout` line, and the `gh` query that
reproduces the per-run figures is
`gh run view 36593121369 -R leynos/cuprum --json jobs`. The green comparison is
run `36356062517`, whose coverage job's rendering of the same test at 7.4 s is
in `/tmp/611-green-full.log`. The local differential — 11.52 s and 12.71 s cold
against 0.49 s warm — came from three invocations of
`.venv/bin/python -m pytest
cuprum/unittests/test_doctest_warning_contract.py -p no:randomly -q`
with `RUSTUP_HOME` pointed at an empty directory, and the toolchain payload
was measured by a cold
`rustup toolchain install nightly-2026-08-23 --profile minimal` into an isolated
`RUSTUP_HOME`, which reported 595 MB.

[bdd-manifest]: https://docs.rs/crate/rstest-bdd/0.5.0/source/Cargo.toml
[bdd-macros-manifest]: https://docs.rs/crate/rstest-bdd-macros/0.5.0/source/Cargo.toml
