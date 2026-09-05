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

_Table 1: The cache families, what each holds, and what scopes its key._

Every key also carries a generation, the operating system, and the architecture.
`target` trees appear in no family at all: sccache holds the objects a
`target` archive would preserve, and a `target` archive is invalidated by every
source change while the objects inside it are not.

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
`--release`, the Cranelift-backed lint build, and the instrumented
`cargo llvm-cov` build all produce different objects from identical source.

This was measured rather than assumed. Until 2026-09-04 the compiler key named
neither dimension, so every Ubicloud job read one archive that the instrumented
Python 3.13 coverage job had written. In two dispatches over an unchanged tree
[^1][^2], the one reader on the same interpreter took 14 of its 17 cacheable
compiles and the 3.12, 3.14, and 3.15a readers took none. Read and write errors
were zero throughout: the archive restored perfectly and simply held nothing
those jobs could use.

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

_Table 2: The single job that publishes each family on a push to `main`._

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

## When to split a family, and when not to

Splitting a compiler-cache family is not free and not automatically right. Two
numbers decide it, and both must be measured rather than estimated:

1. What the split saves. Run the job cold, run it again warm over an unchanged
   tree, and compare the wall time of the step that compiles. Hit counts alone
   overstate the case: seventeen cacheable compiles sounds like a lot and costs
   a few seconds.
2. What the split costs. Read the archive size from the `Cache saved with key`
   step in the writer's log. Do not infer it from the size of the archive being
   replaced.

Split when the saving is worth the archive. Collapse when it is not. Record
both numbers wherever the decision is written down, so the next person can
re-derive it rather than inherit it.

The worked example is this repository's own split, measured on 2026-09-04
between the cold writer[^4] and a warm dispatch[^5] over an unchanged tree.

| Job                    | Compiling step             | Cold  | Warm  | Saved |
| ---------------------- | -------------------------- | ----- | ----- | ----- |
| `typecheck-test` 3.12  | Run tests                  | 112 s | 101 s | 11 s  |
| `typecheck-test` 3.14  | Run tests                  | 99 s  | 90 s  | 9 s   |
| `typecheck-test` 3.15a | Run tests                  | 98 s  | 88 s  | 10 s  |
| `extension-tests`      | Build the native extension | 13 s  | 7 s   | 6 s   |
| `benchmark-ratchet`    | benchmarks and ratchet     | 59 s  | 54 s  | 5 s   |

_Table 3: What each family saves its reader on a warm run. The `typecheck-test`
step also runs the Python suite, so its wall time is dominated by pytest rather
than by compilation; `extension-tests` is the compile-only signal._

The estimate that justified the split was wrong by a factor of seven, which is
the reason this section exists. Six families were expected to cost about 1 GB
per generation against the single 287 MB archive they replaced. Measured, the
five Ubicloud families total about 101 MB, at 19.8 to 20.5 MB each, and the
instrumented coverage family is 286 MB on its own. The old archive was large
because it held one job's instrumented workspace objects, which is precisely
why it served nobody else.

Two comparisons follow from those numbers, and they have different bases. Say
which one you mean.

Against no split at all, the five Ubicloud families cost about 100 MB per
generation more than the single archive they replaced, and they save the whole
41 s in the table above, because every one of those jobs was getting nothing
from the old archive.

Against keeping only the 3.13 unoptimized family, which is the smallest split
that still serves `extension-tests`, the other four families cost about 81 MB
per generation and save 35 s: 11 s, 9 s and 10 s on the three `typecheck-test`
legs and 5 s on `benchmark-ratchet`. `extension-tests`' own 6 s is excluded
because it keeps its cache either way.

Both are worth it against a 30 GB weekly quota, so the six families stay. On
the numbers that were assumed rather than measured, neither would have been.

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

`ubicloud-standard-2` presents a 72 GB volume that is already 85 to 89 % full
while a job runs, leaving 8 to 11 GB free rather than the roughly 31 GB the
estate's earlier notes assumed. Disk, not memory, is what kills a job on that
shape, and it does so silently. Jobs that compile or run a suite therefore
bracket their work with `.github/actions/resource-sampler`, which records peak
memory, peak disk, and least-free disk to the log as well as to the step
summary. The log matters because the jobs API exposes it and does not expose
the summary.

`benchmark-ratchet` is the exception, and deliberately so. It times commands
that run for about half a second and compares the ratio of two of them, so a
background loop waking every 15 s to shell out to `free` and `df` would be
interference in the measurement rather than an observation of it. Its disk
figure was taken once instead[^3]: 62.1 GB used with 11.0 GB free, the same
envelope as every other Ubicloud job here.

The coverage jobs additionally discard the instrumented build tree once the
report is written, printing `df -h` either side. It has no later consumer, it
is the second tree on the volume, and removing it before any cache save keeps
it out of the archives and out of the measured high-water mark. The step
searches for the tree rather than naming it: `cargo llvm-cov` builds beside the
manifest it is given, and an earlier version named a repository-root path and
reclaimed nothing while reporting success[^3].

[^1]: Cuprum CI run
    [33853599399](https://github.com/leynos/cuprum/actions/runs/33853599399), a
    `workflow_dispatch` on `main` measuring warm caches, 2026-09-04.

[^2]: Cuprum CI run
    [33854076859](https://github.com/leynos/cuprum/actions/runs/33854076859),
    the second such dispatch over the same tree, 2026-09-04.

[^3]: Cuprum CI run
    [33857764655](https://github.com/leynos/cuprum/actions/runs/33857764655),
    the first run carrying the resource sampler, 2026-09-04.

[^4]: Cuprum CI run
    [33898839149](https://github.com/leynos/cuprum/actions/runs/33898839149),
    the merge push that first wrote all six families, 2026-09-04.

[^5]: Cuprum CI run
    [33900410222](https://github.com/leynos/cuprum/actions/runs/33900410222), a
    `workflow_dispatch` reading them back over an unchanged tree, 2026-09-04.
