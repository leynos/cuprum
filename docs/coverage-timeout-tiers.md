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
pins the first three relationships, the compile-test tier has a contract of its
own in `cuprum/unittests/test_compile_test_timeout_tier.py`, and the
termination allowance is pinned by
`cuprum/unittests/test_termination_allowance.py`.

_Table 1: Coverage timeout tiers._

| Tier               | Configuration key or environment variable             | Value  | Scope                                        |
| ------------------ | ----------------------------------------------------- | ------ | -------------------------------------------- |
| Per-test allowance | `profile.default.slow-timeout`                        | 300 s  | One Rust test                                |
| Compile-test tier  | `profile.default.overrides` (`binary(compile_tests)`) | 1800 s | The two `trybuild` UI tests                  |
| Whole-run budget   | `profile.default.global-timeout`                      | 2400 s | One nextest run                              |
| Cargo watchdog     | `RUN_RUST_CARGO_WAIT_TIMEOUT`                         | 3300 s | One coverage action cargo call               |
| Job ceiling        | `timeout-minutes`                                     | 75 m   | The `coverage` job and its trunk counterpart |

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
test as slow. That period also sets when a test is reported slow, and the worst
healthy test here is long enough to trip it: the `trybuild` UI test
`compile_time_ui` ran 62 s on run 35400748402, so the coverage log carries a
`SLOW` line for it. That is the warning working, not a fault.

300 s is not, however, a bound no healthy test reaches. `trybuild` compiles a
scratch crate for each UI case into a target directory of its own,
`rust/target/tests/trybuild/`, so the artefacts nextest builds in
`rust/target/` cannot serve that compilation. These tests also inherit
`RUSTFLAGS` from the `cargo` that invoked them, and `make test` passes
`-C codegen-units=1`, which trybuild's nested `cargo` obeys in turn; the
coverage lane passes only `-D warnings`.

`make test` once also pinned Cargo to a single job (`--jobs 1` and
`CARGO_BUILD_JOBS=1`), and the figures below were measured under that pin. The
pin is gone: Cargo now uses its default job count, or the caller's
`CARGO_BUILD_JOBS`. The figures remain measurements under the former pin. They
have not been re-measured without it, and concurrent compilations can contend
for resources, so they are not a guaranteed bound on the new behaviour. The
allowance sized from them is unchanged, and no tier depends on serial builds.

The scratch directory does persist, and a repeat run with it warm took 1.271 s
here. A cold one is the cost that matters, because it recurs on every fresh
checkout, every clean, and every change to `RUSTFLAGS`. Two measurements of
that cost were taken on 2026-09-19 with the gate's exact flags and an emptied
`rust/target/tests/trybuild/`, and they differ by more than a factor of two: a
single-test run took 277.049 s, and a full `make test` took 124.884 s. The gap
is `sccache` and machine load, not the test, so the budget is sized for the
larger figure. The coverage lane on run 35400748402 measured the same test at
62.484 s, with its scratch directory and compiler cache already warm. A gate
run on the same date killed it at the 300 s allowance —
`TERMINATING [>300.000s]` — while it was still compiling a dependency, that is,
while it was healthy. These 2026-09-19 figures are historical measurements from
before the shared compiler queue became the limiting factor; they explain why
the default 300 s tier was first raised, but do not bound a later run that
waits for shared Cargo resources.

In a later `make test` run, `cuprum-rust::compile_tests::compile_time_ui`
reached the 600 s compile-test limit after waiting on shared Cargo
package-cache locks. Its trybuild fixture build completed in 9m35s, and nextest
stopped the test before the later 92 tests could run. A 2026-10-16 run then
reached the 900 s allowance while nested Cargo was still waiting on the same
lock, before the later 99 tests could run. A 2026-10-17 run reached the 1200 s
allowance while the outer Rust test build had taken 19m31s and nested Cargo was
still compiling its trybuild scratch crate. These runs show that the earlier
measurements do not cover queue wait as well as compilation.

`terminate-after` is therefore raised to 30 for the `binary(compile_tests)`
tests, giving them 1800 s, and left at 5 for the other 123. Thirty 60 s periods
allow ten minutes beyond the observed 1200 s cutoff while retaining a finite
bound. The 40-minute global nextest budget leaves ten minutes beyond the
compile-test tier for the sibling UI test and the rest of the suite. The 300 s
general allowance remains unchanged. The override is scoped by binary rather
than by test name so both `trybuild` tests carry it:
`cuprum-streams::compile_tests::transition_privacy` is the same species and
measured 39.6 s from a warm scratch directory, and a bound that covered only
the test that happened to time out first would leave its sibling as the next
tripwire.

`largest_per_test_allowance_seconds()` reads the maximum across the default
profile and every override, not the profile alone. An override exists precisely
to exceed the profile it overrides, so reading only the profile would
understate the allowance and the containment assertions would pass while a test
could still outlast the tier meant to contain it. The 2400 s global budget
contains the 1800 s allowance. The watchdog contains 2400 s for nextest, its 60
s termination allowance, and 600 s for a cold build for 3060 s, with four
minutes of remaining headroom in the 3300 s watchdog. The job ceiling is 4500 s
(75 minutes). The watchdog remains sized above roughly fifty successful
coverage runs: the worst coverage step was 418 s in run 34071469378, the worst
trunk coverage step was 322 s in run 34062626757, and run 34067223641 measured
the worst work outside the watchdog. None was a genuinely cold build.

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

The external-source symlink test in `scripts/tests/test_boundary_workspace.py`
reaches one real Cargo check after the boundary verifier's other compile calls
use a recording seam. The check is bounded by `COMPILE_TIMEOUT_SECONDS` (600
s), and Cargo metadata calls in the test helper are bounded by
`CARGO_METADATA_TIMEOUT_SECONDS` (120 s). The test's 900 s marker sits above
their combined 720 s budget: `COMPILE_TIMEOUT_SECONDS` <
`SYMLINK_COMPILE_TEST_TIMEOUT_SECONDS`.

The marker covers the cold `CARGO_BUILD_JOBS=1` build measured at 181 s. The
test does not set Cargo's job count; parallelism belongs to its caller, so the
allowance does not depend on the caller's `-j` value. The global pytest timeout
remains 30 s, and only this test raises its own bound. The fast contract test
`test_real_compile_test_timeout_exceeds_compile_budget` pins the marker and
both timeout relationships.
