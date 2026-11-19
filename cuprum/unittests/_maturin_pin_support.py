"""The manylinux container pin pattern shared by two test modules.

`test_maturin_pins.py` asserts the pinned aarch64 container reference matches
this pattern, and `test_manylinux_container_ref_properties.py` generates
references to probe the same pattern. It lives here so neither test module has
to import a private name from the other.

The maturin *version* readers that used to live here have moved to
`cuprum.unittests._tool_pin_support`: maturin is read with the same structured
``pyproject.toml`` and ``uv.lock`` readers as every other tool pin, so a bug in
one tool's reading cannot hide behind a maturin-specific search.
"""

from __future__ import annotations

import re

MANYLINUX_CONTAINER_SHA256_RE = re.compile(
    r"^ghcr\.io/rust-cross/manylinux_2_28-cross@sha256:[0-9a-f]{64}$"
)
