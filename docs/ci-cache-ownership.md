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

| Family     | Holds                                     | Scoped by                                                           |
| ---------- | ----------------------------------------- | ------------------------------------------------------------------- |
| `cargo-`   | `~/.cargo/registry` and `~/.cargo/git`    | lane                                                                |
| `tool-`    | installed executables and uv environments | lane, image, interpreter                                            |
| `sccache-` | compiler output                           | lane, image, interpreter, build shape and selected compiler version |

_Table 1: The cache families, what each holds, and what scopes its key._

Every key also carries a generation, the operating system, and the architecture.
`target` trees appear in no family at all: sccache holds the objects a
`target` archive would preserve, and a `target` archive is invalidated by every
source change while the objects inside it are not.

### Boundary verification cache family

The Rust boundary workflow is a scoped member of the scheduled cache family. It
restores the shared Cargo registry through `.github/actions/cache-keys`; it
does not create a second registry owner. Linux native and Verus jobs use the
existing `setup-sccache` action with a 1 GiB cap and record compiler-cache
statistics. Verus compiler objects share that boundary-private sccache cache;
the Verus, Kani, Z3, and Miri binaries or sysroots are cached separately. No
job archives a Cargo `target` tree.

The native matrix uses the pinned `windows-2022` image alongside the supported
Linux and macOS images. The Kani and Miri jobs are scheduled or manually
dispatched rather than added to pull requests, run serially, and use no
ordinary target cache. Their proof, failure, and fault logs are uploaded as
artefacts with 14-day retention. Only scheduled runs on `refs/heads/main` save
boundary-private generations; pull requests and manual dispatches are read-only.

`tests/test_ci_rust_boundaries.py` checks the hosted matrix, time and fan-out
budgets, cache paths, and the absence of target archives. The runner manifest
registers `verus` and `extended` in `GITHUB_HOSTED_JOBS`, and the native matrix
has an explicit shared-registry test entry. These checks preserve the runner
placement and cache ownership contract when the workflow changes.

### Executables the tool family implies

The tool family's key hashes the actions that pin what it installs, so a job
may treat an exact hit as proof that a pinned executable is already present.
`makeutil` is not one of them: the jobs that run the Makefile contracts install
it with the shared `install-makeutil` action, which keeps its own cache keyed
on the pinned digest and re-verifies a restored binary. The tool cache
therefore does not carry makeutil, the install is never skipped on a hit, and a
writer of a tool family need not install it before saving. Each install step
sets `bin-dir` to a directory under `runner.temp`: the action's default,
`~/.local/bin`, is one of the paths the tool cache archives.
`tests/test_ci_makeutil_install.py` holds that arrangement, and refuses a local
copy of the action or a second route that fetches makeutil.

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
`abi3-py312` feature, so the built extension loads on every supported CPython
([ADR-016](adrs/adr-0016-stable-abi-native-wheels.md)). Compiler output is
still per-interpreter: the pyo3 build script records the building interpreter's
configuration and reruns when it changes, so switching `maturin build -i` from
3.13 to 3.12 recompiles `pyo3-ffi`, `pyo3`, and the extension. Objects built
under 3.12 are therefore not shown to serve a 3.14 build.

The build shape. An unoptimized `maturin develop` build, the same build with
`--release`, the dev-fast lint build (LLVM by default), the instrumented
`cargo llvm-cov` build, the `--cfg loom` model build, and the fragment-free
modern-stable build all produce different objects from identical source. The
modern-stable shape also carries its exact Rust version: its Rust/Cargo 1.99.1
objects are kept separate from the project-toolchain and dev-fast objects.

This was measured rather than assumed. Until 2026-09-04 the compiler key named
neither dimension, so every Ubicloud job read one archive that the instrumented
Python 3.13 coverage job had written. In two dispatches over an unchanged tree
[^1][^2], the one reader on the same interpreter took 14 of its 17 cacheable
compiles and the 3.12, 3.14, and 3.15a readers took none. Read and write errors
were zero throughout: the archive restored perfectly and simply held nothing
those jobs could use.

Collapse the per-interpreter split only after a CI measurement shows an archive
written under one interpreter serving compiles under another.
`test_pyo3_targets_the_stable_abi_but_keeps_per_interpreter_objects` records
the current evidence.

## One writer per family

A family is written by exactly one job. For every family except the Loom model
compiler family, that writer runs only on a push to `main`. Pull requests
restore and never save: a pull-request branch cannot publish a generation
anyone should trust, and the attempt produces only `Unable to reserve cache`
noise and wasted upload time. A `workflow_dispatch` run is likewise a reader,
which is what makes repeated dispatches a usable way to measure warm caches
without churning the generation they are measuring. The Loom model compiler
family is the exception: `loom.yml` saves it only during scheduled runs on
`refs/heads/main`. Its manual dispatches and pull-request smoke runs restore
the family without saving it.

