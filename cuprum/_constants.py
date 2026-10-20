"""Shared stream names and defaults for subprocess and output handling.

Keeping child-stream names here lets process reaping wire descriptors before
the higher-level stdio resolver imports them. Output bounds and decoding
defaults keep command and pipeline observers consistent.
"""

from __future__ import annotations

import typing as typ

PACKAGE_NAME = "cuprum"

# Echoed lines are capped so a single oversized child line cannot overflow a
# CI job log (GitHub Actions stops at a 64 KiB line) while capture stays
# complete.
DEFAULT_ECHO_MAX_LINE_BYTES = 64 * 1024

# Line observation retains an incomplete line until its terminator arrives.
# That wait is unbounded only if the terminator never does, so the default
# bounds it in *decoded characters* — not bytes, and not counting the
# terminator — and a longer line is published as a sequence of fragments.
# Chosen to match the echo bound so a default run does not retain more per
# line than it would mirror.
DEFAULT_MAX_LINE_CHARS = 64 * 1024

# Error policy a byte-exact run's *views* decode under. A view is any decode
# that renders the child's bytes for display or inspection — line observation,
# echo, the bounded mirror — rather than the run's capture. In byte mode the
# capture is the child's own bytes and must reach the caller untouched, so a
# view may not end the run and strand them: byte-mode views replace undecodable
# input whatever ``ExecutionContext.errors`` the caller chose, which stops an
# ambient observer — a registered ``sh.observe()`` hook, or the line feeder the
# idle partition attaches — from deciding the fate of a run it merely watches.
# Text mode is untouched: there the view and the capture decode under the
# caller's configured policy, so ``errors="strict"`` raises from either exactly
# as it did before byte mode existed.
OBSERVER_ERROR_POLICY = "replace"

# The child's three standard streams, named as the spawn layer names them. They
# live here, in the one module with no cuprum imports of its own, so the reaping
# layer can gate its pipe wiring without depending on the stdio vocabulary that
# resolves those streams in the first place.
STDIN_STREAM = "stdin"
STDOUT_STREAM = "stdout"
STDERR_STREAM = "stderr"

# The names a pipe can be requested under. ``DirectProcessConfig.pipes`` carries
# a subset of these.
type PipeStream = typ.Literal["stdin", "stdout", "stderr"]
