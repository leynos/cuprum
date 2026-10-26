"""Small fixtures for release-asset provenance tests."""

from __future__ import annotations

import hashlib


def _digest(content: bytes) -> str:
    """Return the SHA-256 hex digest of test bytes."""
    return hashlib.sha256(content).hexdigest()


def _provenance(name: str, digest: str) -> list[dict[str, object]]:
    """Build a verifier result with one named digest subject."""
    return [
        {
            "verificationResult": {
                "statement": {"subject": [{"name": name, "digest": {"sha256": digest}}]}
            }
        }
    ]
