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
| Compile-test tier  | `profile.default.overrides` (`binary(compile_tests)`) | 600 s  | The two `trybuild` UI tests                  |
| Whole-run budget   | `profile.default.global-timeout`                      | 1200 s | One nextest run                              |
| Cargo watchdog     | `RUN_RUST_CARGO_WAIT_TIMEOUT`                         | 2700 s | One coverage action cargo call               |
| Job ceiling        | `timeout-minutes`                                     | 65 m   | The `coverage` job and its trunk counterpart |

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
does not recognise and keeps going, so on anything older the whole-run budget
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
`--jobs 1`, `-C codegen-units=1` and `CARGO_BUILD_JOBS=1`, which trybuild's
nested `cargo` obeys in turn; the coverage lane passes only `-D warnings`.

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
while it was healthy.

`terminate-after` is therefore raised to 10 for the `binary(compile_tests)`
tests, giving them 600 s, and left at 5 for the other 123. The override is
scoped by binary rather than by test name so both `trybuild` tests carry it:
`cuprum-streams::compile_tests::transition_privacy` is the same species and
measured 39.6 s from a warm scratch directory, and a bound that covered only
the test that happened to time out first would leave its sibling as the next
tripwire.

`largest_per_test_allowance_seconds()` reads the maximum across the default
profile and every override, not the profile alone. An override exists precisely
to exceed the profile it overrides, so reading only the profile would
understate the allowance and the containment assertions would pass while a test
could still outlast the tier meant to contain it. The 1200 s global budget
contains that 600 s allowance while remaining well inside the cargo watchdog.
The 2700 s watchdog was sized from roughly fifty successful runs: the worst
coverage step was 418 s in run 34071469378, the worst trunk coverage step was
322 s in run 34062626757, and run 34067223641 measured the worst work outside
the watchdog. None was a genuinely cold build.

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

The coverage jobs currently declare a 65-minute job ceiling in `ci.yml` and
`coverage-main.yml`; the earlier 60-minute figure is stale. That ceiling is
sized independently, by `required_ceiling()`, as the sum of each step's own
watchdog plus the work outside them and a margin. Here it lands on the
boundary: 2700 s + 300 s + 900 s is exactly 3900 s, or 65 minutes, so the
cold-build allowance is sized to fit a ceiling that already existed rather than
the ceiling being raised to fit it.

The coverage action uses `language: mixed`, so nextest does not bound the
Python half of the suite. `pytest-timeout` sets that per-test budget separately
through `timeout = 30` in `pyproject.toml`.
