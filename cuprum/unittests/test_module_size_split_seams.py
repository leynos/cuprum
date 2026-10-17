"""Provenance of the names the 400-line module splits moved.

Five production modules were split when the rebase onto ``main`` pushed them
past the module ceiling. Each split had to *move* a helper, not copy it, and
each left the old import path working through a re-export. Those two properties
are exactly what a line-count-driven split is tempted to break, and neither is
visible to Ruff or Pylint: a copied helper satisfies both the ceiling and the
old path, while silently giving test doubles and monkeypatches a second binding
that production no longer resolves.

The assertions here are therefore identity, not existence. ``hasattr`` would
pass against a stale copy; ``is`` fails, because a copy is a different object.
"""

from __future__ import annotations

import pytest

import cuprum._stdio_ownership as stdio_ownership
import cuprum._subprocess_spawn as spawn_module
import cuprum._subprocess_stream_exit as stream_exit
import cuprum._subprocess_stream_run as stream_run_module
import cuprum.sh.pipeline as pipeline_module
import cuprum.sh.safe_cmd as safe_cmd_module
from cuprum.adapters import _logging_projection as logging_projection
from cuprum.adapters import logging_adapter
from cuprum.sh import bytes_entry_points, line_options, pipeline_output

# (old path, new home, name) for every re-exported helper.
_REEXPORTS = [
    (spawn_module, stdio_ownership, "_close_owned_stdio"),
    (spawn_module, stdio_ownership, "_flush_borrowed_stdio"),
    (spawn_module, stdio_ownership, "_open_owned_stdio"),
    (spawn_module, stdio_ownership, "_output_stdio"),
    (spawn_module, stdio_ownership, "_stdin_stdio"),
    (stream_run_module, stream_exit, "_wait_for_streamed_process_exit"),
    (logging_adapter, logging_projection, "_build_extra"),
    (logging_adapter, logging_projection, "_format_message"),
    (pipeline_module, pipeline_output, "_DeprecatedOutputFlags"),
    (pipeline_module, pipeline_output, "_reject_stdio_targets"),
    (pipeline_module, pipeline_output, "_resolve_pipeline_output"),
    (safe_cmd_module, line_options, "_line_iteration_tracking"),
    (safe_cmd_module, line_options, "_reject_redirected_lines_stdout"),
]


@pytest.mark.parametrize(
    ("old_path", "new_home", "name"),
    _REEXPORTS,
    ids=[f"{home.__name__}.{name}" for _, home, name in _REEXPORTS],
)
def test_split_reexports_the_moved_object(
    old_path: object,
    new_home: object,
    name: str,
) -> None:
    """The old path binds the same object the new module defines."""
    assert getattr(old_path, name) is getattr(new_home, name), (
        f"{old_path.__name__}.{name} is not the object defined in "
        f"{new_home.__name__}; a copy would satisfy the old path without "
        f"being what production calls"
    )


def test_safe_cmd_inherits_the_bytes_mixin() -> None:
    """``SafeCmd``'s byte entry points are the mixin's own functions."""
    assert (
        safe_cmd_module.SafeCmd.run_bytes
        is bytes_entry_points._BytesEntryPoints.run_bytes
    )
    assert (
        safe_cmd_module.SafeCmd.run_bytes_sync
        is bytes_entry_points._BytesEntryPoints.run_bytes_sync
    )
