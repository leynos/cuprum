"""Contract test that cache-writing jobs free disk before any cache step.

A Rust compiler-cache miss (every version bump causes one) grows the archive to
about 3 GiB, which does not fit beside the directory it packs on the 2 GB a
Linux job is left with. Pull-request runs never save, so only a push to `main`
shows the failure.
"""

from __future__ import annotations

import shlex

import pytest

from tests.helpers.ci_runners import (
    CACHE_PLAIN,
    CACHE_RESTORE,
    CACHE_SAVE,
    CACHE_WRITERS,
    steps,
)

FREE_DISK_STEP = "Free preinstalled toolchains the job does not use"
FREED_DIRECTORIES = ("/usr/local/lib/android", "/usr/share/dotnet")
CACHE_USES = (CACHE_RESTORE, CACHE_SAVE, CACHE_PLAIN)


def _is_recursive_removal(tokens: list[str], paths: tuple[str, ...]) -> bool:
    """Return whether a command segment recursively removes every path."""
    command = tokens[1:] if tokens[:1] == ["sudo"] else tokens
    if not command or command[0] != "rm":
        return False

    arguments = command[1:]
    end_options = arguments.index("--") if "--" in arguments else len(arguments)
    options = arguments[:end_options]
    operands = (
        arguments[end_options + 1 :] if end_options < len(arguments) else arguments
    )
    is_recursive = any(
        option == "--recursive"
        or (
            option.startswith("-") and not option.startswith("--") and "r" in option[1:]
        )
        for option in options
    )
    return is_recursive and all(path in operands for path in paths)


def _has_recursive_removal(command: str, paths: tuple[str, ...]) -> bool:
    """Find a shell command segment that recursively removes every path."""
    for line in command.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        segments: list[list[str]] = [[]]
        for token in lexer:
            if token and not token.strip(";&|"):
                segments.append([])
            else:
                segments[-1].append(token)
        if any(_is_recursive_removal(segment, paths) for segment in segments):
            return True
    return False


@pytest.mark.parametrize(
    ("command", "should_remove"),
    [
        ("sudo rm -rf /usr/local/lib/android /usr/share/dotnet", True),
        ("rm --recursive --force /usr/local/lib/android /usr/share/dotnet", True),
        ("echo /usr/local/lib/android /usr/share/dotnet", False),
        ("test -d /usr/local/lib/android && test -d /usr/share/dotnet", False),
        (
            "printf '%s\\n' 'sudo rm -rf /usr/local/lib/android /usr/share/dotnet'",
            False,
        ),
        ("# sudo rm -rf /usr/local/lib/android /usr/share/dotnet", False),
        ("sudo rm -f /usr/local/lib/android /usr/share/dotnet", False),
        ("sudo rm -rf /usr/local/lib/android && echo /usr/share/dotnet", False),
    ],
    ids=[
        "sudo-recursive-removal",
        "long-recursive-option",
        "printed-paths",
        "inspected-paths",
        "quoted-diagnostic",
        "commented-command",
        "non-recursive-removal",
        "paths-in-separate-commands",
    ],
)
def test_cleanup_command_must_recursively_remove_both_directories(
    command: str, should_remove: bool
) -> None:
    """Only an executable recursive removal of both paths satisfies cleanup."""
    assert _has_recursive_removal(command, FREED_DIRECTORIES) is should_remove, (
        f"unexpected recursive removal result for {command!r}"
    )


@pytest.mark.parametrize(
    ("workflow_name", "job_name"), sorted(CACHE_WRITERS["SCCACHE_CACHE_KEY"])
)
def test_compiler_cache_writers_free_disk_before_any_cache_step(
    workflow_name: str, job_name: str
) -> None:
    """Leave room for the archive: free unused toolchains before every cache."""
    job_steps = steps(workflow_name, job_name)
    freeing = [
        index
        for index, step in enumerate(job_steps)
        if step.get("name") == FREE_DISK_STEP
    ]
    where = f"{workflow_name}:{job_name}"
    assert len(freeing) == 1, f"{where} must declare {FREE_DISK_STEP!r} once"
    step = job_steps[freeing[0]]
    command = step.get("run")
    assert isinstance(command, str), f"{where} freeing step must run a command"
    assert _has_recursive_removal(command, FREED_DIRECTORIES), (
        f"{where} freeing step must recursively remove {FREED_DIRECTORIES}"
    )
    assert "runner.os == 'Linux'" in str(step.get("if", "")), (
        f"{where} freeing step must be guarded to Linux runners"
    )
    first_cache = min(
        index
        for index, other in enumerate(job_steps)
        if other.get("uses") in CACHE_USES
    )
    assert freeing[0] < first_cache, f"{where} must free disk before its first cache"
