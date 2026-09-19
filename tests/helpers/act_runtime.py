"""Decide whether this machine can run `act`, and invoke programs on it.

Everything here is about the *host*: whether `act` and a container runtime are
present, which socket `act` should talk to, and how to run a program through
Cuprum's own driver. `tests/helpers/act_harness.py` is the other half — it
describes what a scenario *is* (an event, a changed-path set, a staged
repository) and never asks whether the host can run one.

The separation matters for one behaviour in particular: the probe is an
environment question with an environment answer, and it has to be answerable
without constructing a scenario. It is also the reason the skip reasons the
report shows are produced by one piece of code rather than by each test.
"""

from __future__ import annotations

import os
import pathlib as pth
import shutil

from cuprum import ProgramCatalogue
from cuprum.sh import ExecutionContext, SafeCmd

__all__ = (
    "DOCKER_HOST_ENV",
    "REQUIRE_ACT_ENV",
    "SKIP_REASON_ENV",
    "docker_host",
    "git",
    "git_commit",
    "harness_skip_reason",
    "run",
)

#: Set to `1` to turn "no container runtime" from a skip into a failure. The
#: opt-in CI job sets it, because a job that exists to run these scenarios must
#: not report success for having skipped every one of them.
REQUIRE_ACT_ENV = "CUPRUM_REQUIRE_ACT"
#: A caller-supplied skip reason. An outer harness that has already probed the
#: host can pass that verdict down through this rather than paying for the
#: probe again, and the reasons a report shows stay decided by one piece of
#: code. It carries a reason *string*, not a flag: any non-empty value is
#: reported as the skip reason, so `CUPRUM_ACT_SKIP_REASON=1` would skip with
#: the reason `1`.
SKIP_REASON_ENV = "CUPRUM_ACT_SKIP_REASON"

# `act` accepts several spellings for the same runtime, and `DOCKER_HOST` can
# point at any of them. The probe checks the sockets rather than the
# executables alone, because podman can be installed with no machine started.
#: The variable naming the runtime endpoint. A value set by the caller is
#: authoritative: it is the only spelling that can name a daemon this host
#: cannot probe — a remote one — so the harness must neither replace it with a
#: local socket nor skip because no local socket was found.
DOCKER_HOST_ENV = "DOCKER_HOST"
_RUNTIME_PROBES = (
    "/run/user/{uid}/podman/podman.sock",
    "/var/run/docker.sock",
    "/run/podman/podman.sock",
)
_RUNTIME_COMMANDS = ("podman", "docker")
#: The catalogue name for the commands the harness runs. It is the project
#: those commands are attributed to, not a repository name.
_CATALOGUE = "cuprum-act-harness"


