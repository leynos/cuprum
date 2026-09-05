# Debugging Plan: benchmark runner cannot fetch df12-python-lints

**Generated**: 2026-09-03T00:03:05Z **Issue ID**: PR #289, CI run 33678861823
**Severity**: High **Falsification sub-agent**: alchemist **Planning agent
boundary**: This document was prepared by the planning agent. Falsification
must be executed by the named sub-agent, not by the planning agent.

## Problem Statement

The `benchmark-ratchet` job fails while its baseline-fetch step creates the
project environment. `uv run` tries to fetch the public
`leynos/df12-python-lints` dependency at the commit resolved from `v0.3.0`, but
Git reports that it cannot read a GitHub username with terminal prompts
disabled. The job should fetch the baseline and run the benchmark. All other
jobs in the same workflow pass, so this failure alone consumes paid-runner time
without producing a comparison.

## Context Summary

| Aspect              | Details                                                            |
| ------------------- | ------------------------------------------------------------------ |
| First observed      | Run 33646570001 at commit `7e56bef1`, before the tag change        |
| Reproduction rate   | Both post-rebase benchmark runs failed in the same step            |
| Affected components | Ubicloud `benchmark-ratchet` job, `uv`, and the Git dependency     |
| Recent changes      | Rebase onto `origin/main`; later replace the SHA ref with `v0.3.0` |

### Error Artefacts

```plaintext
Failed to download and build `df12-python-lints`
failed to fetch commit `4cf41736cce2f7ba2778882a5c629c044568a0e5`
fatal: could not read Username for 'https://github.com': terminal prompts disabled
```

The failed SHA-based and tag-based runs have the same error. GitHub reports
`leynos/df12-python-lints` as public, and `refs/tags/v0.3.0` advertises the
resolved commit `4cf41736cce2f7ba2778882a5c629c044568a0e5`.

### Information Gaps

- The failing runner's effective Git configuration was not printed.
- The runner image and checkout credential state may have changed between the
  successful 2026-09-01 run and the failures on 2026-09-02.
- The workflow does not distinguish an anonymous fetch failure from a fetch
  carrying checkout's repository-scoped authorization header.

### Falsification Results

- **H1 not falsified.** With fresh isolated environments, the default `uv run`
  installed all 54 packages and included `df12-python-lints`. Adding `--no-dev`
  installed only Cuprum, started the baseline client's help path, and exited
  successfully.
- **H2 falsified.** A hosted run with `persist-credentials: false` showed
  checkout removing `http.https://github.com/.extraheader` before the failing
  fetch. The baseline step passed, but the later dev-group sync received the
  same authentication-style error.
- **H3 falsified.** The public `refs/tags/v0.3.0` advertises the exact commit in
  `uv.lock`, and the same failure occurred before the configuration switched
  from that commit SHA to the tag.
- **H4 inconclusive.** GitHub rejected `gh run rerun --failed` with
  `Resource not accessible by integration`, so no unchanged hosted retry was
  available.
- **H5 not falsified.** A fresh dev-group sync with
  `--no-install-package df12-python-lints` installed the other 53 packages,
  including Maturin 1.13.3, without fetching or installing the lint plugin.
- **H6 falsified.** Passing `--no-sync` through both `uv run` commands stopped
  the outer resync, but Maturin's editable installation performed its own
  dependency installation and fetched the plugin.
- **H7 not falsified.** With `--skip-install`, the fresh release build completed
  without fetching or installing df12, and the resulting environment imported
  `cuprum._rust_backend_native`.

______________________________________________________________________

## Hypotheses

### H1: Baseline fetching needlessly resolves the development dependency group

**Claim**: `uv run python benchmarks/fetch_main_benchmark_baseline.py` performs
the default development-group sync, so an unrelated lint-only Git dependency
can prevent the standard-library-only baseline client from starting.

**Plausibility**: High — the failure occurs while `uv run` creates `.venv`, and
its own diagnostic says `cuprum:dev` brought in `df12-python-lints`.

**Prediction**: If this hypothesis holds, an isolated `uv run --no-dev` of the
client excludes `df12-python-lints` while the default invocation includes it.

#### H1 Falsification Plan

| Step | Action                                                                                                | Expected Negative Result                              |
| ---- | ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| 1    | Compare isolated, frozen `uv run` resolution with and without `--no-dev` using fresh temporary caches | `df12-python-lints` is still resolved by `--no-dev`   |
| 2    | Run the baseline client's `--help` path with `--no-dev`                                               | The client cannot start without the development group |

**Tooling**: `uv`, fresh directories under `/tmp`, and the checked-in lockfile.

**Confidence on falsification**: Decisive for the baseline step's dependency
boundary, but not for the later benchmark build, which deliberately uses the
development group.

______________________________________________________________________

### H2: Checkout credentials poison the sibling public-repository fetch

**Claim**: The failing runner's Git configuration supplies the current
repository's authorization header to the sibling repository. GitHub rejects
that repository-scoped credential instead of serving the public dependency
anonymously.

**Plausibility**: Medium — the error is an authentication prompt rather than an
unknown ref, and the same public commit fetched successfully on an earlier run.