| Family                                 | Writer                              |
| -------------------------------------- | ----------------------------------- |
| Cargo registry, Ubicloud               | `ci.yml` `extension-tests`          |
| Tools, 3.13                            | `ci.yml` `extension-tests`          |
| Tools, 3.12/3.14/3.15.2                | `ci.yml` `typecheck-test`, that leg |
| Compiler, 3.13 unoptimized             | `ci.yml` `extension-tests`          |
| Compiler, 3.12/3.14/3.15.2 unoptimized | `ci.yml` `typecheck-test`, that leg |
| Compiler, 3.13 release                 | `ci.yml` `benchmark-ratchet`        |
| Compiler, dev-fast lint                | `ci.yml` `lint-test`                |
| Compiler, 3.13 modern stable 1.99.1    | `ci.yml` `modern-stable`            |
| Compiler, Loom model                   | `loom.yml` `loom`                   |

`lint-test` moved to the Ubicloud lane with the fork fallback, taking its
dev-fast lint family with it, and stopped saving the Cargo registry: on its
owned arm it renders the same family `extension-tests` writes, and one writer
per family is the invariant. The Loom family stays on the GitHub-hosted lane,
because its writer is a scheduled job that stays there too.

The fork arm renders `runner.environment` as `github-hosted`, so a fork's pull
request reads a scope whose only writer is that scheduled Loom job and runs
cold for everything else. Every save is guarded on a push to `refs/heads/main`,
which a fork can never produce, so the fork arm restores and never publishes.

_Table 2: The single job that publishes each family under the writer trigger
described above._

The instrumented coverage family is absent from that table, and from the
repository. Both coverage lanes, `ci.yml`'s `coverage` and `coverage-main.yml`'s
`coverage-upload`, run sccache on the Actions cache service instead of on an
archived directory. On an Ubicloud runner that service is Ubicloud's own proxy
on the runner's private network, reached by calling
`export-ubicloud-cache-credentials` before `setup-sccache` and asking it for
`backend: gha`. Each run reads and writes that store directly, so there is no
generation for a writer to publish.

The store is branch scoped, as GitHub's own is. Ubicloud's cache branch
protection, on by default, lets a run read entries from its own branch and from
`main` only. `coverage-upload` on each push to `main` therefore warms every
pull request, and a pull request's own entries warm only its later pushes. The
design depends on that protection staying on, and a cold pull request is not a
reason to switch it off: this repository is public, and sharing entries across
branches would let objects compiled on any branch reach every other branch's
build, which is the cross-branch cache poisoning Ubicloud's documentation warns
about.

The archive those lanes replaced was the single worst one here. It was 286 MB,
larger than the five Ubicloud families put together, and it served exactly one
reader: `coverage` on a pull request, which could only match what the last push
to `main` had saved. Run 35664872714 measured what that was worth: 1 hit
against 562 misses, 0.18%, over roughly nine minutes.

The other Rust lanes keep the directory backend. `loom` runs GitHub-hosted,
where the Actions cache service really is GitHub's and sccache traffic would
compete with the Windows and macOS lanes for the per-repository quota. That was
the reason the whole repository chose a directory in the first place, measured
from the Ubicloud console on 2026-09-03, and for a GitHub-hosted lane it still
holds. The Ubicloud lanes other than coverage archive their directories through
the key families above; moving any of them to the proxy is a separate change.

A fork's or Dependabot's pull request runs `coverage` on the GitHub-hosted fork
arm, where there is no proxy and the credentials action fails closed. That arm
skips the credentials step and passes `backend: local`, so it compiles cold
into a directory nothing saves, as every fork arm already does. Both halves are
read by position from the same fork-or-Dependabot condition the runner uses,
and a contract asserts each.

The interpreter matrix has no 3.13 leg. The coverage job runs that
interpreter's suite and `extension-tests` runs its typechecker, so a leg would
only start a runner to repeat one of them. `extension-tests` therefore owns
both 3.13 families: the unoptimized compiler family, which it compiles, and the
3.13 tool family, which it saves on `main` after a miss. Every remaining matrix
leg compiles when it runs (the 3.15.2 leg is skipped on pull requests), and
compiler-cache publication happens on `main` pushes, so none of them can freeze
a rolling generation by republishing it unchanged.

The experimental leg's exact patch is part of its key, so its tool and compiler
families are distinct from any `3.15` minor family. When the leg is renamed to
another patch, the first `main` push that runs it starts cold on both: no
archive matches the new key, the run compiles and installs without a restore,
and that push publishes the first archive of the new families. The older
families age out through the rolling generation rather than being deleted.

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
instrumented coverage family was 286 MB on its own. The old archive was large
because it held one job's instrumented workspace objects, which is precisely
why it served nobody else, and why that family is now gone rather than split
further: the coverage lanes went to the Actions backend instead.

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

