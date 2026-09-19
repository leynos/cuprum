# Local validation of GitHub Actions with act and pytest (black-box)

This guide focuses on **pre-continuous integration (CI) smoke/integration
testing** of a workflow using `act` and `pytest`, treating the workflow as a
**black box**. The assertions target artefacts, workspace side effects, and
structured logs. Host-side command interception is intentionally avoided;
containers execute in isolation.

It is the background for the *supported* harness this repository ships. The
harness preserves the real `changes` job and the `benchmark-ratchet` admission
boundary from `.github/workflows/ci.yml`, while replacing unrelated
prerequisite jobs and the benchmark body with probes. To run that, or to read
the contract it enforces, start with
[ADR-013](adr-013-actions-runner-integration-harness.md) and:

```bash
make test-act   # runs the opt-in scenarios; refuses to skip in CI
make test       # does not run the scenarios; they need a container runtime
```

The hosted opt-in entry point is
`.github/workflows/benchmark-gate-harness.yml`. It runs weekly and on manual
dispatch, always on GitHub-hosted `ubuntu-latest`; `ci.yml` retains its general
manual dispatch for ordinary CI runs.

The rest of this document is the general recipe for validating *any* workflow
in this repository with `act`, and is the reason the supported harness is
shaped the way it is.

## TL;DR

- Keep **unit tests** in the action codebase (plain `pytest` or the language's
  runner).
- Integration-test the **workflow** locally via `act`, from a `pytest` harness.
- Assert on **artefacts**, **file outputs**, and **logs** (using `act --json`,
  which emits JavaScript Object Notation (JSON) events).
- Treat results as pre-CI confidence; certify on GitHub runners for
  permissions/OpenID Connect (OIDC) parity.

## Prerequisites

- A container runtime. Docker is the default; rootless Podman also works when
  its socket is running. The hosted harness uses Docker on `ubuntu-latest`.
- `act` 0.2.89, or the version pinned by the repository's checksum-verified
  install step.
- Python 3.10+ with `pytest`.
- The harness pins this immutable runner image to reduce drift:

  ```bash
  image='catthehacker/ubuntu:act-latest@sha256:c58e2b364da03b0c804c7d660f2ecbedf2f221a382b9baa0b344b0144780ff43'
  act pull_request -P "ubuntu-latest=${image}" --list
  ```

The hosted harness installs `act` 0.2.89 from the Linux x86_64 release archive
and verifies SHA-256
`0191d6f1f3b716b5c55820032605d05fc3c1cdbf581ebeff655019e5dd1524c0` before
running it. Maintainers update the CLI version, checksum, and image digest
together, then rerun the repository gates.

Verify both before debugging a scenario that will not start:

```bash
docker info --format '{{.ServerVersion}}'   # or: podman info
act --version
```

## Minimal layout

```plaintext
.github/workflows/selftest.yml
scripts/
  # optional helper scripts used by the workflow
tests/
  fixtures/events/pull_request-relevant.event.json
  integration/test_workflow_integration.py
```

### Example workflow (self-checking)

This job builds a tiny JSON artefact with environment/version data and uploads
it. This provides deterministic material to assert on from the host.

```yaml
# .github/workflows/selftest.yml
name: selftest
on:
  workflow_dispatch:
  pull_request:
jobs:
  selftest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build artefact
        run: |
          set -euo pipefail
          mkdir -p out
          python - <<'PY'
          import json, os, platform, sys
          print("Hello from workflow")
          data = {
            "status": "ok",
            "python": sys.version.split()[0],
            "os": platform.platform(),
            "env": {
              "CI": os.getenv("CI", ""),
              "GITHUB_REF": os.getenv("GITHUB_REF", ""),
            },
          }
          with open("out/result.json", "w") as f:
              f.write(json.dumps(data))
          PY
      - name: Upload artefact
        uses: actions/upload-artifact@v4
        with:
          name: result
          path: out/result.json
```

### Event payload fixtures

The supported fixtures under `tests/fixtures/` cover both `pull_request` and
`push` events, with relevant, irrelevant, mixed, and empty changed-path sets. A
detector-failure fixture exercises the `skip-detector-failed` path. Templates
contain only fields consumed by the workflow; the harness fills the repository,
local branch refs, and commit SHAs from the temporary Git history before
invoking `act`. This keeps the event payload and the checked-out history
consistent.

## Driving `act` from `pytest` (black-box harness)

`tests/helpers/act_harness.py` builds a temporary Git repository whose commits
represent the changed-path set. It copies the real workflow, projects the
`changes` job and the `benchmark-ratchet` dependency boundary, and replaces
unrelated prerequisite bodies with success probes and the benchmark body with
an admission marker. Running `benchmark-ratchet` therefore executes the real
`changes` job and its admission expression without running the paid benchmark.

The helper's `run_act` function defaults to `job="changes"`; integration tests
pass `job="benchmark-ratchet"` when they need to exercise the dependency graph.
It accepts a temporary repository, an event template, and an image, invokes
`act --job` with `--eventpath` and `--json`, and uses
`subprocess.run(..., check=False, capture_output=True, timeout=...)`. The
result exposes the exit code, parsed JSON lines, summary, and path-filter
output for assertions.

