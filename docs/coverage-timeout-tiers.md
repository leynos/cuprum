# Coverage timeout tiers

A timeout that fires on a healthy run is worse than no timeout: it turns a slow
machine into a red build, and the fix that suggests itself — raise the number —
is the one that hides a real hang. These lanes therefore carry four tiers
rather than one, each sized to contain the tier inside it, and each asserted by
value so a tier cannot drift back towards a default that no longer fits. This
document records what each tier bounds and why it holds the value it does.

The tiers are ordered per-test allowance < global-timeout < cargo watchdog <
job ceiling. The first two live in `rust/.config/nextest.toml`; the third is
the shared coverage action's cargo watchdog; and the fourth is GitHub Actions'
job timer. The contract in `cuprum/unittests/test_timeout_ordering_contract.py`
pins the first three relationships, and the termination allowance is pinned by
`cuprum/unittests/test_termination_allowance.py`.

_Table 1: Coverage timeout tiers._

| Tier               | Configuration key or environment variable | Value  | Scope                                        |
| ------------------ | ----------------------------------------- | ------ | -------------------------------------------- |
| Per-test allowance | `profile.default.slow-timeout`            | 300 s  | One Rust test                                |
| Whole-run budget   | `profile.default.global-timeout`          | 2400 s | One nextest run                              |
| Cargo watchdog     | `RUN_RUST_CARGO_WAIT_TIMEOUT`             | 3300 s | One coverage action cargo call               |
| Job ceiling        | `timeout-minutes`                         | 75 m   | The `coverage` job and its trunk counterpart |

The file sits in `rust/` rather than the repository root because nextest
resolves `.config/nextest.toml` from the Cargo workspace root and searches no
parent directory. Cuprum keeps no root `Cargo.toml`, so its workspace is
`rust/`. A copy at the repository root is never read: the tiers it declares are
inert while every value assertion still passes, because those read the file
rather than the run. Worse, `generate-coverage` treats a repository-root config
as one already supplied and skips writing its own fallback, so the run proceeds
on nextest's built-in defaults — a 60 s slow warning that never terminates the
test, and no whole-run budget at all.

The file therefore opens with `nextest-version = "0.9.100"`, the release that
first understood `global-timeout`. Nextest warns about a configuration key it
does not recognize and keeps going, so on anything older the whole-run budget
is not rejected but simply dropped, and the suite still passes: the same
inert-tier failure as a misplaced file, reached from the other direction. The
declaration is honoured from 0.9.55 onwards; releases older than that ignore it
as an unknown key too, so the `Makefile` checks the installed version before
running nextest and refuses anything below the floor. Both sites are pinned by
`test_the_nextest_floor_agrees_between_the_config_and_the_makefile` in
`cuprum/unittests/test_toolchain_pins.py`.

The 300 s per-test allowance is `period = "60s"` multiplied by
`terminate-after = 5`, so nextest kills a hung test after it has reported the
test as slow. Every Rust test sits on this tier, and each is bounded by its own
work rather than by a build.

That was not always so. Until
[ADR-027](adrs/adr-0027-compile-contracts-in-the-build-graph.md), the
`trybuild` UI tests ran in the `compile_tests` binaries under a
`binary(compile_tests)` override. `trybuild` compiled a scratch crate with
flags no workspace artefact matched, so each checkout rebuilt the whole
dependency graph inside one test's allowance while every compiler invocation
queued for the host-wide compiler admission pool. That override was raised from
300 s to 600 s, 900 s, 1200 s and 1800 s, and still expired mid-build on
2026-10-20, with between 66 and 382 admission requests queued ahead. The queue
belongs to the host rather than the test, so no allowance could bound it. Those
contracts are now ordinary and `compile_fail` doctests compiled against the
workspace's existing artefacts, and the override is gone.
[ADR-030](adrs/adr-0030-retain-default-nextest-scheduling.md) records the
scheduling half of that outcome: with the long-running binaries removed, the
priority and test-group candidates in issue #606 have no target, so nextest's
default ordering, grouping, and concurrency are retained and pinned.

