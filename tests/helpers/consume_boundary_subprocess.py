"""Exercise direct native consume against a live subprocess pipe."""

from __future__ import annotations

import os
import queue
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed interpreter and source.
import sys
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


# The producer rewrites ``sys.stdout`` in binary mode on Windows so the
# platform's text translation cannot alter the payload the native consumer
# reads. It streams a repeated pattern so a one-mebibyte capture never asks
# this process to hold the whole payload twice.
_PRODUCER_SOURCE = (
    "import os, sys\n"
    "if os.name == 'nt':\n"
    " import msvcrt\n"
    " msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)\n"
    "pattern = b'cuprum-boundary/'\n"
    "length = int(sys.argv[1])\n"
    "tail = bytes.fromhex(sys.argv[2])\n"
    "remaining = length - len(tail)\n"
    "chunk = (pattern * 4096)[:65536]\n"
    "while remaining:\n"
    " data = chunk[:remaining]\n"
    " sys.stdout.buffer.write(data)\n"
    " remaining -= len(data)\n"
    "sys.stdout.buffer.write(tail)\n"
    "sys.stdout.buffer.flush()\n"
)

_PRODUCER_TIMEOUT_SECONDS = 30
_STATUS_TIMEOUT_SECONDS = 5


class _ProducerExitError(RuntimeError):
    """Report a producer that failed before the consumer saw end of input."""

    def __init__(self, status: int, diagnostics: bytes) -> None:
        """Keep the exit status and the captured diagnostics for the report."""
        super().__init__(f"producer exited {status}: {diagnostics!r}")
        self.status = status
        self.diagnostics = diagnostics


class _ConsumerTimeoutError(TimeoutError):
    """Report a native consumer that never reached pipe end of input."""


class _ConsumerOutcomeError(RuntimeError):
    """Re-raise a consumer failure outside its reporting thread."""

    def __init__(self, cause: BaseException) -> None:
        """Retain the originating failure so the caller can re-raise it."""
        super().__init__("native consumer failed")
        self.cause = cause


def _expected_prefix(payload: bytes, tail: bytes) -> bytes:
    """Rebuild the repeated-prefix payload this helper streams to a child."""
    pattern = b"cuprum-boundary/"
    prefix_length = len(payload) - len(tail)
    repetitions = (prefix_length + len(pattern) - 1) // len(pattern)
    return (pattern * repetitions)[:prefix_length]


def _start_producer(payload: bytes, tail: bytes) -> subprocess.Popen[bytes]:
    """Launch the fixed Python producer of ``payload`` on its standard output."""
    return subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed interpreter and source.
        [sys.executable, "-c", _PRODUCER_SOURCE, str(len(payload)), tail.hex()],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _consume_in_thread(
    consume: cabc.Callable[[int], str],
    reader_fd: int,
) -> tuple[threading.Thread, queue.Queue[tuple[str, object]]]:
    """Run ``consume`` off the main thread so producer cleanup never blocks."""
    outcome: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def consume_output() -> None:
        """Record the native result, or the failure that replaced it."""
        try:
            result = consume(reader_fd)
            # A borrowed descriptor must still be usable once consume returns.
            os.fstat(reader_fd)
        except Exception as error:  # ruff: ignore[blind-except] - re-raised in the caller.
            outcome.put(("error", error))
        else:
            outcome.put(("result", result))

    consumer = threading.Thread(
        target=consume_output,
        name="native-consumer",
        daemon=True,
    )
    consumer.start()
    return consumer, outcome


def _await_consumer(
    consumer: threading.Thread,
    outcome: queue.Queue[tuple[str, object]],
    producer: subprocess.Popen[bytes],
) -> str:
    """Join the consumer, then unwrap its recorded result or failure."""
    consumer.join(timeout=_PRODUCER_TIMEOUT_SECONDS)
    if consumer.is_alive():
        producer.terminate()
        consumer.join(timeout=_STATUS_TIMEOUT_SECONDS)
        if consumer.is_alive():
            producer.kill()
            consumer.join(timeout=_STATUS_TIMEOUT_SECONDS)
        msg = "native consumer did not reach pipe EOF"
        raise _ConsumerTimeoutError(msg)

    producer_status = producer.wait(timeout=_STATUS_TIMEOUT_SECONDS)
    producer_error = producer.stderr.read(4096) if producer.stderr else b""
    if producer_status != 0:
        raise _ProducerExitError(producer_status, producer_error)

    kind, result = outcome.get_nowait()
    if kind == "error":
        cause = typ.cast("Exception", result)
        raise _ConsumerOutcomeError(cause) from cause
    if not isinstance(result, str):
        msg = "native consumer did not return text"
        raise _ConsumerOutcomeError(TypeError(msg))
    return result


def capture_from_producer(
    payload: bytes,
    tail: bytes,
    consume: cabc.Callable[[int], str],
) -> str:
    """Drain a generated one-mebibyte producer through the native consumer."""
    if payload != _expected_prefix(payload, tail) + tail:
        msg = "subprocess payload must use the named boundary pattern"
        raise ValueError(msg)

    producer = _start_producer(payload, tail)
    if producer.stdout is None or producer.stderr is None:
        producer.kill()
        producer.wait()
        msg = "producer pipes were unavailable"
        raise RuntimeError(msg)

    consumer, outcome = _consume_in_thread(consume, producer.stdout.fileno())
    try:
        return _await_consumer(consumer, outcome, producer)
    finally:
        if producer.poll() is None:
            producer.terminate()
            try:
                producer.wait(timeout=_STATUS_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                producer.kill()
                producer.wait()
        producer.stdout.close()
        producer.stderr.close()
