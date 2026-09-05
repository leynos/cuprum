"""JSON and filesystem persistence for benchmark-ratchet history."""

from __future__ import annotations

import json
import os
import pathlib as pth
import tempfile
import typing as typ

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
    _require_positive_float,
)
from benchmarks.benchmark_profile import require_worker_iterations
from benchmarks.errors import BenchmarkError
from benchmarks.ratchet_history import BaselineHistory, HistorySample

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: Schema version; later shapes fail rather than silently becoming empty history.
HISTORY_SCHEMA = 1


class BaselineHistoryReadError(ValueError, BenchmarkError):
    """A persisted baseline-history payload could not be read or validated.

    Parameters
    ----------
    reason : str
        Stable explanation of the failed read or validation operation.
    path : pathlib.Path | None
        History file involved in the failure, when one was being read.

    Attributes
    ----------
    reason : str
        Stable explanation supplied to the constructor.
    path : pathlib.Path | None
        File supplied to the constructor, if the failure involved one.
    """

    def __init__(self, reason: str, *, path: pth.Path | None = None) -> None:
        self.path = path
        self.reason = reason
        subject = f"baseline history {path}" if path is not None else "baseline history"
        super().__init__(f"{subject}: {reason}")


class BaselineHistoryNotFoundError(BaselineHistoryReadError):
    """The optional baseline-history file was not present.

    Parameters
    ----------
    path : pathlib.Path
        Missing history file, exposed through ``path``; ``reason`` is
        ``"does not exist"``.
    """

    def __init__(self, path: pth.Path) -> None:
        super().__init__("does not exist", path=path)


def _sample_to_payload(sample: HistorySample) -> dict[str, object]:
    """Convert one immutable history sample to its JSON-compatible payload."""
    return {
        "commit": sample.commit,
        "run_id": sample.run_id,
        "benchmark_profile_version": sample.benchmark_profile_version,
        "worker_iterations": sample.worker_iterations,
        "ratios": dict(sorted(sample.ratios.items())),
    }


def history_to_payload(history: BaselineHistory) -> dict[str, object]:
    """Convert history to the stable JSON payload shape.

    Parameters
    ----------
    history : BaselineHistory
        Immutable rolling-window history to serialize.

    Returns
    -------
    dict[str, object]
        Payload containing the current schema value and ordered sample entries.
    """
    return {
        "schema": HISTORY_SCHEMA,
        "samples": [_sample_to_payload(sample) for sample in history.samples],
    }


def _sample_from_payload(value: object, *, index: int) -> HistorySample:
    """Build one `HistorySample` from its JSON payload."""
    payload = _require_mapping(value, name=f"samples[{index}]")
    ratios_payload = _require_mapping(
        payload.get("ratios"), name=f"samples[{index}].ratios"
    )
    ratios = {
        _require_non_empty_string(name, name=f"samples[{index}].ratios key"): (
            _require_positive_float(ratio, name=f"samples[{index}].ratios[{name!r}]")
        )
        for name, ratio in ratios_payload.items()
    }
    return HistorySample(
        commit=_require_non_empty_string(
            payload.get("commit"), name=f"samples[{index}].commit"
        ),
        run_id=_require_non_empty_string(
            payload.get("run_id"), name=f"samples[{index}].run_id"
        ),
        benchmark_profile_version=_require_non_empty_string(
            payload.get("benchmark_profile_version"),
            name=f"samples[{index}].benchmark_profile_version",
        ),
        worker_iterations=require_worker_iterations(payload),
        ratios=ratios,
    )


def history_from_payload(payload: cabc.Mapping[str, object]) -> BaselineHistory:
    """Build a `BaselineHistory` from a recognized, validated JSON payload.

    Parameters
    ----------
    payload : collections.abc.Mapping[str, object]
        Decoded persistence payload with the history schema and samples.

    Returns
    -------
    BaselineHistory
        Immutable domain history represented by the payload.

    Raises
    ------
    BaselineHistoryReadError
        If the schema does not match the persisted-history contract.
    TypeError, ValueError
        If a sample does not meet the persistence contract.
    """  # ruff: ignore[docstring-extraneous-exception] - validation helpers preserve their public errors.
    schema = payload.get("schema")
    if schema != HISTORY_SCHEMA:
        msg = f"baseline history schema must be {HISTORY_SCHEMA!r}, got {schema!r}"
        raise BaselineHistoryReadError(msg)
    samples = _require_list(payload.get("samples"), name="samples")
    return BaselineHistory(
        samples=tuple(
            _sample_from_payload(value, index=index)
            for index, value in enumerate(samples)
        )
    )


def _read_history_payload(path: pth.Path) -> object:
    """Read and JSON-decode a persisted history payload."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BaselineHistoryNotFoundError(path) from exc
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"could not read: {exc}"
        raise BaselineHistoryReadError(msg, path=path) from exc


def _history_from_payload_at_path(
    *, payload: object, path: pth.Path
) -> BaselineHistory:
    """Validate a decoded history payload while retaining its source path."""
    if not isinstance(payload, dict):
        msg = "must contain a JSON object"
        raise BaselineHistoryReadError(msg, path=path)
    try:
        return history_from_payload(typ.cast("dict[str, object]", payload))
    except BaselineHistoryReadError as exc:
        raise BaselineHistoryReadError(exc.reason, path=path) from exc
    except (TypeError, ValueError) as exc:
        msg = f"invalid payload: {exc}"
        raise BaselineHistoryReadError(msg, path=path) from exc


def load_history(path: pth.Path) -> BaselineHistory:
    """Load history, distinguishing an absent file from unreadable content.

    Parameters
    ----------
    path : pathlib.Path
        History JSON path to read and validate.

    Returns
    -------
    BaselineHistory
        The validated persisted history.

    Raises
    ------
    BaselineHistoryNotFoundError
        If ``path`` does not exist.
    BaselineHistoryReadError
        If the path cannot be read or its JSON payload is invalid.
    """  # ruff: ignore[docstring-extraneous-exception] - helpers raise the documented typed errors.
    payload = _read_history_payload(path)
    return _history_from_payload_at_path(payload=payload, path=path)


def write_history(*, history: BaselineHistory, output_path: pth.Path) -> None:
    """Atomically replace ``output_path`` with the serialized history payload.

    Parameters
    ----------
    history : BaselineHistory
        Immutable history to serialize.
    output_path : pathlib.Path
        Destination to replace atomically after a durable temporary-file write.

    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: pth.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = pth.Path(temporary.name)
            temporary.write(
                json.dumps(history_to_payload(history), indent=2, sort_keys=True)
            )
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
