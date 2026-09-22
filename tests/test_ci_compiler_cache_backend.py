"""Contract tests for which store each Rust lane's compiler cache binds.

sccache binds its backend once, when its server starts, and then reports a
plausible hit rate whatever it bound. Both ways of getting this wrong are
silent in a green run.

On the Actions backend, an Ubicloud runner advertises `ACTIONS_CACHE_URL` as a
proxy on its own private network, and advertises it to action steps alone. A
server started before those values are republished through `GITHUB_ENV` falls
back to local disk, caches everything to a directory that dies with the runner,
and reports hits for the second half of its own compilation.

On the local-directory backend, a lane that sets `SCCACHE_DIR` without a job
somewhere that saves that directory reads an empty cache on every run. The
`coverage` lane did exactly that: run 35664872714 took 1 hit against 562 misses,
0.18%, because the only writer of its family was a push to `main` whose archive
its rendered key could not match.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.ci_runners import (
    CACHE_RESTORE,
    CREDENTIALS_ACTION,
    CREDENTIALS_STEP,
    GHA_BACKEND_JOBS,
    SCCACHE_ACTION,
    SCCACHE_ACTION_FILE,
    SCCACHE_JOBS,
    UBICLOUD_JOBS,
    expand,
    step_inputs,
    steps,
)

if typ.TYPE_CHECKING:  # pragma: no cover - typing only
    import collections.abc as cabc

    from tests.helpers.workflow_types import Step

#: Every Rust lane that is not on the Actions backend, and therefore owns a
#: cache directory. Derived rather than listed, so a lane cannot be moved
#: between the two arms without one of these rules noticing.
LOCAL_BACKEND_JOBS: typ.Final = tuple(
    pair for pair in SCCACHE_JOBS if pair not in GHA_BACKEND_JOBS
)


def _setup_step(workflow_name: str, job_name: str) -> Step:
    """Return the one step that installs and configures sccache in a job."""
    matches = [
        step
        for step in steps(workflow_name, job_name)
        if step.get("uses") == SCCACHE_ACTION
    ]
    assert len(matches) == 1, (
        f"{workflow_name}:{job_name} must set up sccache exactly once, "
        f"found {len(matches)}"
    )
    return matches[0]


def _declared_backend(workflow_name: str, job_name: str) -> str:
    """Return the backend a job asks `setup-sccache` for.

    Returns
    -------
    str
        The `backend` input the job passes, or `local` when it passes none,
        which is the action's default. For example, `ci.yml`'s `coverage` job
        returns `gha` and `lint-test` returns `local`.
    """
    step = _setup_step(workflow_name, job_name)
    declared = step.get("with")
    if not isinstance(declared, dict):
        return "local"
    return str(declared.get("backend", "local"))


def _declared_path(step: Step) -> str:
    """Return the `path` input a step declares, or an empty string.

    Returns
    -------
    str
        The newline-delimited `path` value, or `""` for a step that declares
        no `with` mapping or no path within it. Written rather than reusing
        `cache_paths`, because that helper fails a step with no paths, and
        here a step with none is the ordinary case being looked past.
    """
    declared = step.get("with")
    if not isinstance(declared, dict):
        return ""
    return str(declared.get("path", ""))


def _step_index(job_steps: cabc.Sequence[Step], name: str) -> int:
    """Return the position of the uniquely named step, or fail saying so."""
    matches = [
        index
        for index, step in enumerate(job_steps)
        if str(step.get("name", "")) == name
    ]
    assert len(matches) == 1, (
        f"expected exactly one {name!r} step, found {len(matches)}"
    )
    return matches[0]


@pytest.mark.parametrize(("workflow_name", "job_name"), GHA_BACKEND_JOBS)
def test_the_actions_backend_lanes_ask_for_it_explicitly(
    workflow_name: str, job_name: str
) -> None:
    """The manifest and the workflow must agree on which store a lane binds.

    Without this the manifest could name a lane the workflow still points at a
    directory, and every rule below would be asserted against the wrong arm.
    """
    assert _declared_backend(workflow_name, job_name) == "gha", (
        f"{workflow_name}:{job_name} is listed as an Actions-backend lane but "
        "does not pass backend: gha to setup-sccache"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), LOCAL_BACKEND_JOBS)
def test_every_other_rust_lane_keeps_the_directory_backend(
    workflow_name: str, job_name: str
) -> None:
    """The narrow half of the rule above.

    Without it, the manifest could be emptied and every lane silently moved to
    the Actions backend while the Actions-backend rules went unasserted.
    """
    assert _declared_backend(workflow_name, job_name) == "local", (
        f"{workflow_name}:{job_name} binds the Actions cache service without "
        "being listed as one of the lanes that may"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), GHA_BACKEND_JOBS)
def test_the_credentials_are_exported_before_sccache_is_set_up(
    workflow_name: str, job_name: str
) -> None:
    """A server started before the export binds local disk for the whole job."""
    job_steps = steps(workflow_name, job_name)
    credentials_index = _step_index(job_steps, CREDENTIALS_STEP)
    setup_index = job_steps.index(_setup_step(workflow_name, job_name))
    assert credentials_index < setup_index, (
        f"{workflow_name}:{job_name} must export the proxy credentials before "
        "setting up sccache, because sccache binds its backend when its "
        "server starts"
    )
    for name in ("Reset compiler-cache counters", "Install code", "Generate coverage"):
        later = [
            index
            for index, step in enumerate(job_steps)
            if str(step.get("name", "")) == name
        ]
        for index in later:
            assert credentials_index < index, (
                f"{workflow_name}:{job_name}: {CREDENTIALS_STEP!r} must "
                f"precede {name!r}"
            )


@pytest.mark.parametrize(("workflow_name", "job_name"), GHA_BACKEND_JOBS)
def test_the_credentials_step_runs_the_action_that_exports_them(
    workflow_name: str, job_name: str
) -> None:
    """A step carrying the name but not the action satisfies order and nothing else."""
    job_steps = steps(workflow_name, job_name)
    step = job_steps[_step_index(job_steps, CREDENTIALS_STEP)]
    uses = str(step.get("uses", ""))
    assert uses.startswith(f"{CREDENTIALS_ACTION}@"), (
        f"{workflow_name}:{job_name}: {CREDENTIALS_STEP!r} must run "
        f"{CREDENTIALS_ACTION}, not {uses!r}"
    )
    _, _, ref = uses.partition("@")
    assert len(ref) == 40, (
        f"{workflow_name}:{job_name}: {CREDENTIALS_STEP!r} must pin a full "
        f"40-character commit SHA, not {ref!r}"
    )
    assert all(char in "0123456789abcdef" for char in ref), (
        f"{workflow_name}:{job_name}: {CREDENTIALS_STEP!r} must pin a commit "
        f"SHA rather than a tag or branch, but {ref!r} is not hexadecimal"
    )


def test_no_github_hosted_lane_exports_ubicloud_credentials() -> None:
    """The action fails closed off Ubicloud, and it would be lying if it did not.

    Republishing a GitHub-hosted runner's cache endpoint under Ubicloud's name
    would point sccache at GitHub's quota while the workflow claimed otherwise.
    """
    ubicloud = set(expand(UBICLOUD_JOBS))
    for workflow_name, job_name in SCCACHE_JOBS:
        if (workflow_name, job_name) in ubicloud:
            continue
        names = [str(step.get("name", "")) for step in steps(workflow_name, job_name)]
        assert CREDENTIALS_STEP not in names, (
            f"{workflow_name}:{job_name} is not on an Ubicloud runner, so it "
            "has no cache proxy to export"
        )


def test_the_action_exports_one_backend_and_only_one() -> None:
    """`SCCACHE_DIR` and `SCCACHE_GHA_ENABLED` must never both be exported.

    sccache prefers whichever it finds configured first and reports neither, so
    a job with both configured would show a hit rate for a store nobody owns.
    The action's two arms are therefore mutually exclusive branches of one
    conditional rather than two independent exports.
    """
    source = SCCACHE_ACTION_FILE.read_text(encoding="utf-8")
    assert 'if [ "${SCCACHE_BACKEND}" = gha ]; then' in source, (
        "the two backends must be branches of one conditional"
    )
    gha_arm, _, local_arm = source.partition("\n        else\n")
    _, _, gha_arm = gha_arm.partition('if [ "${SCCACHE_BACKEND}" = gha ]; then')
    assert "SCCACHE_GHA_ENABLED=true" in gha_arm, (
        "the gha arm must enable the Actions backend"
    )
    assert "SCCACHE_DIR=" not in gha_arm, (
        "the gha arm must not also point sccache at a directory"
    )
    assert "SCCACHE_DIR=" in local_arm, "the local arm must export the directory"
    assert "SCCACHE_CACHE_SIZE=" in local_arm, (
        "the local arm must cap the directory it exports; the archive grows "
        "with every new compilation unit until it is trimmed"
    )
    assert "SCCACHE_GHA_ENABLED" not in local_arm, (
        "the local arm must not also enable the Actions backend"
    )


def test_the_action_refuses_the_actions_backend_without_credentials() -> None:
    """Fail loudly rather than cache a whole build to a directory nothing reads.

    This is the failure mode that has no symptom: the job stays green, the hit
    rate looks plausible on its own second half, and the store is gone when the
    runner is.
    """
    source = SCCACHE_ACTION_FILE.read_text(encoding="utf-8")
    for variable in ("ACTIONS_CACHE_URL", "ACTIONS_RUNTIME_TOKEN"):
        assert f'[ -z "${{{variable}:-}}" ]' in source, (
            f"the gha arm must refuse to proceed with an empty {variable}"
        )
    assert "exit 1" in source, "the credential check must fail the step"


def test_the_setup_action_declares_a_backend_input() -> None:
    """A caller must be able to name the store without editing the action."""
    source = SCCACHE_ACTION_FILE.read_text(encoding="utf-8")
    assert "\n  backend:\n" in source, "setup-sccache must expose the store as an input"
    assert "    default: local" in source, (
        "the default must be the directory backend, so a GitHub-hosted caller "
        "that names no backend cannot silently reach for GitHub's quota"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), GHA_BACKEND_JOBS)
def test_an_actions_backend_lane_archives_no_cache_directory(
    workflow_name: str, job_name: str
) -> None:
    """An archive nothing reads is paid upload time on every run."""
    for step in steps(workflow_name, job_name):
        assert "~/.cache/sccache" not in _declared_path(step), (
            f"{workflow_name}:{job_name} binds the Actions cache service but "
            f"still archives ~/.cache/sccache in {step.get('name')!r}"
        )


@pytest.mark.parametrize(("workflow_name", "job_name"), LOCAL_BACKEND_JOBS)
def test_a_directory_backend_lane_restores_the_directory_it_writes(
    workflow_name: str, job_name: str
) -> None:
    """`SCCACHE_DIR` without a restore is an empty cache on every run.

    Restore steps only. A job that saves the directory and never restores it
    writes a generation it cannot read, which is the shape this rule first
    failed to catch: matching any step that named the path let a lane's own
    save satisfy the rule after its restore had been deleted.

    The save side is held by `test_each_cache_family_has_exactly_one_writer`:
    every rendered family in the manifest names the one job that publishes it,
    so a restore whose family has no writer fails there.
    """
    restores = [
        step
        for step in steps(workflow_name, job_name)
        if step.get("uses") == CACHE_RESTORE
        and "~/.cache/sccache" in _declared_path(step)
    ]
    assert restores, (
        f"{workflow_name}:{job_name} points sccache at ~/.cache/sccache but no "
        "step restores it, so the cache is empty on every run"
    )


def test_the_shared_rust_setup_still_owns_no_compiler_cache() -> None:
    """Two arms configuring one sccache is the failure being guarded against.

    The shared action's own sccache arm would run
    `mozilla-actions/sccache-action`, whose last act rewrites the cache-service
    selection this repository's credentials step published.
    """
    for workflow_name, job_name in GHA_BACKEND_JOBS:
        for step in steps(workflow_name, job_name):
            uses = str(step.get("uses", ""))
            if "shared-actions/.github/actions/setup-rust@" not in uses:
                continue
            inputs = step_inputs(step, f"{workflow_name}:{job_name} setup-rust inputs")
            assert inputs.get("use-sccache") == "false", (
                f"{workflow_name}:{job_name} must keep the shared action's "
                "compiler cache disabled, or its sccache steps will overwrite "
                "the proxy selection"
            )