## Compiler-cache backend experiment

Issue [#609][issue-609] asks whether a non-coverage Rust family should move
from an archived directory to the Actions cache service, where the coverage
lanes already run. On Ubicloud that service is the runner's own proxy, so a
`main` push warms a trusted baseline, a pull request reads `main` and its own
branch, and a pull request's later pushes reuse what its earlier pushes wrote.
The alternative, the directory backend, has exactly one writer per family, a
`main` push, so a pull request's compiler objects never serve its next push.

This section records the experiment. Creating the issue neither ran it nor
established a saving, and no family moves without the gate below being met.

### Implementation baseline

The issue's source evidence was inspected at
`24c39650e388975a9b9abdc884cdf3e237745bb9` on 10 October 2026. This section was
written against `87de64c2cad4e970984760096faf278bd70439f3` on 22 November 2026,
which is also `origin/main` at that time. The differences that matter to the
inventory are:

- `lint-test` moved to the Ubicloud lane with the fork fallback, so its `lint`
  compiler family now shares the `self-hosted` lane with the other paid jobs.
- The experimental matrix leg names the exact patch `3.15.2` and is skipped on
  pull requests; it therefore has no pull-request consumer at all.
- The `modern-stable` route exists with its own `1.99.1` compiler family.
- Cranelift is disabled; the dev-fast routes use LLVM and the `mold` linker.
- MSRV is `1.87.0`, and Dependabot pull requests take the hosted arm.

### Families and their routes

`make typecheck` is `uv sync --group dev` followed by `ty check`. The project
uses `uv_build`, so that route builds no PyO3 extension. A `typecheck-test`
leg's compiler traffic therefore comes from selected tests that invoke Cargo
for real, principally `cuprum/unittests/test_doctest_warning_contract.py` and
the cargo-warning fixture probes, which build scratch crates. The effective
toolchain for that traffic is the dev-fast nightly `nightly-2026-08-23`, not the
`1.87.0` toolchain the job installs for the repository contract.

| Family                 | Job and leg             | Compile route                            | Producer (default branch) | Pull-request consumer |
| ---------------------- | ----------------------- | ---------------------------------------- | ------------------------- | --------------------- |
| `py3.12-debug`         | `typecheck-test` 3.12   | Scratch-crate contracts under dev-fast   | Same leg on `main` push   | Same leg              |
| `py3.14-debug`         | `typecheck-test` 3.14   | Scratch-crate contracts under dev-fast   | Same leg on `main` push   | Same leg              |
| `py3.15.2-debug`       | `typecheck-test` 3.15.2 | Scratch-crate contracts under dev-fast   | Same leg on `main` push   | None; leg is skipped  |
| `py3.13-debug`         | `extension-tests`       | `maturin develop` through dev-fast       | Same job on `main` push   | Same job              |
| `py3.13-lint`          | `lint-test`             | Rustdoc, Clippy, Whitaker, MSRV, Windows | Same job on `main` push   | Same job              |
| `py3.13-release`       | `benchmark-ratchet`     | Optimized benchmark build                | Same job on `main` push   | Same job              |
| `py3.13-modern-stable` | `modern-stable`         | Stable `1.99.1` validation build         | Same job on `main` push   | Same job              |
| `loom`                 | `loom` (scheduled)      | `--cfg loom` model build                 | Scheduled `main` run      | Smoke run only        |

_Table 4: The compiler families, where each is produced, and which pull request
may read it. The first four rows are the experiment's candidates, in the order
they are considered; `py3.13-release` and the proof families are out of scope._

The runner and lane are the same for every candidate: `ubicloud-standard-2` with
`runner.environment` rendering `self-hosted`, or `ubuntu-latest` on a fork or
Dependabot pull request, where the lane is `github-hosted` and no writer
exists. The fork arm consequently compiles cold, as every fork arm always has.

The boundary-verification workflow is not a reader of any family in Table 4.
`rust-boundaries.yml` calls the renderer for its Cargo registry key only and
restores its compiler cache under the boundary-private `boundary-*` keys
described under
[Boundary verification cache family](#boundary-verification-cache-family).
Those families share the GitHub-hosted lane and are out of scope here.

No other job restores a family from Table 4. The orphan-reader contract in
`tests/test_ci_cache_ownership.py` resolves every compiler-cache restore in a
cached job and joins it to the writer manifest, so a new reader of a family
with no writer fails rather than compiling cold unnoticed.

The Cargo registry and installed-tool families are unchanged by this
experiment. They are content-addressed and have their own writers, and a
candidate migration removes only the compiler-directory restore and save.

### The historical figures are historical

`docs/ci-cache-ownership.md` records a retired coverage archive of 286 MB, a
run with 1 hit against 562 misses, and warm-step savings of roughly 5 to 11
seconds. Every one of those numbers belongs to the archive backend as it stood
before the coverage lanes moved, and to the jobs that were measured then. They
are not this experiment's baseline, and coverage's poor old archive is not
evidence that any other family will benefit from the proxy.

The baseline for a candidate family is the per-job record produced by the
instrumentation described in
[CI compiler-cache evidence](ci-compiler-cache-evidence.md): restore and save
elapsed time and bytes, compile requests, hits, misses, non-cacheable reasons,
read and write errors, and whole-job occupancy.

### Adoption gate

The gate is committed before any candidate data is collected. A family is
adopted only when all of the following hold, measured over ordinary CI activity:

1. At least five comparable warm control/candidate pairs have been recorded.
2. The median whole-job saving is larger than the restore-plus-save time the
   migration removes, and is at least 5 % of the median control occupancy.
3. The incremental paid cost across the repository's observed
   pull-request/default-branch workload, including cold starts, is
   non-positive. Runner-seconds are labelled a proxy unless verified billing
   rounding is attached.
4. Cache isolation is unchanged: branch protection stays enabled, and no
   pull-request entry reaches `main`, a sibling pull request, or a release.
5. Test fidelity is unchanged: node IDs, parametrizations, required outcomes,
   expected skips, native-required execution, property budgets, compile-fail
   diagnostics, warning enforcement, and coverage settings all match the
   control.

A family that fails or cannot satisfy the gate keeps archived caching. A higher
hit rate alone does not satisfy it.

### Adopt/reject record

The gate is evaluated per family from ordinary CI activity. No family is
adopted by the change that introduced this record.

| Family           | Decision       | Owner  | Review date | Evidence | Rollback                       |
| ---------------- | -------------- | ------ | ----------- | -------- | ------------------------------ |
| `py3.12-debug`   | Not adopted    | leynos | 22 Nov 2026 | None yet | None; the archive is unchanged |
| `py3.14-debug`   | Not adopted    | leynos | 22 Nov 2026 | None yet | None; the archive is unchanged |
| `py3.15.2-debug` | Not adopted    | leynos | 22 Nov 2026 | None yet | None; the archive is unchanged |
| `py3.13-debug`   | Not adopted    | leynos | 22 Nov 2026 | None yet | None; the archive is unchanged |
| `py3.13-lint`    | Inventory only | leynos | 22 Nov 2026 | None yet | None; the archive is unchanged |

_Table 5: The experiment's decisions at this revision. "None yet" is a
statement about this change, not about the repository's history: the
instrumentation that produces the evidence lands with this record, so no
comparable control/candidate pair exists at the review date._

Reasons, per family:

- The matrix legs and `extension-tests` are not adopted because the issue
  forbids synthetic pushes and extra dispatches, and the gate requires at least
  five comparable warm control/candidate pairs from ordinary activity. Those
  pairs can only accumulate after the instrumentation has run on `main` and on
  ordinary pull requests. Adopting first would invert the experiment.
- `lint-test` is not a candidate yet. Its build shape spans four toolchains and
  a Windows cross-target check, so its inventory has to be read before a gate
  decision is meaningful.
- `benchmark-ratchet`, the scheduled proof and model lanes, Windows, and macOS
  remain out of scope.

Rollback, for a family that is later adopted: revert the single trial commit
that swapped the archived restore and save for the credentials export and
`backend: gha`. The next writing run on `main` then publishes the first archive
of the restored local family; one cold generation is accepted, and no data is
migrated. No cache is purged.

Refresh trigger: re-derive the baseline after any material change to the
toolchain, the PyO3 or Cargo build shape, the sccache release, the runner
image, or the cache backend, and after any change that alters which jobs
compile.

Branch protection and storage terms: Ubicloud documents 30 GB of free cache
storage per repository, a seven-day eviction of entries not accessed in that
window, and branch protection on by default, under which a run reads the
current branch or the default branch. This repository's coverage lanes already
depend on that default. A dated, redacted console observation for the
experiment period is not available from the runner and is therefore recorded as
`unknown`; it must be attached before any family is adopted. Disabling branch
protection is never an option.

### Storage and protection terms

Ubicloud's cache store is documented at 30 GB of free storage per repository,
with the oldest entries removed when the limit is exceeded and entries not
accessed for seven days removed automatically. Branch protection is on by
default. This repository's coverage lanes already depend on that protection.
Direct console observations for the experiment period, including storage,
transfer, eviction, and verified billing rounding, must be attached to the
decision record; where they are missing the field is `unknown`, never zero.

[issue-609]: https://github.com/leynos/cuprum/issues/609

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
