# CI cache ownership

Cuprum's continuous-integration caches are not an incidental optimization. They
are the only reason the Linux gates fit inside a two-vCPU paid runner, and a
cache with the wrong shape is invisible: a job that recompiles its whole
dependency graph on every run looks exactly like one that restored it. This
document records who owns which archive and why, so a reviewer can explain a
miss without re-deriving the key from the workflow source.

Every key renders once, in `.github/actions/cache-keys`, and both the job that
restores a path and the job that saves it read the rendered value from the
environment. The contract tests in `tests/test_ci_cache_ownership.py` read the
ownership back out of the workflows.

## The three families

| Family     | Holds                                     | Scoped by                             |
| ---------- | ----------------------------------------- | ------------------------------------- |
| `cargo-`   | `~/.cargo/registry` and `~/.cargo/git`    | lane                                  |
| `tool-`    | installed executables and uv environments | lane, image, interpreter              |
| `sccache-` | compiler output                           | lane, image, interpreter, build shape |

Every key also carries a generation, the operating system and the architecture.
`target` trees appear in no family at all: sccache holds the objects a `target`
archive would preserve, and a `target` archive is invalidated by every source
change while the objects inside it are not.

## Why the lane is in every key

`runner.environment` renders to `self-hosted` on Ubicloud and `github-hosted`
on a GitHub-hosted runner, and the two lanes read different cache services. An
archive written from an Ubicloud runner lands in Ubicloud's store, which a
GitHub-hosted job cannot read, and vice versa. Naming the lane in the key makes
that separation checkable rather than merely true; without it, one lane would
appear to own a key it can never read and would be permanently cold.

## Why the compiler cache is split further

The compiler cache carries two dimensions the other families do not, because
compiler output is only interchangeable between jobs that compile the same way.

The interpreter. `rust/cuprum-rust/Cargo.toml` declares `pyo3` with the
`extension-module` feature and without `abi3`, so the extension is compiled
against one specific CPython version. Objects built against 3.12 are useless to
a 3.14 build.

The build shape. An unoptimized `maturin develop` build, the same build with
`--release`, the Cranelift-backed lint build and the instrumented
`cargo llvm-cov` build all produce different objects from identical source.

This was measured rather than assumed. Until 2026-09-04 the compiler key named
neither dimension, so every Ubicloud job read one archive that the instrumented
Python 3.13 coverage job had written. In runs 33853599399 and 33854076859, over
an unchanged tree, the one reader on the same interpreter took 14 of its 17
cacheable compiles and the 3.12, 3.14 and 3.15a readers took none at all. Read
and write errors were zero throughout: the archive restored perfectly and
simply held nothing those jobs could use.

If `pyo3` ever adopts `abi3`, one archive could serve every interpreter and the
per-interpreter split becomes pure overhead.
`test_pyo3_is_still_declared_without_abi3` is the reminder to collapse it.

## One writer per family

A family is written by exactly one job, and only on a push to `main`. Pull
requests restore and never save: a pull-request branch cannot publish a
generation anyone should trust, and the attempt produces only
`Unable to reserve cache` noise and wasted upload time. A `workflow_dispatch`
run is likewise a reader, which is what makes repeated dispatches a usable way
to measure warm caches without churning the generation they are measuring.

| Family                                | Writer                                |
| ------------------------------------- | ------------------------------------- |
| Cargo registry, Ubicloud              | `ci.yml` `extension-tests`            |
| Cargo registry, GitHub-hosted         | `ci.yml` `lint-test`                  |
| Tools, per interpreter                | `ci.yml` `typecheck-test`, that leg   |
| Compiler, 3.13 unoptimized            | `ci.yml` `extension-tests`            |
| Compiler, 3.12/3.14/3.15a unoptimized | `ci.yml` `typecheck-test`, that leg   |
| Compiler, 3.13 release                | `ci.yml` `benchmark-ratchet`          |
| Compiler, 3.13 instrumented           | `coverage-main.yml` `coverage-upload` |
| Compiler, Cranelift lint              | `ci.yml` `lint-test`                  |

The interpreter matrix has one leg, 3.13, that only typechecks, because the
coverage job already runs that interpreter's suite. It compiles nothing, so it
installs no wrapper and saves no compiler archive; `extension-tests` owns the
3.13 unoptimized family instead. A writer that compiled nothing would restore
the previous generation and republish it unchanged for ever, reporting hits
while absorbing nothing new.

`tests/helpers/ci_cache_families.py` resolves the family each save step
actually publishes, expanding matrix legs and honouring a save condition that
names a matrix value. This matters because the `env` name a step writes is not
the archive it publishes: five jobs name `SCCACHE_CACHE_KEY` and write five
disjoint archives.

## Rolling keys

The compiler key ends in the run identifier, so it never matches exactly and
every restore is a `restore-keys` prefix match. That is deliberate. The
contents of a compiler cache depend on the source that was compiled, which no
lockfile hash captures, so a content-addressed key would hit for ever and
absorb nothing new. Each writing run publishes a fresh entry seeded from the
newest entry its prefix matched.

The corollary is that each writer run adds an entry rather than replacing one,
and old entries are never read again once a newer one exists. They persist
until the cache service sweeps them. Watch the weekly footprint if the number
of writing runs grows.

The Cargo and tool families are content-addressed instead, so their saves carry
an additional cache-hit guard: a run that already hit must not re-upload what
it just downloaded.

## Resource sampling

`ubicloud-standard-2` carries a 72 GB volume with roughly 31 GB free at job
start. Disk, not memory, is what kills a job on that shape, and it does so
silently. Every paid Linux job that compiles or runs a suite therefore brackets
its work with `.github/actions/resource-sampler`, which records peak memory,
peak disk and least-free disk to the log as well as to the step summary. The
log matters because the jobs API exposes it and does not expose the summary.

The coverage jobs additionally discard `target/llvm-cov-target` once the report
is written, printing `df -h` either side. It has no later consumer, it is the
second tree on the volume, and removing it before any cache save keeps it out
of the archives and out of the measured high-water mark.
