"""Generate deterministic base64 fixtures for tee-path profiling."""

from __future__ import annotations

import argparse
import base64
import dataclasses as dc
import hashlib
import json
import pathlib as pth
import typing as typ

_ALGORITHM = "sha256-counter-v1"
_RAW_CHUNK_SIZE = 96 * 1024
_COUNTER_BYTES = 8


class _Digest(typ.Protocol):
    """Minimal hash object protocol used while streaming output."""

    def update(self, data: bytes) -> None:
        """Update the digest with bytes."""

    def hexdigest(self) -> str:
        """Return the hexadecimal digest."""


@dc.dataclass(frozen=True, slots=True)
class FixtureConfig:
    """Configuration for deterministic fixture generation."""

    seed: int
    raw_bytes: int
    wrap: int

    def __post_init__(self) -> None:
        """Validate fixture invariants."""
        if self.raw_bytes < 0:
            msg = f"raw-bytes must be >= 0, got {self.raw_bytes}"
            raise ValueError(msg)
        if self.wrap not in {0, 76}:
            msg = f"wrap must be 0 or 76, got {self.wrap}"
            raise ValueError(msg)


@dc.dataclass(slots=True)
class _CounterStream:
    """Small deterministic byte stream backed by SHA-256 counter mode."""

    seed_bytes: bytes
    counter: int = 0
    pending: bytes = b""

    def read(self, size: int) -> bytes:
        """Return exactly ``size`` bytes unless ``size`` is zero."""
        output = bytearray()
        while len(output) < size:
            if not self.pending:
                counter_bytes = self.counter.to_bytes(_COUNTER_BYTES, "big")
                self.pending = hashlib.sha256(self.seed_bytes + counter_bytes).digest()
                self.counter += 1
            needed = size - len(output)
            output.extend(self.pending[:needed])
            self.pending = self.pending[needed:]
        return bytes(output)


@dc.dataclass(slots=True)
class _EncodedWriter:
    """Stateful base64 output writer with optional line wrapping."""

    target: typ.BinaryIO
    wrap: int
    digest: _Digest
    column: int

    def write_chunk(self, payload: bytes) -> int:
        """Write one encoded payload chunk and return output byte count."""
        encoded = base64.b64encode(payload)
        if self.wrap == 0:
            self.target.write(encoded)
            self.digest.update(encoded)
            return len(encoded)

        return self._write_wrapped(encoded)

    def _write_wrapped(self, encoded: bytes) -> int:
        """Write encoded bytes with stable line wrapping."""
        output_bytes = 0
        offset = 0
        while offset < len(encoded):
            available = self.wrap - self.column
            part = encoded[offset : offset + available]
            self.target.write(part)
            self.digest.update(part)
            output_bytes += len(part)
            offset += len(part)
            self.column += len(part)
            if self.column == self.wrap:
                self.target.write(b"\n")
                self.digest.update(b"\n")
                output_bytes += 1
                self.column = 0
        return output_bytes


def write_fixture(
    config: FixtureConfig,
    *,
    output: pth.Path,
    manifest: pth.Path,
) -> dict[str, object]:
    """Write a deterministic base64 fixture and JSON manifest."""
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)

    stream = _CounterStream(str(config.seed).encode("utf-8"))
    remaining = config.raw_bytes
    output_bytes = 0
    digest = hashlib.sha256()

    with output.open("wb") as target:
        writer = _EncodedWriter(
            target=target,
            wrap=config.wrap,
            digest=typ.cast("_Digest", digest),
            column=0,
        )
        while remaining > 0:
            size = min(remaining, _RAW_CHUNK_SIZE)
            size -= size % 3 if remaining > _RAW_CHUNK_SIZE else 0
            chunk = stream.read(size)
            output_bytes += writer.write_chunk(chunk)
            remaining -= size

    payload: dict[str, object] = {
        "seed": config.seed,
        "raw_bytes": config.raw_bytes,
        "wrap": config.wrap,
        "output_bytes": output_bytes,
        "sha256": digest.hexdigest(),
        "algorithm": _ALGORITHM,
    }
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def _parse_args() -> argparse.Namespace:
    """Parse fixture generation CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--raw-bytes", type=int, required=True)
    parser.add_argument("--wrap", type=int, choices=(0, 76), required=True)
    parser.add_argument("--output", type=pth.Path, required=True)
    parser.add_argument("--manifest", type=pth.Path, required=True)
    return parser.parse_args()


def main() -> int:
    """Run the fixture generator."""
    args = _parse_args()
    payload = write_fixture(
        FixtureConfig(seed=args.seed, raw_bytes=args.raw_bytes, wrap=args.wrap),
        output=args.output,
        manifest=args.manifest,
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
