# Architectural decision record (ADR) 012: Actions-runner integration harness

## Status

Accepted on 2026-09-16. A supported pytest-driven `act` harness executes the
real `changes` job from `.github/workflows/ci.yml` and asserts what
`dorny/paths-filter` decided and what the gate did with that decision.
Delivered by issue #339.

## Date

2026-09-16.

## Context and problem statement

The `changes` job is the only thing standing between a pull request and the
metered `benchmark-ratchet` job. Its behaviour is currently covered by contract
tests that read the workflow source, and by behavioural tests that extract the
`run:` block of the decision step and execute it in isolation. Both are
valuable, and neither executes the job. They cannot observe whether
`dorny/paths-filter` actually produces the `bench` output the contract assumes,
whether an event payload reaches the gate the way a real event would, or
whether the gate expression admits the benchmark job for the paths it should.

The repository already documents local workflow debugging with `act` in
`docs/local-validation-of-github-actions-with-act-and-pytest.md`, but that
guidance is a manual recipe. There is no supported harness that a maintainer or
a test can invoke, and no fixture set that names the changed-path cases which
matter.

## Decision drivers

- The harness must exercise the workflow boundary, not a reimplementation of
  it. Asserting against a fake job would restate the contract test.
- It must be runnable without Docker Desktop, without network access, and
  without a GitHub credential, because it has to work on the machines that
  actually run the gates.
- It must not run on a metered runner, and it must not be able to corrupt the
  developer's checkout.
- It must skip cleanly, rather than fail, where no container runtime is
  available, so that `make test` stays portable.
- The cases it covers must be the cases that change the outcome: relevant,
  irrelevant, mixed, and empty changed-path sets, pull-request and
  non-pull-request events, and a detector failure.

## Requirements

### Functional requirements

- Run the real `changes` job from `ci.yml` and report its exit status, the
  `filter` step's `bench` output, and the recorded gate table.
- Cover relevant, irrelevant, mixed, and empty changed-path sets, a push event,
  and a detector failure.
- Skip with a stated reason when `act` or a container runtime is absent.

### Technical requirements

- Never bind the repository worktree into a container that runs `git checkout`.
- Pin the container image by tag, and resolve the runtime through an explicit
  socket so rootless Podman works without a Docker daemon.
- Recover the step summary from the `act` JSON log stream, because the
  in-container summary file is truncated after upload.
- Take the last value of a repeated `set-output`. `act` folds ordinary
  `$GITHUB_OUTPUT` writes and emits one event carrying the final value, so the
  real `changes` job produces each name exactly once. A name still reaches the
  stream more than once when a step also uses the legacy `::set-output::`
  command, and there the live value is the last event's — so the parser
  resolves repeats last-wins and a recorded fixture pins that behaviour.

## Options considered

### Option A: a pytest-driven `act` harness over the real workflow

A helper copies the workflow and an event fixture into a temporary git
repository, invokes `act` against that repository with `--json`, and parses the
stream into an `ActRun` value exposing the exit code, named outputs, failed
steps, and the recorded summary.

This runs the real job, works offline against rootless Podman, and returns
typed evidence a test can assert on. It costs a container runtime and a few
minutes per scenario.

### Option B: extend the existing source-reading contract tests

Assert more about the YAML text: the filter's patterns, the gate expression,
the outputs.

This is free and fast, but it cannot observe the boundary. It would have
noticed none of the traps recorded in the plan's `Surprises & discoveries`, and
it cannot fail when `act` and a real runner disagree.

### Option C: a repository-hosted self-hosted runner executing the workflows

Run the workflows against a dedicated runner the repository controls.

This is the most faithful environment and the least portable. It requires
infrastructure, cannot run in a pull-request gate for a fork, and is
disproportionate to a job that installs two actions and runs two shell scripts.

| Topic                   | Option A          | Option B | Option C      |
| ----------------------- | ----------------- | -------- | ------------- |
| Executes the real job   | yes               | no       | yes           |
| Runs offline            | yes               | yes      | no            |
| Needs infrastructure    | container runtime | none     | a runner host |
| Runnable in `make test` | yes, or skips     | yes      | no            |

_Table 1: Comparison of workflow verification options._

## Decision outcome / proposed direction

Option A. `tests/helpers/act_harness.py` provides a `run_act` helper that
prepares a temporary git repository, materializes the changed-path set as real
commits, and invokes `act` pinned to the `catthehacker/ubuntu:act-latest`
image. Event fixtures live under `tests/fixtures/`, and
`tests/integration/test_workflow_integration.py` asserts, per scenario, the
detector's `bench` output, the gate table's decision, and — on the
detector-failure path — that the decision was still recorded despite the
non-zero exit.