`largest_per_test_allowance_seconds()` reads the maximum across the default
profile and every override, not the profile alone. An override exists precisely
to exceed the profile it overrides, so reading only the profile would
understate the allowance and the containment assertions would pass while a test
could still outlast the tier meant to contain it. The 2400 s global budget
contains the 300 s allowance. It was sized to leave ten minutes beyond the
retired 1800 s tier and is kept rather than narrowed, because the watchdog and
the boundary-harness marker below are sized against it; narrowing it is a
separate, measured change. The watchdog contains 2400 s for nextest, its 60 s
termination allowance, and 600 s for a cold build for 3060 s, with four minutes
of remaining headroom in the 3300 s watchdog. The job ceiling is 4500 s (75
minutes). The watchdog remains sized above roughly fifty successful coverage
runs: the worst coverage step was 418 s in run 34071469378, the worst trunk
coverage step was 322 s in run 34062626757, and run 34067223641 measured the
worst work outside the watchdog. None was a genuinely cold build.

The watchdog must satisfy
`watchdog >= global-timeout + termination + cold build`. Termination is the
largest configured `slow-timeout.grace-period`, with a 60 s floor; the contract
reads it through `termination_allowance_seconds()`. That floor is this
repository's, not nextest's: nextest waits 10 s by default. It is a floor on an
allowance rather than a reading of the tool, so it can only raise the sum the
watchdog must contain, never lower it. Cuprum configures no grace period. The
cold-build term is the allowance that keeps the watchdog a hang detector rather
than a schedule: these lanes archive no `target` tree, so a branch's first run
compiles everything sccache cannot serve, and a budget shared with the test run
can be spent before a test starts. `COLD_BUILD_ALLOWANCE_SECONDS` is 600 s
there, taken from the estate's own cold run rather than derived from Cuprum's
warm one — Netsuke, whose suite is roughly twenty-five times the 112 tests
measured here, spent about 512 s and was killed at 600 during report
generation. Cuprum's whole warm Rust invocation was 83 s on run 35391248951.
The contract asserts the three-term sum.

The coverage jobs declare a 75-minute job ceiling in `ci.yml` and
`coverage-main.yml`. That ceiling is sized independently, by
`required_ceiling()`, as the sum of each step's own watchdog plus the work
outside them and a margin. Here it lands on the boundary: 3300 s + 300 s + 900
s is exactly 4500 s, or 75 minutes, so the ceiling includes the full watchdog,
measured outside work, and the required margin.

The coverage action uses `language: mixed`, so nextest does not bound the
Python half of the suite. `pytest-timeout` sets that per-test budget separately
through `timeout = 30` in `pyproject.toml`.

## Retired trybuild compile tier

`trybuild` held the compile-time contracts until
[ADR-027](adrs/adr-0027-compile-contracts-in-the-build-graph.md) moved them
into the ordinary build graph. Its scratch crate could never share the
workspace's artefacts: it forced a private target directory, added
`--cfg trybuild` to every crate, and folded the host crate's whole
`[dev-dependencies]` table into the scratch graph. This section records what
that shape cost, because the allowance history above is only intelligible with
it.

