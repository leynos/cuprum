# Architectural decision record (ADR) 012: Actions-runner integration harness

______________________________________________________________________

## Status

Accepted. This ADR records the supported local compatibility harness for the
benchmark gate. The opt-in CI entry point is owned by
`.github/workflows/benchmark-gate-harness.yml`; its provisioning and hosted
execution are separate from the local harness and are not evidence of exact
GitHub Actions parity.

## Date

2026-09-17

______________________________________________________________________

## Context and problem statement

Static workflow parsing and shell-fragment execution cannot validate the hosted
Actions runtime boundary. They do not show whether the real
`dorny/paths-filter` action produces the `bench` output that the gate consumes,
whether event fields are delivered as expected, or whether the
`benchmark-ratchet` admission expression reaches the intended result.

The repository needs a repeatable boundary test that can run without a GitHub
credential. It must exercise the relevant changed-path cases and detector
failure while keeping the paid benchmark job out of the test environment.

## Decision

Use a pytest-driven `act` harness as a black-box compatibility test. The
harness builds a temporary repository and preserves the complete `changes` job
plus the `needs` and `if` boundary of `benchmark-ratchet`. It replaces
unrelated prerequisite job bodies with success probes and replaces the
benchmark body with an admission marker. Tests invoke
`act --job benchmark-ratchet`, which executes the dependency graph and
therefore tests `changes` and the real admission condition together.

`tests/helpers/act_harness.py` exposes `run_act` with `changes` as its default
job. It invokes `subprocess.run` with `check=False`, captured output, and a
bounded timeout. A test that wants the full graph passes
`job="benchmark-ratchet"`. The helper supplies `--eventpath`, `--json`, and a
single pinned `ubuntu-latest` image:

`catthehacker/ubuntu:act-latest@sha256:c58e2b364da03b0c804c7d660f2ecbedf2f221a382b9baa0b344b0144780ff43`.

The opt-in workflow runs only on a GitHub-hosted `ubuntu-latest` runner. It
owns a weekly schedule and a `workflow_dispatch` trigger in
`.github/workflows/benchmark-gate-harness.yml`; the general manual dispatch of
`ci.yml` remains owned by CI. The workflow pins `act` 0.2.89 by the existing
archive checksum. Maintainers update the CLI version, checksum, and image
digest together, then rerun the repository gates.

Fixtures cover pull-request and push events, each with relevant, irrelevant,
mixed, and empty changed-path sets, plus detector-failure scenarios. Event
templates are completed with the temporary repository's actual local refs and
commit SHAs, so the action evaluates a real history rather than fixture
placeholders. With an empty `GITHUB_TOKEN`, the pinned filter uses its local
Git fallback instead of the hosted REST API; action and image downloads can
still require network access.

## Consequences

- The detector, event delivery, output propagation, and benchmark admission are
  tested together through the workflow boundary.
- The harness needs `act`, Docker or another compatible container runtime, and
  the pinned runner image. Tests skip with a stated reason when those tools are
  unavailable; the opt-in hosted workflow turns an unavailable runtime into a
  failed check so it cannot pass by running no scenarios.
- The dedicated workflow consumes GitHub-hosted runner time only. It never
  invokes the paid `benchmark-ratchet` implementation because the harness
  replaces that body with an admission marker.
- The image is intentionally different from the hosted `ubuntu-latest`
  environment, and `act` implements only part of the Actions runtime. A green
  result is a compatibility signal and cannot prove identical hosted behaviour,
  permissions, credentials, or OpenID Connect (OIDC) semantics.
- The JSON stream and artefact parsing are repository-owned compatibility code.
  A change in `act` output or the pinned image must be reviewed with the
  scenario results before upgrading the pins.
