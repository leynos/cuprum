"""Public-surface tests for the consumer-cancellation error.

A stream consumer that ends cancelled must be distinguishable from the
caller's own cancellation and from an expired deadline, so the error is a
public, ordinary exception with its own stream label. These tests pin the
export identity, the inheritance contract, and the annotation a caller can
resolve at runtime.
"""

from __future__ import annotations

import asyncio
import typing as typ

import cuprum as c
from cuprum import sh
from cuprum._stream_consumer_errors import (
    ConsumerCancelledError,
    ConsumerStreamName,
)
from cuprum.lines import LineStreamName


def test_consumer_cancelled_error_is_one_object_across_all_roots() -> None:
    """The error is exported from both public roots with one definition."""
    assert c.ConsumerCancelledError is ConsumerCancelledError, (
        "cuprum must re-export the defining class itself"
    )
    assert sh.ConsumerCancelledError is ConsumerCancelledError, (
        "cuprum.sh must re-export the defining class itself"
    )
    assert "ConsumerCancelledError" in c.__all__, (
        "cuprum.__all__ must publish the error"
    )
    assert "ConsumerCancelledError" in sh.__all__, (
        "cuprum.sh.__all__ must publish the error"
    )


def test_consumer_cancelled_error_is_not_a_cancellation_or_deadline() -> None:
    """The error is an ordinary exception, so callers can tell it apart."""
    error = ConsumerCancelledError("stderr")
    assert isinstance(error, Exception), "the error must be an ordinary exception"
    assert not isinstance(error, asyncio.CancelledError), (
        "a consumer cancellation must not be mistaken for the caller's"
    )
    assert not isinstance(error, TimeoutError), (
        "a consumer cancellation must not be mistaken for a deadline"
    )
    assert error.stream == "stderr", (
        f"the stream must be reported, got {error.stream!r}"
    )
    assert "stderr" in str(error), f"the message must name the stream, got {error!r}"
    assert "consumer ended cancelled before end of stream" in str(error), (
        f"the message must name the outcome, got {error!r}"
    )


def test_consumer_cancelled_error_stream_annotation_is_resolvable() -> None:
    """The annotation resolves, and names exactly the line-stream vocabulary."""
    hints = typ.get_type_hints(ConsumerCancelledError)
    assert hints["stream"] == ConsumerStreamName, (
        "the annotation must resolve to the module's stream alias, got "
        f"{hints['stream']!r}"
    )
    # ``ConsumerStreamName`` is a PEP 695 alias, so the resolved annotation is
    # the alias itself rather than a flattened literal. Compare its value.
    assert ConsumerStreamName.__value__ == LineStreamName.__value__, (
        "the stream alias must be exactly the line-stream vocabulary, got "
        f"{ConsumerStreamName.__value__!r}"
    )
