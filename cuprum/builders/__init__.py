"""Core builders for common command-line tools."""

from __future__ import annotations

from cuprum.builders.args import GitRef, SafePath, git_ref, safe_path
from cuprum.builders.git import git_checkout, git_rev_parse, git_status
from cuprum.builders.rsync import RsyncOptions, rsync_sync
from cuprum.builders.tar import TarCreateOptions, tar_create, tar_extract

__all__ = [
    "GitRef",
    "RsyncOptions",
    "SafePath",
    "TarCreateOptions",
    "git_checkout",
    "git_ref",
    "git_rev_parse",
    "git_status",
    "rsync_sync",
    "safe_path",
    "tar_create",
    "tar_extract",
]
