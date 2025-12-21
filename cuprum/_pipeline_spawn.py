"""Pipeline subprocess spawning and stream wiring.

This module is a thin wrapper around the underlying spawn and cleanup helpers.
The implementations live in ``cuprum._process_lifecycle`` to keep pipeline
orchestration cohesive and avoid import cycles.
"""

from __future__ import annotations

from cuprum._process_lifecycle import (
    _build_spawn_observations,
    _cleanup_spawned_processes,
    _spawn_pipeline_processes,
)

__all__ = [
    "_build_spawn_observations",
    "_cleanup_spawned_processes",
    "_spawn_pipeline_processes",
]
