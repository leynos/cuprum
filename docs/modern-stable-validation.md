# Modern-stable Rust validation

This document records the independent modern-stable Rust route, the Cargo
warning-policy boundary, and the evidence that supports it. Rust 1.85.0 remains
Cuprum's published source compatibility floor.

The roadmap 6.1.1 implementation, its required Linux and Windows runtime
evidence, and plan closeout were complete in merged PR #432 before this work
began. This validation does not extend that typed-error change or make it a
technical dependency of the modern compiler route.

## Toolchain selection

The issue proposal named Rust/Cargo 1.98.1, while rejecting 1.98.0 because of a
vtable miscompilation fixed by the
[1.98.1 patch release](https://blog.rust-lang.org/2026/09/03/Rust-1.98.1/). At
implementation, the
[official Rust release index](https://blog.rust-lang.org/releases/latest/)
identified Rust 1.99.1, released on 15 October 2026, as current stable. The
route therefore selects Rust/Cargo 1.99.1. It is an explicit Makefile pin;
`rust/rust-toolchain.toml` and the workspace's `rust-version = "1.85.0"` remain
unchanged.

Cargo 1.97 introduced `build.warnings` and the matching `CARGO_BUILD_WARNINGS`
environment variable. See the [Cargo configuration reference][cargo-warnings]
and [Rust 1.97 release notes][rust-1-97]. Cargo's setting changes local package
lint-warning fatality without changing the compiler flags that identify the
build artefact. The probe below found that Cargo also makes a warn-level
Rustdoc lint on the local package fatal when this setting is present. The
setting does not define every diagnostic emitted by dependency crates, build
tools, or packaging tools.

The pinned modern toolchain reported these versions on the Linux development
host:

| Tool    | Reported version                      |
| ------- | ------------------------------------- |
| `rustc` | `rustc 1.99.1 (58cf8f9c3 2026-10-13)` |
| Cargo   | `cargo 1.99.1 (5f94df478 2026-08-27)` |

_Table 1: Compiler and Cargo versions printed by the explicit toolchain guard._

## Route and warning-policy inventory

`make modern-stable-check` verifies the selected compiler, then runs workspace
check, Clippy, tests, documentation, and doctests in that order.
`make modern-stable-extension` verifies the pin, rebuilds the native extension
with Maturin, and runs `test-extension`. `make modern-stable` calls those two
aggregates sequentially. Cargo invocations use `--locked`, a dedicated
`rust/target/modern-stable` target directory, and
`RUSTFLAGS="-C codegen-units=1"`. Neither the target directory nor the
compiler-cache shape is shared with the existing routes.

| Route                     | Compiler selection                   | Warning mechanism                                        | Migration                          |
| ------------------------- | ------------------------------------ | -------------------------------------------------------- | ---------------------------------- |
| Modern check              | Rust/Cargo `1.99.1`                  | `CARGO_BUILD_WARNINGS=deny`                              | Migrated for local packages        |
| Modern Clippy             | Rust/Cargo `1.99.1`                  | Cargo policy plus existing manifest Clippy levels        | Migrated for local packages        |
| Modern tests              | Rust/Cargo `1.99.1`                  | `CARGO_BUILD_WARNINGS=deny`                              | Migrated for local packages        |
| Modern Maturin develop    | Rust/Cargo `1.99.1`                  | Environment passed through Maturin                       | Migrated after propagation control |
| Modern documentation      | Rust/Cargo `1.99.1`                  | `RUSTDOC_FLAGS` with `-D warnings`; Cargo policy omitted | Retained                           |
| Modern doctests           | Rust/Cargo `1.99.1`                  | Stable-compatible `RUSTDOC_FLAGS`; Cargo policy omitted  | Retained                           |
| Linux debug and lint      | Pinned dev-fast nightly and fragment | Existing `-D warnings`, codegen, and linker flags        | Unchanged                          |
| Published MSRV check      | Rust/Cargo `1.85.0`                  | Existing check command and manifest levels               | Unchanged                          |
| Whitaker                  | Independently pinned verifier        | `WHITAKER_RUSTFLAGS` includes `-D warnings`              | Unchanged                          |
| Release and wheel builds  | Existing release selection           | Existing route and manifest policy                       | Unchanged                          |
| Coverage and verification | Existing independent toolchains      | Existing route and manifest policy                       | Unchanged                          |

_Table 2: Routes, toolchains, and the policy boundary exercised by this change._

The published MSRV route uses Cargo 1.85.0, which predates Cargo 1.97 and does
not support `build.warnings`; it keeps its existing check and manifest policy.
The shared Linux dev-fast route and Whitaker retain their explicit
`-D warnings` flags because their compiler and verifier selections remain
independent of this migration. Release, wheel, coverage, and verification
routes keep their existing policy and compiler selection; changing them needs
their own validation. In particular, the wheel workflow's mutable `stable`
selection remains out of scope.

Cargo warning denial is scoped to local package lint warnings. It does not
replace explicit manifest lint levels. `rust/Cargo.toml` retains its
`[workspace.lints]` tables, including warning-level `pedantic` and
`unexpected_cfgs` groups and the existing explicit deny levels. No lint
baseline, workspace lint level, or source compatibility declaration is relaxed.

Rustdoc and doctests keep their own warning controls. The modern route uses
`RUSTDOC_FLAGS` for both; it does not send the nightly-only
`DOCTEST_RUSTDOC_FLAGS` to stable Rust. The existing nightly doctest route
retains those flags. The probe's Rustdoc result is recorded as an observation,
not a Rustdoc migration claim.

The wheel action's existing `toolchain: stable` selection is outside this route
and remains unchanged. The separate Maturin `build-backend` packaging
diagnostic tracked by [issue #430](https://github.com/leynos/cuprum/issues/430)
is not a Rust lint warning and is not addressed by Cargo's warning setting.

## CI and compiler-cache scope

CI adds one Linux `modern-stable` job, with a 90-minute ceiling and no matrix.
It installs the project Rust 1.85.0 toolchain and separately installs the
modern pin with the minimal profile and Clippy, without changing rustup's
default toolchain. Cargo build jobs and test threads follow
`LINUX_RUNNER_VCPUS`. Nextest is installed from the official prebuilt release
and checked against pinned archive and binary digests; there is no source-build
fallback.

The sccache shape is `modern-stable` with the explicit Rust version appended to
the key. The job is the sole writer for that family. It restores through the
shared cache-key renderer and saves only on a trunk push after a miss. The
existing Cargo registry and Git dependency key is unchanged, and `target` trees
are not archived. Saving also requires sccache to start; a fallback run still
completes without the cache and does not publish a cache generation.
`setup-sccache` keeps its existing 4 GiB cap and fallback behaviour.

## Warning and cache controls

The dependency-free Cargo fixture runs the selected compiler and holds its
source, target directory, profile, target triple, dependencies, and
code-generation flags fixed. Its expected sequence is:

1. `warn`: compile successfully and record the local warning.
2. `deny`: fail while Cargo reports the package as `Fresh`.
3. `deny` again: fail again without recompiling the package.
4. `warn`: succeed from the same fresh artefact.
5. Remove the warning and run `deny`: rebuild the edited package and succeed.

A separate clean-source control builds with the old `-D warnings` compiler
flag, removes that flag, and confirms one recompilation under the new flags.
After that artefact is built, changing only `CARGO_BUILD_WARNINGS` keeps it
fresh. The warning fixture independently confirms that policy-only changes
replay a cached warning without recompiling its local package. Clippy has its
own warn-level lint fixture and warm failure control. Where the environment has
a configured sccache wrapper, the warm policy replay runs through it as well.

The Rustdoc probe sets `CARGO_BUILD_WARNINGS=deny` without `RUSTDOCFLAGS` and
uses a warn-level rustdoc lint. Cargo 1.99.1 exited 101 and reported that
`build.warnings` denied the Rustdoc warning. This is informational: the modern
doc and doctest recipes deliberately omit Cargo's warning setting and retain
`RUSTDOC_FLAGS`; no Rustdoc migration is claimed. The existing doctest negative
control remains in the suite.

The full-gate harness injects an unused-function warning into `cuprum-streams`
for check, Clippy, and tests, then into `cuprum-rust` for Maturin. Each gate
runs cold with denial, warm under `warn`, twice warm under denial, and once
after source restoration. It uses `make -C` on a temporary Git worktree and one
shared modern-stable target directory. The worktree starts from committed
`HEAD`, so uncommitted source or build-policy edits in the caller's checkout
are not included in this evidence. Maturin runs with verbose Cargo output so
the harness can record exit status and whether the affected package was
compiled, checked, or fresh.

### Observed gate results

The results below come from Linux runs on the implementation candidate. They
record statuses and Cargo freshness; they do not claim a measured speedup.

| Check                                               | Result                                  | Evidence                                                              |
| --------------------------------------------------- | --------------------------------------- | --------------------------------------------------------------------- |
| Modern toolchain guard                              | Passed                                  | Exact `rustc -vV` and `cargo -V` output above                         |
| Rust 1.85.0 MSRV check                              | Passed                                  | `make msrv-check`; versions recorded below                            |
| Modern stable check, lint, tests, docs and doctests | Passed                                  | `make modern-stable`                                                  |
| Native extension suite                              | 103 passed; 4 Windows-only skips        | `make test-extension` on Linux                                        |
| Cargo warning fixture and warm-cache controls       | Passed, 24 tests                        | `make test-modern-stable-contract`                                    |
| Informational Rustdoc probe                         | Exit 101; Cargo denied the lint warning | `rust/target/cargo-build-warnings-contract/rustdoc-warning-probe.txt` |
| Full-gate warning controls                          | Passed; no control failures             | `make modern-stable-warning-controls`                                 |
| `rust/Cargo.lock` unchanged                         | Passed; SHA-256 recorded below          | CI lockfile check and local checksum                                  |

_Table 3: Implementation validation evidence._

The 1.85.0 compatibility check reported `rustc 1.85.0 (4d91de4e4 2025-02-17)`
and `cargo 1.85.0 (d73d2caf9 2024-12-31)`. The modern route ran with the
versions printed above. Its Rust test run reported 165 passed and no skipped
tests; the separate doctest run reported 8 passed and 3 ignored. The extension
skips are Windows-only tests and are not Linux runtime evidence. The lockfile
SHA-256 was `f28db8538186391b44b89c77f73530e2d82c879116ee2e25b9644ba4192c0591`.

The full-gate harness reported these exit statuses and Cargo freshness counts.
Exit 2 is the expected denied-warning result; exit 0 is the expected
permissive-prewarm or clean-source success. A cached warning replay has no
`Fresh` line for the local package, so the report labels that state separately
and records the positive number of fresh dependency lines.

| Gate    | Package          | Phase               | Exit | Package state         | Compile/check lines | Fresh lines |
| ------- | ---------------- | ------------------- | ---: | --------------------- | ------------------: | ----------: |
| check   | `cuprum-streams` | warning-cold        | 2    | Checking              | 159                 | 0           |
| check   | `cuprum-streams` | warning-prewarm     | 0    | Checking              | 16                  | 153         |
| check   | `cuprum-streams` | warning-warm        | 2    | Cached warning replay | 0                   | 152         |
| check   | `cuprum-streams` | warning-warm-repeat | 2    | Cached warning replay | 0                   | 152         |
| check   | `cuprum-streams` | clean               | 0    | Checking              | 2                   | 167         |
| clippy  | `cuprum-streams` | warning-cold        | 2    | Checking              | 2                   | 166         |
| clippy  | `cuprum-streams` | warning-prewarm     | 0    | Fresh                 | 1                   | 168         |
| clippy  | `cuprum-streams` | warning-warm        | 2    | Cached warning replay | 0                   | 152         |
| clippy  | `cuprum-streams` | warning-warm-repeat | 2    | Cached warning replay | 0                   | 152         |
| clippy  | `cuprum-streams` | clean               | 0    | Checking              | 2                   | 167         |
| test    | `cuprum-streams` | warning-cold        | 2    | Compiling             | 67                  | 92          |
| test    | `cuprum-streams` | warning-prewarm     | 0    | Compiling             | 9                   | 160         |
| test    | `cuprum-streams` | warning-warm        | 2    | Cached warning replay | 0                   | 155         |
| test    | `cuprum-streams` | warning-warm-repeat | 2    | Cached warning replay | 0                   | 155         |
| test    | `cuprum-streams` | clean               | 0    | Compiling             | 2                   | 167         |
| Maturin | `cuprum-rust`    | warning-cold        | 2    | Compiling             | 12                  | 10          |
| Maturin | `cuprum-rust`    | warning-prewarm     | 0    | Fresh                 | 0                   | 22          |
| Maturin | `cuprum-rust`    | warning-warm        | 2    | Fresh                 | 0                   | 22          |
| Maturin | `cuprum-rust`    | warning-warm-repeat | 2    | Fresh                 | 0                   | 22          |
| Maturin | `cuprum-rust`    | clean               | 0    | Compiling             | 1                   | 21          |

_Table 4: Cold, permissive prewarm, repeated warm denial, and clean-source
results from `make modern-stable-warning-controls`._

Each gate failed on the injected local warning when cold, succeeded after a
permissive prewarm, failed twice when the cached warning was replayed under
denial, and succeeded after removing the warning. The clean-source row shows
the affected package compiling or being checked again. Maturin emitted the
separate `build-backend` packaging diagnostic; the local Rust warning still
failed under Cargo's policy and the restored source rebuilt successfully.

## Limits and interpretation

The modern-stable runtime evidence is Linux-only. No Windows or macOS runtime
run is claimed for this route. The platform evidence for roadmap 6.1.1 belongs
to the already-merged implementation and does not extend to this compiler
experiment. The route relies on a POSIX shell and `env -u`; it is not a new
cross-platform validation promise.

`CARGO_BUILD_WARNINGS=deny` applies to local package lint warnings. It does not
fix Maturin packaging diagnostics, does not make a modern build MSRV evidence,
and does not make compiler outputs interchangeable between Rust versions or
backends. No speedup is claimed. The wheel route's mutable `stable` selection
remains a separately reviewable follow-up.

[cargo-warnings]:
  <https://doc.rust-lang.org/cargo/reference/config.html#buildwarnings>
[rust-1-97]:
  <https://blog.rust-lang.org/2026/07/09/Rust-1.97.0/>