**Prediction**: If this hypothesis holds, the effective Git configuration
contains an authorization header or credential rewrite, and clearing it makes
an otherwise identical fresh fetch succeed.

#### H2 Falsification Plan

| Step | Action                                                                                  | Expected Negative Result                                           |
| ---- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| 1    | Capture redacted effective Git credential and URL configuration on the benchmark runner | No credential helper, authorization header, or URL rewrite applies |
| 2    | Fetch `refs/tags/v0.3.0` and its commit with those entries disabled                     | The anonymous fetch fails identically                              |

**Tooling**: A temporary diagnostic workflow step, `git config --show-origin`,
and redaction that never prints credential values.

**Confidence on falsification**: High if the anonymous fetch succeeds or no
credential configuration applies.

______________________________________________________________________

### H3: The tag or resolved commit is unavailable

**Claim**: `v0.3.0` does not advertise the lockfile's resolved commit to an
anonymous Git client.

**Plausibility**: Low — the SHA-based run failed first, the repository is
public, and `git ls-remote` currently maps the tag to the exact lockfile commit.

**Prediction**: If this hypothesis holds, an anonymous `ls-remote` or fresh
fetch cannot observe the tag-to-commit mapping.

#### H3 Falsification Plan

| Step | Action                                              | Expected Negative Result                                      |
| ---- | --------------------------------------------------- | ------------------------------------------------------------- |
| 1    | Query the public tag without GitHub CLI credentials | The tag advertises `4cf41736cce2f7ba2778882a5c629c044568a0e5` |

**Tooling**: `git ls-remote` with credential helpers and authorization headers
disabled.

**Confidence on falsification**: Decisive. An advertised, fetchable mapping
rules out a missing or moved tag.

______________________________________________________________________

### H4: The anonymous GitHub failure is transient

**Claim**: The Ubicloud runner received a transient authentication-style error
from GitHub while fetching the otherwise public dependency.

**Prediction**: Rerunning the unchanged failed job succeeds. An identical
second failure falsifies this hypothesis.

**Falsification test**: Run `gh run rerun --failed`, then observe the unchanged
job with `gh run watch --exit-status`.

**Result**: Inconclusive because the GitHub integration cannot rerun jobs.

______________________________________________________________________

### H5: The benchmark does not need the lint-only Git dependency

**Claim**: `df12-python-lints` is incidental to the benchmark's use of the
development group. Excluding that package leaves the extension toolchain
available while removing the failing Git fetch.

**Prediction**: A fresh dev sync with `--no-install-package df12-python-lints`
neither fetches nor installs the plugin and still installs Maturin.

**Falsification test**: Sync the dev group into fresh cache, tool, and virtual
environment directories with that exclusion, then query Maturin directly.

**Result**: Not falsified. The sync installed 53 packages without fetching the
plugin, and Maturin 1.13.3 remained available.

______________________________________________________________________

### H6: The shared develop target must preserve the selective sync

**Claim**: Passing `--no-sync` through `make develop` prevents its `ensurepip`
and Maturin invocations from undoing the selective dependency sync.

**Prediction**: A fresh `make develop` with both the df12 exclusion and
`--no-sync` builds the extension without fetching the plugin.

**Falsification test**: Run that target with fresh uv directories and query the
built extension from the resulting environment.

**Result**: Falsified. The target built and the extension imported, but
Maturin's editable-install path still fetched and installed the plugin.

______________________________________________________________________

### H7: An in-place build avoids Maturin's dependency installation

**Claim**: Maturin's `--skip-install` mode builds Cuprum's mixed-project native
module in place without running the editable installation that resolves df12.

**Prediction**: A fresh release build with `--skip-install`, the selective
sync, and the outer `--no-sync` flags imports the native module without any
df12 fetch.

**Falsification test**: Run the exact `make develop` invocation in a fresh uv
environment, inspect the log for df12, and import `cuprum._rust_backend_native`.

**Result**: Not falsified. The fresh release build completed without any df12
fetch or install, and the native module imported successfully.

______________________________________________________________________

## Recommended Execution Order

1. **H1** — cheapest local test and directly isolates the failing baseline
   command's dependency boundary.
2. **H3** — cheap anonymous Git test that eliminates tag availability.
3. **H2** — inspect the hosted checkout credential state.
4. **H4** — retry unchanged to distinguish a transient failure.
5. **H5** — isolate the benchmark from the lint-only Git dependency.
6. **H6** — preserve the selective sync within the shared develop target.
7. **H7** — build the mixed-project extension in place without installation.

## Termination Criteria

- **Root cause identified**: H1 and H5 explain why the unrelated dependency was
  installed by both baseline discovery and the later `make develop` sync. The
  correction must exclude it at both boundaries without removing Maturin.
- **Escalation trigger**: H1 and H3 are falsified, and a redacted H2 diagnostic
  shows no applicable Git credential state.

## Notes for Executing Agent

H1 and H5 were not falsified. H2 and H3 were falsified. H4 was inconclusive
because the GitHub integration could not rerun the failed job. The hosted H5
run exposed the internal `uv run` resync. H6 then exposed Maturin's own
editable-install sync. H7 avoided both paths and preserved the native module;
validate it in the next hosted workflow run.
