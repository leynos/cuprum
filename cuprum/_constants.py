"""Internal constants for the cuprum package."""

from __future__ import annotations

PACKAGE_NAME = "cuprum"

# Echoed lines are capped so a single oversized child line cannot overflow a
# CI job log (GitHub Actions stops at a 64 KiB line) while capture stays
# complete.
DEFAULT_ECHO_MAX_LINE_BYTES = 64 * 1024

# Bytes a ``FileBackedCapture`` holds on the heap before it spills the stream to
# a temporary file. One mebibyte is enough to keep a chatty-but-finite command's
# output in memory, where the caller reads it from ``stdout``/``stderr`` as
# before, while still bounding what a genuinely noisy child can cost the parent.
# The threshold is a memory decision, never a content one: a stream that stays
# below it is finalized exactly as ``FullCapture`` would finalize it.
DEFAULT_CAPTURE_SPILL_THRESHOLD_BYTES = 1024 * 1024

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