The gate log of `make test` against `c711ac95d`, recorded in
[#653](https://github.com/leynos/cuprum/issues/653), measured the two UI
binaries at 1300.050 s (`compile_time_ui`) and 433.030 s (`transition_privacy`)
inside a 1735.071 s nextest window. The other 165 tests summed to 1.963 s and
none exceeded 0.144 s, so the two binaries accounted for 1733.080 s of the
1735.043 s summed test duration, or 99.887 %. A green log cannot show the
nested build directly: nextest captures a passing test's output and prints it
only on failure, and that log held no `Compiling`, `build-limits`, or
`rustc-heartbeat` line.

The cold build's composition is reconstructible from the dependency graph even
though its per-crate seconds are not. At `c711ac95d`, a kind-aware
`cargo metadata` walk resolves the scratch manifest trybuild would have
generated as the host crate's `[dependencies]` plus its `[dev-dependencies]`,
both as ordinary dependencies, and then the normal and build closure beneath
them. The host crate's own build needs only the first table's closure. Counted
in package instances, with distinct crate names in parentheses:

_Table 2: Composition of the retired scratch graph at `c711ac95d`._

| Host crate       | Required closure | Scratch closure | Inherited-only | Inherited share |
| ---------------- | ---------------: | --------------: | -------------: | --------------: |
| `cuprum-rust`    | 82 (69)          | 222 (197)       | 140 (135)      | 63.1 %          |
| `cuprum-streams` | 50 (38)          | 116 (101)       | 66 (64)        | 56.9 %          |

The inherited-only set is the dev-dependency tail the UI fixtures never
imported: `proptest`, `rstest`, `rstest-bdd`, `rstest-bdd-macros` and
`trybuild` for `cuprum-rust`, and `cap-std`, `insta`, `proptest`, `rstest`,
`tempfile` and `trybuild` for `cuprum-streams`, together with their transitive
dependencies. It is the majority of both scratch graphs, and the observed cold
run is consistent with that share: when the 1800 s allowance expired on
2026-10-20, the nested build had produced 122 dependency libraries with between
66 and 382 admission requests queued ahead (ADR-027). The attribution is by
graph composition because the gate log retained no per-crate timings; it does
not claim a split in seconds.

A green `make test` on `2ad7f393b` shows the result. Nextest started 196 tests
and ran them in 7.654 s; the slowest was 2.433 s
(`generated_payloads_round_trip_and_reach_eof`) and the next 2.272 s, with
every other test under 0.3 s. The recipe recorded
`rust_test_workload: 454.1 s`, which spans the test-profile build, the nextest
run, and the doctests, so the dominant cost is now compilation in the ordinary
build rather than a timer charged to one test.

After ADR-027 the binaries and the `binary(compile_tests)` override are gone.
No nextest test target named `compile_tests` remains, and the compile-fail
contracts run as doctests against the workspace build.
`cuprum/unittests/test_compile_contracts_build_graph.py` refuses a `trybuild`
dependency, either Cargo integration-test entry point for a `compile_tests`
target, and a nextest override that names one in any profile, so the allowance
cannot drift back up in place of a shape that no longer exists.

The upstream request that would let a `trybuild` consumer share the workspace's
artefacts already exists.
[trybuild #330](https://github.com/dtolnay/trybuild/issues/330) proposes
seeding the private target from the parent workspace under an opt-in feature,
and remains open. It supersedes
[#328](https://github.com/dtolnay/trybuild/issues/328), which carried the
problem statement for sharing the target. The draft pull request that
implemented that sharing, [#329](https://github.com/dtolnay/trybuild/pull/329),
was closed because a shared target reintroduced Cargo's build-lock contention
between the parent workspace and the spawned build. Cuprum no longer depends on
`trybuild`, so it does not wait on any of these outcomes; the request stays
relevant to projects that still do.

Two limits are recorded rather than inferred. The requested per-crate seconds
split between the required closure and the dev-dependency tail is not
available: neither the gate log nor `trybuild` reports per-crate timings, so
graph composition is the recorded substitute and is not presented as a timing
measurement. And [#653](https://github.com/leynos/cuprum/issues/653)'s
requirement to preserve the exact `.stderr` snapshots is superseded by ADR-027,
which pins each compile-fail case to its named error code beside a positive
control. Only nightly rustdoc checks the named code; stable rustdoc accepts any
compile error, and the Windows-only doctests run on stable, so their codes are
not checked in CI. The positive control reduces that risk but does not remove
it: an unrelated error specific to a negative case can still make it pass on
stable rustdoc, as ADR-027 records. Those snapshots were removed rather than
loosened, and this branch does not restore them.

## Boundary harness compile tier

This pytest-only tier sits outside the Rust and coverage tiers in Table 1 and
is not configured through nextest.

The external-source symlink test in `scripts/tests/test_boundary_workspace.py`
reaches one real Cargo check after the boundary verifier's other compile calls
use a recording seam. The check is bounded by `COMPILE_TIMEOUT_SECONDS` (3600
s), and its two Cargo metadata calls are each bounded by
`CARGO_METADATA_TIMEOUT_SECONDS` (120 s). Their combined subprocess budget is
3840 s, below the test's 3900 s marker: `COMPILE_TIMEOUT_SECONDS` (3600 s) <
`SYMLINK_COMPILE_TEST_TIMEOUT_SECONDS` (3900 s). The outer marker leaves 60 s
for surrounding test work and catches a wedged test process.

The historical 181-second observation used `CARGO_BUILD_JOBS=1` outside the
resource contract. It is not a current execution-budget measurement. The test
does not set Cargo's job count; parallelism belongs to its caller. The
suite-wide global `timeout = 30` remains tight, and only this test raises its
bound. The fast contract test
`test_real_compile_test_timeout_exceeds_compile_budget` pins the marker and
both timeout relationships. These bounds are end-to-end watchdogs rather than
workload-only budgets.
[ADR-0029](adrs/adr-0029-shared-build-resources-and-prepared-cargo-state.md)
records the shared-resource decision and notes that a top-level workload lease
is follow-up work.

## Doctest contract tier

The doctest-warning contract in
`cuprum/unittests/test_doctest_warning_contract.py` makes two real Cargo calls,
one per Rustdoc route. Each call is bounded by `DOCTEST_CALL_TIMEOUT_SECONDS`
(3600 s) and owns its process group. The test's pytest mark is
`DOCTEST_TEST_TIMEOUT_SECONDS` (7260 s), twice the per-call bound plus a 60 s
margin for surrounding work, and a fast contract test pins that relationship.

The per-call bound is an end-to-end watchdog, not an execution budget. Both
calls compete for the host's compiler-admission tokens, and the coordinator
exposes no workload lease that could exclude that queue. On 2026-11-03 a
270-second per-call bound expired while the captured stream reported
`[build-limits] warning_doctest: waiting for a compile slot (343 ahead, 45s)`.
The 3600 s bound therefore matches the boundary compile tier's finite watchdog
rather than a measured workload; an expired call still terminates its Cargo
descendants. Replace this with a measured execution budget once a documented
workload lease exists.

## Packaging build measurement

On 2026-10-26, the grouped packaging consumers built each frontend and archive
once through `DistributionArtefactFactory`. The focused session used a prepared
snapshot and fresh run-local Cargo homes. A second Maturin wheel build used a
new Cargo home and the same configuration-keyed target as the first, so the
target was cold for the first build and warm for the second.

_Table 3: Local packaging route timing observations._

| Phase                       | Duration    | Conditions                                      |
| --------------------------- | ----------: | ----------------------------------------------- |
| Dependency preparation      | 21.2 s      | Snapshot reused; run-local home copied          |
| uv source archive           | 0.10 s      | Grouped session; prepared snapshot              |
| uv wheel                    | 0.05 s      | Grouped session; prepared snapshot              |
| Maturin source archive      | 7.15 s      | Grouped session; prepared snapshot              |
| Maturin wheel, empty target | 38.6–49.1 s | Compiler objects already cached                 |
| Maturin wheel, warm target  | 23.8 s      | Separate home; same target; `sccache` available |
| Maturin wheel, rebuilt      | 163 s       | Fresh target and home; 2026-10-30; loaded host  |

The packaging command watchdogs are 180 s for the uv frontend and 3600 s for
Maturin. `CUPRUM_PACKAGING_BUILD_TIMEOUT` can raise both end-to-end caps for a
runner with more queueing, but never lowers either below its default, and the
pytest mark stays 60 s above the largest configured cap. The 49.1 s Maturin
wheel was measured in the grouped session; the 38.6 s sample used a separate
empty target; in both, compiled objects were already available to the compiler
cache. The 163 s sample rebuilt the native extension from an empty target, but
a later gate run waited behind 67 and then 123 compiler-admission requests and
overran a 600 s Maturin bound. The native watchdog therefore remains the finite
end-to-end bound inherited from the pre-factory contract, not the measured
workload, because the coordinator exposes no workload lease that could exclude
that queue. This was a developer-host observation, not a CI-runner or
concurrent-run measurement. It does not establish the workload-only budget
under a top-level resource lease. Repeat the measurement on the target CI
runner and under the coordinator's documented workload lease before revising
timeout contracts.

The current compiler coordinator exposes bounded compiler scopes only, not a
bounded outer Cargo-workload lease; its queue wait has no finite limit. Until
that contract exists, no concurrent workload timing or admission bound is
claimed here. The grouped packaging execution and the configuration-keyed
target directories are recorded in
[ADR-0029](adrs/adr-0029-shared-build-resources-and-prepared-cargo-state.md).
See the
[external compiler admission contract](developers-guide.md#external-compiler-admission-contract).
