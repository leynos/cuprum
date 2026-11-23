# Architectural decision record (ADR) 0030: Retain default nextest scheduling

## Status

Accepted on 2026-11-22. Keep cargo-nextest's default scheduling for the Rust
suite: no test priorities, no test groups, and no `test-threads` or
`threads-required` in any profile. The experiment in issue [#606] is closed as
superseded because [ADR-027] removed the binaries it proposed to re-order.

## Date

2026-11-22.

## Context and problem statement

Issue [#606] asked whether scheduling Cuprum's expensive `trybuild` tests
differently would shorten the existing Continuous Integration (CI) critical
path without raising Ubicloud charges or reducing test fidelity. It was written
against `24c39650e388975a9b9abdc884cdf3e237745bb9` on 10 October 2026. At that
revision `rust/.config/nextest.toml` declared a 20-minute `global-timeout` and a
`binary(compile_tests)` override raising the per-test allowance to ten
60-second periods. Two historical cold `compile_time_ui` measurements (124.884
s and 277.049 s) motivated the question. Both predate removal of the one-job
Cargo limit and were never a contemporary CI baseline or a predicted saving.

The experiment proposed three arms: a priority for the compile tests, a
one-slot test group, and the two combined. It also carried a correction to its
own hypothesis: nextest orders equal-priority tests lexicographically, sorted
by binary name and then test name ([test priorities]), so `compile_tests` may
already have started early, and a missing `priority` was not evidence of a
defect. Concurrent compile-test processes would not, by themselves, have proved
contention on one lock or one target directory.

[ADR-027] then moved the compile-time contracts into the ordinary build graph.
`trybuild` compiled a scratch crate that could never reuse the workspace's
artefacts, so each checkout rebuilt the dependency graph inside one test's
allowance while every compiler invocation queued for the host-wide admission
pool. ADR-027 replaced the UI cases with doctests and removed `trybuild`, the
`compile_tests` targets, and the `binary(compile_tests)` override. That removal
landed in commit `7126a1ffb` on 2026-10-25.

The refreshed baseline is `87de64c2cad4e970984760096faf278bd70439f3` on
2026-11-22. No `compile_tests` target, `trybuild` dependency, `.stderr`
fixture, or `binary(compile_tests)` override remains. The candidates in [#606]
therefore have no target: there is no long-running binary whose position in the
schedule could be changed.

## Decision drivers

- A scheduling change must be measured on the work it re-orders, against the
  same tests, the same runner shape, and the same locked dependencies.
- Issue [#606] authorizes no paid shadow jobs, extra matrix legs, synthetic
  dispatch loops, scheduled benchmark lanes, or larger runners.
- Timeouts, retry policy, warning enforcement, features, coverage
  instrumentation, and target layout are outside this decision.
- The decision must remain legible when a long-running test binary next
  appears, so that the experiment is repeated rather than assumed redundant.

## Options considered

1. **Add `priority = 100` to a `binary(compile_tests)` override.** Rejected.
   The binary no longer exists, so the change would match nothing.
2. **Assign the compile tests to a group with `max-threads = 1`.** Rejected for
   the same reason. The group would have no member, and the configuration would
   claim a contention remedy that nothing exercises.
3. **Evaluate both together.** Rejected. The independent arms have no subject,
   so their combination has none either, and the issue forbids assuming that
   the effects would add.
4. **Retain default scheduling and pin it.** Chosen. The suite already runs
   under nextest's defaults, and a contract records that this is deliberate
   rather than unexamined.

| Option                 | Has a target | Paid runs needed | Fidelity risk     |
| ---------------------- | ------------ | ---------------- | ----------------- |
| Priority only          | No           | None authorized  | None, but vacuous |
| One-slot group         | No           | None authorized  | None, but vacuous |
| Combined               | No           | None authorized  | None, but vacuous |
| Retain default and pin | Yes          | None             | None              |

_Table 1: The candidate arms and the retained baseline._

## Decision outcome

Cuprum keeps cargo-nextest's default scheduling. `rust/.config/nextest.toml`
declares the per-test allowance and the whole-run budget, and nothing about
ordering, grouping, or concurrency.
`cuprum/unittests/test_nextest_scheduling_baseline.py` refuses a top-level
`test-groups` table, a `test-threads` or `threads-required` setting on any
profile, and a `priority`, `test-group` or `threads-required` setting on any
override. Each refusal names this ADR, so a future change must update the
decision record rather than drifting past it.

The guard permits an override that declares only a timeout, because an
allowance is a timeout-tier concern and not a scheduling one. The tiers are
pinned by `test_nextest_timeout_tiers.py` and `test_termination_allowance.py`,
and the filter that names a retired binary is pinned separately by
`test_compile_contracts_build_graph.py` under [ADR-027]. The division keeps
each contract with the concern it expresses. The keys themselves follow the
upstream schema: a test group is a top-level `[test-groups]` table assigned
through an override's `test-group` field ([test groups]), and a priority is an
override's `priority` field.

The concurrency a run actually uses comes from its caller, not from the
configuration: both coverage jobs bind `NEXTEST_TEST_THREADS` and
`CARGO_BUILD_JOBS` to `LINUX_RUNNER_VCPUS`, which is 2 for
`ubicloud-standard-2`. `tests/test_ci_test_coverage_overlap.py` pins the
binding, and `tests/test_ci_worker_bounds.py` pins the vCPU constant to the
runner label.

### Refreshed baseline

Two ordinary `coverage-main.yml` runs and one ordinary pull-request `ci.yml`
coverage job were read from the existing workflows. No workflow, job, runner,
matrix leg, or dispatch was added to obtain them, and no paid runner-second was
spent on the experiment.

_Table 2: nextest execution at the refreshed baseline._

| Run                       | Checkout     | Tests                  | Nextest run | Slowest test |
| ------------------------- | ------------ | ---------------------- | ----------: | -----------: |
| coverage-main 45047782739 | `87de64c2c`  | 196 passed, 30 skipped | 2.726 s     | 1.892 s      |
| coverage-main 45009685886 | `f3c5e327d`  | 196 passed, 30 skipped | 2.943 s     | 1.722 s      |
| ci.yml 45043437758        | PR merge ref | 196 passed, 30 skipped | 3.003 s     | 1.914 s      |

Each run used cargo-nextest 0.9.120 with its `default` profile,
`NEXTEST_TEST_THREADS=2`, and `CARGO_BUILD_JOBS=2`, and invoked nextest through
`cargo llvm-cov`:

```bash
cargo llvm-cov nextest --manifest-path rust/Cargo.toml --workspace \
  --all-targets --all-features --cobertura --output-path coverage.rust.xml
```

The four binaries that executed tests were `cuprum-native-io` (19),
`cuprum-rust` (61), `cuprum-rust::consume_boundary_parity` (8), and
`cuprum-streams` (108). Nextest reported a fifth binary that executed no test;
the installed output does not name a binary whose tests all pass, so its
identity is unrecorded.

The whole nextest run is now shorter than the slowest ordinary compile in the
same job: the outer test build took 33.71 s, 35.31 s, and 34.79 s in the three
samples, while every test together took under 3.1 s. Re-ordering tests cannot
shorten a critical path that is dominated by compilation.

### Doctest successor

ADR-027 maps the former UI cases to doctests, and `make test` and the coverage
lane already run them. Each sampled coverage run collected 8 doctests, passed
all 8, and ignored 3. Four `compile_fail` doctests execute on Linux; the three
Windows-only cases are compiled only there. The mapping is documented intent,
not a case-by-case verification, and the diagnostic guarantee is weaker than
the removed `.stderr` snapshots: a `compile_fail` doctest pins an error code,
only nightly rustdoc checks that code, and stable Windows rustdoc accepts any
compile error. The positive control in the same doc comment limits, but does
not remove, that risk.

### Evidence limits

The retired binary's nested compile time, scratch-tree state, lock-wait
attribution, and child-process overlap are not applicable rather than zero:
nothing takes a second Cargo lock. Central processing unit (CPU) utilization is
unknown, because the resource sampler records peak memory and disk, not
processor time. The three samples in Table 2 are warm-cache runs: the Cargo
registry and tool caches hit, and sccache reported an 83.41 % hit rate. A
naturally occurring cold run is recorded in the addendum below. These limits
are recorded so that a future reader does not read a missing measurement as a
zero cost.

## Consequences

### Positive

- The suite keeps nextest's default ordering, which already starts every test
  within the first three seconds of the run.
- No configuration claims a scheduling remedy for a binary that no longer
  exists.
- The guard fails a future priority, group, `test-threads`, or
  `threads-required` change until someone reopens this decision with fresh
  evidence.
- The experiment costs nothing: no runner, cache entry, artefact, dispatch, or
  matrix leg was added.

### Negative

- There is no measured comparison for a scheduling change, because there was
  nothing to change. If a future test binary becomes long-running, this ADR
  does not answer whether re-ordering it would help; it requires the experiment
  to be repeated.
- The guard forbids a priority or a test group wherever one appears, not only
  on a `compile_tests` filter. A legitimate future use must update this ADR in
  the same change, which is intended friction rather than an oversight.

## Review and rollback

- **Owner:** the Cuprum maintainers (`leynos`).
- **Review date:** 2027-11-22, or sooner if any nextest test exceeds the 300 s
  per-test allowance or a test binary starts a nested build.
- **Rollback:** revert this ADR and its guard contract. Reopen issue [#606] and
  repeat its Stage 1 and Stage 2 protocol against the new binary, using only
  the existing required CI executions. Do not reintroduce a priority or a test
  group as an unmeasured default.

## Addendum (2026-11-23): a naturally cold observation

The evidence limits above say the samples are warm-cache runs. The pull-request
coverage job at head `3808d1413` supplies the missing cold observation, and it
changes the comparison by an order of magnitude without changing the conclusion.

That job was a genuine cold compile rather than a warm one: sccache served 1 of
725 compile requests, a 0.23 % hit rate, against 83.41 % in the warm samples.
Under that condition the outer test build took 2 m 31 s for the nextest phase
and 2 m 29 s for the doctest phase, while the nextest run itself still took
only 3.096 s across 196 tests and 30 skips. The coverage step ran from
16:05:30Z to 16:28:50Z, 1 400 s, and the job from 16:02:38Z to 16:29:00Z, 1 582
s, both inside the unchanged 3 300 s watchdog and 75-minute ceiling.

The conclusion is unchanged and now better supported: a cold compile makes the
compilation share of the critical path larger, not smaller, so no reordering of
tests could have shortened it. The retired `trybuild` shape differed precisely
because it paid a second cold compile inside one test's allowance; no
scheduling control can recover that cost, which is why ADR-027 removed the
shape rather than rescheduling it.

## Assumptions

- The global nextest budget stays at 40 minutes. It was sized to leave ten
  minutes beyond the retired 1800 s tier, and the cargo watchdog and the
  boundary-harness marker are sized against it, so narrowing it is a separate,
  measured change. [ADR-027] records the same assumption.
- Nextest resolves the configuration from the Cargo workspace root.
  `cuprum/unittests/test_nextest_config_discovery.py` asks the installed tool
  to report the version requirement the resolved file declares, so a
  configuration that nextest never loads fails that contract rather than
  passing silently.

[#606]: https://github.com/leynos/cuprum/issues/606
[ADR-027]: adr-0027-compile-contracts-in-the-build-graph.md
[test groups]: https://nexte.st/docs/configuration/test-groups/
[test priorities]: https://nexte.st/docs/configuration/test-priorities/