def _socket_paths() -> tuple[str, ...]:
    """Return the candidate runtime sockets for this host, if any.

    Returns
    -------
    tuple of str
        The socket paths with the user id substituted, or an empty tuple on a
        host with no POSIX user id, where none of the candidates can be named
        and so none can be tested.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return ()
    return tuple(path.format(uid=getuid()) for path in _RUNTIME_PROBES)


def _probe_reason() -> str:
    """Return why `act` cannot run here, or ``""`` when it can.

    A configured `DOCKER_HOST` answers the endpoint question by itself, so the
    local-socket test is skipped for it: the value may name a remote daemon
    that no probe here can reach, and a run that would have worked must not be
    skipped for want of a local socket. The `act` and runtime-command checks
    still apply, because both are still needed to run anything.

    Returns
    -------
    str
        A human-readable blocker, or the empty string if `act` and a container
        runtime are both present.
    """
    if shutil.which("act") is None:
        return (
            "act is not installed; see "
            "docs/local-validation-of-github-actions-with-act-and-pytest.md"
        )
    if not any(shutil.which(command) for command in _RUNTIME_COMMANDS):
        return "neither podman nor docker is installed, so act has no container runtime"
    if os.environ.get(DOCKER_HOST_ENV, ""):
        # A configured endpoint is this host's answer, and it may name a
        # daemon no local probe can reach. Testing the local sockets instead
        # would skip a run the caller has already said how to perform.
        return ""
    paths = _socket_paths()
    if not paths:
        return "this host has no POSIX user id, so act has no container socket"
    if not any(pth.Path(path).exists() for path in paths):
        return (
            "no container runtime socket was found at "
            f"{', '.join(paths)}, so act cannot start a container"
        )
    return ""


def harness_skip_reason() -> str:
    """Return why a scenario should skip, or fail when skipping is refused.

    `CUPRUM_REQUIRE_ACT=1` turns a missing runtime into a failure. Without it,
    a job that provides a runtime for these scenarios would still report
    success on the day the runtime silently disappeared — and a suite that
    skips is indistinguishable from a suite that passes.

    Returns
    -------
    str
        The reason to skip.

    Raises
    ------
    AssertionError
        If the harness cannot run and skipping has been refused.
    """
    reason = os.environ.get(SKIP_REASON_ENV) or _probe_reason()
    if not reason:
        return ""
    if os.environ.get(REQUIRE_ACT_ENV) == "1":
        message = (
            f"{REQUIRE_ACT_ENV}=1 requires the workflow integration harness to "
            f"run, but it cannot: {reason}"
        )
        raise AssertionError(message)
    return reason


def docker_host() -> str:
    """Return the runtime endpoint `act` should use.

    A caller-supplied `DOCKER_HOST` wins, because it names an endpoint this
    function cannot discover: a daemon on another host, a TCP listener, or a
    socket outside the probed set. Overriding it with a local socket would
    silently redirect the run away from the daemon the caller configured, and
    failing because no *local* socket exists would refuse to run a harness that
    would have worked. Both are failures of a supported configuration, so the
    configured value is returned unchanged and unprobed.

    Returns
    -------
    str
        The configured endpoint, or a `DOCKER_HOST` value naming an existing
        local socket.

    Raises
    ------
    AssertionError
        If no endpoint is configured and no known local socket exists, so the
        probe and the run cannot disagree.
    """
    configured = os.environ.get(DOCKER_HOST_ENV, "")
    if configured:
        return configured
    for path in _socket_paths():
        if pth.Path(path).exists():
            return f"unix://{path}"
    message = f"no container runtime socket found in {_RUNTIME_PROBES}"
    raise AssertionError(message)


def run(program: str, *arguments: str) -> SafeCmd:
    """Return a runnable command for one program.

    Parameters
    ----------
    program : str
        Program to run, resolved through a catalogue this module owns.
    *arguments : str
        Arguments to pass.

    Returns
    -------
    SafeCmd
        A command whose project settings allowlist ``program`` alone.
    """
    catalogue = ProgramCatalogue.from_programs(program, name=_CATALOGUE)
    entry = catalogue.lookup(program)
    return SafeCmd(program=entry.program, argv=arguments, project=entry.project)


def git(repository: pth.Path, *arguments: str) -> str:
    """Run one `git` command in ``repository`` and return its stdout.

    `git` runs through Cuprum's own driver, the same way the scenarios run
    `act`. A non-zero exit is raised rather than returned, because a scenario
    whose repository is not the shape it asked for would otherwise fail
    somewhere further away from the cause.

    Parameters
    ----------
    repository : pathlib.Path
        Working directory for the command.
    *arguments : str
        Arguments after the subcommand's name.

    Returns
    -------
    str
        The command's standard output.

    Raises
    ------
    AssertionError
        If `git` exits non-zero, which would mean the scenario's repository is
        not the shape the harness asked for.
    """
    result = run("git", *arguments).run_sync(
        context=ExecutionContext(cwd=str(repository))
    )
    message = (
        f"git {' '.join(arguments)} failed in {repository} "
        f"with exit {result.exit_code}:\n{result.stderr}"
    )
    if not result.ok:
        raise AssertionError(message)
    return result.stdout or ""


def git_commit(
    repository: pth.Path, *, message: str, allow_empty: bool = False
) -> None:
    """Commit ``repository``'s staged tree with the harness's fixed identity.

    The identity is passed per-command rather than configured, so the harness
    never depends on — and never writes to — the developer's git config.

    Parameters
    ----------
    repository : pathlib.Path
        Repository to commit into.
    message : str
        Commit message, which also names the scenario in `git log`.
    allow_empty : bool
        Commit even when the tree is unchanged. Needed for the empty
        changed-path set, which is a scenario in its own right.
    """
    arguments = [
        "-c",
        "user.name=act-harness",
        "-c",
        "user.email=act-harness@example.invalid",
        "commit",
        "--quiet",
        f"--message={message}",
    ]
    if allow_empty:
        arguments.append("--allow-empty")
    git(repository, *arguments)
