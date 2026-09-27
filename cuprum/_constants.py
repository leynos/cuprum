"""Internal constants for the cuprum package."""

from __future__ import annotations

import typing as typ

PACKAGE_NAME = "cuprum"

# Echoed lines are capped so a single oversized child line cannot overflow a
# CI job log (GitHub Actions stops at a 64 KiB line) while capture stays
# complete.
DEFAULT_ECHO_MAX_LINE_BYTES = 64 * 1024

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