```python
from tests.helpers.act_harness import Event, IMAGE, run_act

event = Event(
    name="pull_request",
    payload={
        "action": "opened",
        "number": 1,
        "pull_request": {"base": {"ref": "main"}, "head": {"ref": "feature"}},
    },
    ref="refs/pull/1/merge",
    sha=head_sha,
    branch="feature",
)
run = run_act(repository, event, job="benchmark-ratchet", image=IMAGE)
assert run.output("bench") == "true"
assert run.output("decision") == "run"
assert run.output("benchmark_admitted") == "true"
```

Assertions use the summary, artefacts, and parsed JSON events. Raw terminal
output is diagnostic only. The helper empties `GITHUB_TOKEN`, which routes the
real `dorny/paths-filter` action to its local Git fallback rather than the
hosted REST API; action and image downloads can still require network access.

The persistent decision writer runs in every scenario. Its `record` output must
contain the same bounded labels as the gate outputs. The GitHub artefact upload
is skipped under `act`, so these tests do not claim to verify hosted storage;
validate that separately by downloading a CI run's decision log.

The harness does not intercept commands inside the container. If a workflow
needs deterministic command substitution, test that helper separately with the
repository's command-mocking tools; keep this suite focused on the workflow
boundary and its observable outputs.

## Traps this repository has hit

Each of these produces a *plausible wrong answer* rather than an error, which
is why they are listed here rather than left to be rediscovered.

- **Scope `act` to one workflow with `-W`.** `--job` matches a job name across
  every workflow file, so without `-W .github/workflows/<file>.yml` a job name
  that appears in two files runs both, and the failure names an unintended job.
- **Pass `-s GITHUB_TOKEN=` when the workflow uses `actions/checkout` and
  `dorny/paths-filter`. With an empty token, `paths-filter` takes its local
  `git diff` path and avoids the GitHub API. `act` may still need network
  access to download actions and the pinned image. With a token `act`
  fabricates, it calls the GitHub API and fails with `::error::Not Found` — a
  failure that looks like a broken detector rather than a credential-routing
  problem.
- **A path filter diffs the checked-out branch, not the event's head.** With
  an empty token the base resolves to `base || baseSha || defaultBranch` and is
  compared against `git branch --show-current`; `pull_request.base.sha` is not
  consulted. A scenario left on the default branch diffs that branch against a
  commit that already contains its own changes and reports "no relevant
  changes" — the same answer a genuinely irrelevant scenario produces. Put the
  temporary repository on a branch first.
- **A workflow file is often itself one of its own filter patterns.** If
  `.github/workflows/<file>.yml` matches the filter, it must be in the *base*
  commit, or every scenario is relevant for a reason unrelated to what it is
  testing.
- **Read outputs from the stream, and step summaries too.**
  `$GITHUB_STEP_SUMMARY` is truncated inside the container after upload, so
  recover it from the `⚙ Summary -` log message. `act` folds `$GITHUB_OUTPUT`
  writes into a single event carrying the final value; a name reaches the
  stream more than once only when a step also uses the legacy
  `::set-output name=X::Y` command, and there the last event's value is the
  live one.
- **Preserve the admission boundary when projecting the workflow.** The
  harness keeps all of `changes`, the `benchmark-ratchet` `needs` edge, and its
  `if` expression. Only unrelated prerequisite bodies and the benchmark body
  are replaced, so a passing marker proves admission without spending paid
  benchmark time.
- **Fill event templates from the temporary Git history.** Static refs and
  SHAs can disagree with the checked-out branch and make a scenario pass for
  the wrong reason. The harness writes the local refs and commit SHAs into each
  JSON event before invoking `act`.

## What to assert (beyond exit code)

- **Artefacts:** existence, schema, and specific fields; normalize line endings
  when CRLF matters.
- **Workspace side effects:** files created/modified when using `-b`.
- **Structured logs:** look for key lines (cache keys, matrix values, tool
  versions). Prefer `--json` and parse rather than grepping raw TTY output.
- **Idempotence:** run the same job twice and assert identical artefacts (or
  intentional cache hits).

## Useful `act` flags in this setup

- `--job changes` (the helper default), or `--job benchmark-ratchet` to run
  the projected dependency graph.
- `--eventpath <event.json>`: provide the generated event payload.
- `-P ubuntu-latest=<immutable-image>`: select the pinned image shown in
  [Prerequisites](#prerequisites).
- `--json`: emit a line-delimited JSON log stream suitable for parsing.
- `-s GITHUB_TOKEN=`: force the filter's local Git fallback and avoid hosted
  REST API calls.

## Known limitations (by design)

- **Runner parity:** `act` images are close, not identical, to `ubuntu-latest`.
- **Permissions/OIDC:** token scopes, OIDC federation, and GitHub-provided
  credentials cannot be faithfully validated locally; rely on GH runners.
- **Service containers & networking:** usually fine but can diverge under load
  or with subtle DNS/health-check timing.

## Validation ladder

1. **Local fast loop:** unit tests -> `act` black-box tests via `pytest`.
2. **Authoritative CI:** run the same workflow on GitHub-hosted runners.
3. **End-to-end (privileged paths):** GH-only with least-privilege tokens; gate
   behind labels/paths.

This arrangement provides tight feedback for workflow correctness and
orchestration logic, without pretending local containers are perfect stand-ins
for GitHub's environment.
