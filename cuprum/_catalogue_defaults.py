"""Default program data for Cuprum's curated catalogue."""

from __future__ import annotations

from cuprum.program import Program

CORE_OPS_PROJECT = "core-ops"
DOCUMENTATION_PROJECT = "docs"

ECHO = Program("echo")
GIT = Program("git")
LS = Program("ls")
RSYNC = Program("rsync")
TAR = Program("tar")
DOC_TOOL = Program("mdbook")

DEFAULT_PROJECT_DATA: tuple[
    tuple[str, tuple[Program, ...], tuple[str, ...], tuple[str, ...]], ...
] = (
    (
        CORE_OPS_PROJECT,
        (ECHO, GIT, LS, RSYNC, TAR),
        ("docs/users-guide.md#program-catalogue",),
        (r"^progress:", r"^note:"),
    ),
    (
        DOCUMENTATION_PROJECT,
        (DOC_TOOL,),
        ("https://docs.example.invalid/cuprum/catalogue",),
        (r"^\[INFO\]",),
    ),
)
