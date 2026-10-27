"""State-derived Hypothesis coverage for Rust writer ownership hand-off.

The precondition model passed 60 examples but generated 85 invalid cases;
98.62% of rule draws retried to find an enabled transition. With terminal
instrumentation, every generated run was incomplete, while 31.72% reached
duplicate-creation failure, 6.21% blocking-mode failure, 1.38% executor
rejection, and 2.76% a worker-side failure. No run completed or reached native
I/O. In the direct-transition run (seed 648), 60 examples had 2 invalid cases;
12.90% completed, 6.45% completed after cancellation, and 80.65% were
incomplete. All three pre-submission failures, worker-side failure, and native
I/O failure were observed. These terminal events overlap by failure category.
The health-check suppression has been removed after this coverage measurement.
"""

from __future__ import annotations

from hypothesis import settings

from cuprum.unittests._rust_pump_writer_handoff_stateful_model import (
    _RustPumpWriterHandoffModel,
)

TestRustPumpWriterHandoffLifecycle = _RustPumpWriterHandoffModel.TestCase
TestRustPumpWriterHandoffLifecycle.settings = settings(
    max_examples=60,
    stateful_step_count=16,
    deadline=None,
)
