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
both timeout relationships.

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

_Table 2: Local packaging route timing observations._

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
runner with more queueing, and the pytest mark stays 60 s above the largest
configured cap. The 49.1 s Maturin wheel was measured in the grouped session;
the 38.6 s sample used a separate empty target; in both, compiled objects were
already available to the compiler cache. The 163 s sample rebuilt the native
extension from an empty target, but a later gate run waited behind 67 and then
123 compiler-admission requests and overran a 600 s Maturin bound. The native
watchdog therefore remains the finite end-to-end bound inherited from the
pre-factory contract, not the measured workload, because the coordinator
exposes no workload lease that could exclude that queue. This was a
developer-host observation, not a CI-runner or concurrent-run measurement. It
does not establish the workload-only budget under a top-level resource lease.
Repeat the measurement on the target CI runner and under the coordinator's
documented workload lease before revising timeout contracts.

The current compiler coordinator exposes bounded compiler scopes only, not a
bounded outer Cargo-workload lease; its queue wait has no finite limit. Until
that contract exists, no concurrent workload timing or admission bound is
claimed here. See the
[external compiler admission contract](developers-guide.md#external-compiler-admission-contract).
