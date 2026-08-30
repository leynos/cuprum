"""Shared exception types for the benchmark suite."""


class BenchmarkError(Exception):
    """Base class for exceptions raised by the benchmark suite."""


class PtyBlackholeStateError(RuntimeError, BenchmarkError):
    """Raised when a ``PtyBlackhole`` lifecycle operation is unavailable."""
