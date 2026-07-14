"""Worker process used by hyperfine pipeline throughput benchmarks."""

from __future__ import annotations

import argparse
import dataclasses as dc
import logging
import sys

from cuprum import Program, ProgramCatalogue, ProjectSettings, ScopeConfig, scoped, sh

_logger = logging.getLogger(__name__)

_MIN_PIPELINE_STAGES = 2


def _validate_iterations_range(iterations: int) -> None:
    """Raise ``ValueError`` when *iterations* is outside the permitted range."""
    if iterations < 1:
        msg = f"iterations must be >= 1, got {iterations}"
        raise ValueError(msg)
    if iterations > 1000:  # noqa: PLR2004
        msg = f"iterations must be <= 1000, got {iterations}"
        raise ValueError(msg)


@dc.dataclass(frozen=True, slots=True)
class PipelineWorkerConfig:
    """Configuration for a single pipeline throughput run."""

    payload_bytes: int
    stages: int
    with_line_callbacks: bool
    iterations: int

    def __post_init__(self) -> None:
        """Validate worker invariants for direct callers and CLI usage."""
        if self.payload_bytes < 1:
            msg = f"payload-bytes must be >= 1, got {self.payload_bytes}"
            raise ValueError(msg)
        if self.stages < _MIN_PIPELINE_STAGES:
            msg = f"stages must be >= {_MIN_PIPELINE_STAGES}, got {self.stages}"
            raise ValueError(msg)
        _validate_iterations_range(self.iterations)


def _parse_args() -> PipelineWorkerConfig:
    """Parse command-line arguments for pipeline throughput worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--payload-bytes",
        type=int,
        required=True,
        help="Number of bytes the writer stage emits.",
    )
    parser.add_argument(
        "--stages",
        type=int,
        default=3,
        help="Total number of pipeline stages (minimum 2).",
    )
    parser.add_argument(
        "--line-callbacks",
        action="store_true",
        help=(
            "Use text line-based sink consumption to approximate callback-style "
            "line processing overhead."
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Number of pipeline runs to execute inside this worker process.",
    )
    args = parser.parse_args()

    return PipelineWorkerConfig(
        payload_bytes=args.payload_bytes,
        stages=args.stages,
        with_line_callbacks=args.line_callbacks,
        iterations=args.iterations,
    )


def _writer_script(*, with_line_callbacks: bool) -> str:
    """Return script for upstream payload generation.

    Parameters
    ----------
    with_line_callbacks:
        When ``True``, emit newline-terminated lines so downstream
        line-by-line readers process multiple lines rather than one
        giant blob.  When ``False``, emit raw binary bytes.
    """
    if with_line_callbacks:
        return "\n".join(
            [
                "import sys",
                "size = int(sys.argv[1])",
                "line = b'x' * 79 + b'\\n'",
                "remaining = size",
                "out = sys.stdout.buffer",
                "while remaining > 0:",
                "    n = min(remaining, len(line))",
                "    out.write(line[:n])",
                "    remaining -= n",
                "out.flush()",
            ],
        )
    return "\n".join(
        [
            "import sys",
            "size = int(sys.argv[1])",
            "chunk = b'x' * 65536",
            "remaining = size",
            "out = sys.stdout.buffer",
            "while remaining > 0:",
            "    n = min(remaining, len(chunk))",
            "    out.write(chunk[:n])",
            "    remaining -= n",
            "out.flush()",
        ],
    )


def _passthrough_script() -> str:
    """Return script for intermediate pass-through stages."""
    return "\n".join(
        [
            "import shutil",
            "import sys",
            "shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer, 65536)",
            "sys.stdout.buffer.flush()",
        ],
    )


def _sink_script(*, with_line_callbacks: bool) -> str:
    """Return sink script for final pipeline stage."""
    if with_line_callbacks:
        return "\n".join(["import sys", "for _ in sys.stdin:", "    pass"])
    return "\n".join(["import sys", "sys.stdin.buffer.read()"])


def _catalogue_for_worker() -> tuple[ProgramCatalogue, Program]:
    """Create an allowlist catalogue scoped to the current Python executable."""
    python_program = Program(sys.executable)
    project = ProjectSettings(
        name="benchmark-worker",
        programs=(python_program,),
        documentation_locations=("docs/users-guide.md#benchmark-suite",),
        noise_rules=(),
    )
    return ProgramCatalogue(projects=(project,)), python_program


def _build_pipeline(config: PipelineWorkerConfig) -> tuple[sh.Pipeline, Program]:
    """Build a multi-stage pipeline for throughput measurements."""
    catalogue, python_program = _catalogue_for_worker()
    python = sh.make(python_program, catalogue=catalogue)

    writer = python(
        "-c",
        _writer_script(with_line_callbacks=config.with_line_callbacks),
        str(config.payload_bytes),
    )
    passthrough = python("-c", _passthrough_script())
    sink = python(
        "-c",
        _sink_script(with_line_callbacks=config.with_line_callbacks),
    )

    pipeline = writer
    for _ in range(config.stages - 2):
        pipeline |= passthrough
    pipeline |= sink
    return pipeline, python_program


def run_pipeline_worker(config: PipelineWorkerConfig) -> int:
    """Execute one configured throughput benchmark pipeline run."""
    pipeline, python_program = _build_pipeline(config)
    _logger.debug(
        "starting pipeline worker: iterations=%d, payload_bytes=%d, "
        "stages=%d, with_line_callbacks=%s",
        config.iterations,
        config.payload_bytes,
        config.stages,
        config.with_line_callbacks,
    )
    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        for i in range(config.iterations):
            result = pipeline.run_sync(
                output=sh.RunOutputOptions(capture=False, echo=False),
            )
            if not result.ok:
                failure = result.failure
                exit_code = failure.exit_code if failure is not None else 1
                _logger.warning(
                    "pipeline iteration %d/%d failed: exit_code=%d",
                    i + 1,
                    config.iterations,
                    exit_code,
                )
                return exit_code

    _logger.debug(
        "pipeline worker completed: %d iteration(s) succeeded",
        config.iterations,
    )

    return 0


def main() -> int:
    """Run the throughput benchmark worker process."""
    config = _parse_args()
    return run_pipeline_worker(config)


if __name__ == "__main__":
    raise SystemExit(main())