The harness passes `-s GITHUB_TOKEN=` so that `github.token` is empty and the
pinned `dorny/paths-filter` takes its local `git diff` path rather than calling
the GitHub API. It is exposed as a pytest module and a Makefile target; the
maintainer-facing evidence is the point, and a run on every pull request would
buy little for a boundary that changes rarely.

It is additionally run by an opt-in `workflow-harness` job in `ci.yml`, on a
dispatch or a weekly schedule and never on a pull request. That job exists
because the alternative is a harness nothing runs: the suite skips where no
container runtime is present, so without a job that sets `CUPRUM_REQUIRE_ACT=1`
a silent regression in the harness, its pinned image, or `act`'s own output
format would be indistinguishable from a passing suite. It runs on
`ubuntu-latest` and binds the Docker daemon the hosted image installs and
starts — deliberately not the rootless Podman a developer machine uses, because
on that image Podman's socket comes from a systemd _user_ unit and a runner has
no user session to create one.

## Goals and non-goals

- Goals:
  - Verify the changed-path inputs, the event types, the `dorny/paths-filter`
    output, and the benchmark-gate admission against the real job.
  - Be runnable on a developer machine with no network and no credential.
  - Fail loudly when the boundary changes, and skip loudly when it cannot run.
- Non-goals:
  - Emulating every Actions feature. The harness covers what the `changes` job
    uses and makes no claim beyond that.
  - Running on every pull request. The opt-in job runs the scenarios on a
    dispatch or a schedule, never as a pull-request gate.
  - Running `benchmark-ratchet`, which needs the metered runner's toolchain.
  - Verifying that the telemetry sink accepts the payload, which
    [ADR-011](adr-011-benchmark-gate-telemetry-sink.md) leaves to a documented
    manual read-back.

## Known risks and limitations

- `act` is a partial emulation. A scenario that passes here can still differ on
  a real runner, and the harness's value depends on pinning the image and
  re-running it when Actions semantics change.
- The harness depends on `act`'s JSON stream format, which is not a stable
  interface. The parsing is therefore repository-owned code with its own unit
  tests over recorded streams, including one that carries the same output name
  twice, so a format change fails visibly instead of silently returning the
  wrong output.
- Container images for non-amd64 hosts are not always available, so the pinned
  public image is the committed default and any divergence in the opt-in CI job
  is documented rather than assumed to be equivalent.

## Consequences

### Positive

- The workflow boundary the gate depends on is exercised, not inferred, and the
  traps that motivated the harness are encoded as tests.
- A maintainer can reproduce a gate decision locally with one command.
- The harness skips rather than fails where no runtime exists, so the suite
  stays green on machines that cannot run it.

### Negative

- `make test` gains a scenario set that is slower than the rest of the suite and
  that requires a container runtime to contribute.
- The repository owns a parser for a third-party tool's output format.

## Revision note, 2026-09-16: two findings from building the harness

Two things the harness taught us after this decision was first written. Both
are recorded here because they change how the harness must be used, not merely
how it is implemented.

**The detector diffs the checked-out branch, not the event's head.** With an
empty `github.token`, `dorny/paths-filter` falls back to
`git diff <base> <current-branch>`, where the current branch is whatever the
container has checked out. It does not consult `pull_request.base.sha`. A
pull-request scenario that leaves the repository on its default branch
therefore diffs that branch against a commit already containing every scenario
commit, observes no changes, and reports `bench=false` for a scenario that is
plainly relevant. `tests/helpers/act_harness.py` makes this explicit through
`branch()`, which every pull-request scenario must call first. The trap is
worth recording because it fails _quietly and plausibly_: the wrong answer is
the same `false` that a genuinely irrelevant scenario produces.

**The legacy `::set-output::` command is the only source of a repeated output.**
`act` folds `$GITHUB_OUTPUT` writes and emits a single event carrying the
final value, so the real `changes` job produces each name exactly once. The
technical requirement above originally justified last-wins resolution as a
property of the whole stream; the measurement says the requirement holds for
the legacy path specifically. The parser resolves repeats last-wins either way,
and `tests/fixtures/act_stream_repeated_output.jsonl` is a real recording that
pins it. A parser written on the original premise would still be correct; it
would simply have been correct for a reason that was never checked.

The harness also runs through Cuprum's own `SafeCmd` driver rather than
`subprocess`, so the repository's command runner is the thing exercising the
repository's workflow. That is a deliberate choice: it dogfoods the driver on a
real, long-running, container-boundary command, and it means a regression in
`run_sync`'s argument or environment handling surfaces here as well.
