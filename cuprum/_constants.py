"""Internal constants for the cuprum package."""

from __future__ import annotations

PACKAGE_NAME = "cuprum"

# Echoed lines are capped so a single oversized child line cannot overflow a
# CI job log (GitHub Actions stops at a 64 KiB line) while capture stays
# complete.
DEFAULT_ECHO_MAX_LINE_BYTES = 64 * 1024

# Error policy for every decode that renders a *view* of a child's bytes rather
# than the run's capture: line observation, echo, and the bounded mirror. Such
# a renderer must never be able to end the run, so it always replaces
# undecodable bytes whatever ``ExecutionContext.errors`` the caller chose. That
# policy governs the capture alone, where ``errors="strict"`` still raises,
# because the capture decodes its own untouched buffer. Reading the caller's
# policy here instead would let an ambient observer — a registered
# ``sh.observe()`` hook, or the line feeder the idle partition attaches —
# decide the fate of a run it merely watches.
OBSERVER_ERROR_POLICY = "replace"
