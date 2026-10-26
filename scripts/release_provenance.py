"""Inspect GitHub CLI attestation JSON for a matching release asset subject."""

from __future__ import annotations

import json
import typing as typ

if typ.TYPE_CHECKING:
    import pathlib as pth


def has_matching_record(provenance: pth.Path, name: str, digest: str) -> bool:
    """Read one verifier record and find its exact release asset subject.

    Parameters
    ----------
    provenance : pathlib.Path
        Directory holding one ``<name>.json`` verifier result per asset.
    name : str
        Exact release asset filename to find in a statement subject.
    digest : str
        Expected SHA-256 hexadecimal digest; comparison ignores letter case.

    Returns
    -------
    bool
        Whether the record exists, is readable JSON, and binds ``name`` to
        ``digest``. A missing, unreadable, or malformed record returns
        ``False``.

    Examples
    --------
    >>> from pathlib import Path
    >>> has_matching_record(Path("provenance"), "cuprum.whl", "a" * 64)
    False
    """
    record = provenance / f"{name}.json"
    try:
        document = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return has_matching_subject(document, name, digest)


def has_matching_subject(document: object, name: str, digest: str) -> bool:
    """Return whether verifier JSON binds ``name`` to the expected SHA-256.

    Parameters
    ----------
    document : object
        JSON-decoded output from ``gh attestation verify --format json``.
    name : str
        Exact release asset filename to find in a statement subject.
    digest : str
        Expected SHA-256 hexadecimal digest; comparison ignores letter case.

    Returns
    -------
    bool
        Whether a non-empty verification array contains the exact filename and
        digest.

    Examples
    --------
    >>> digest = "a" * 64
    >>> record = {
    ...     "verificationResult": {
    ...         "statement": {
    ...             "subject": [{"name": "cuprum.whl", "digest": {"sha256": digest}}]
    ...         }
    ...     }
    ... }
    >>> has_matching_subject([record], "cuprum.whl", digest)
    True
    """
    if not isinstance(document, list) or not document:
        return False
    return any(_result_matches(result, name, digest) for result in document)


def _result_matches(result: object, name: str, digest: str) -> bool:
    """Check one verification result for a matching subject."""
    # Each untrusted JSON layer must pass an explicit type check before lookup.
    if not isinstance(result, dict):
        return False
    verification = result.get("verificationResult")
    if not isinstance(verification, dict):
        return False
    statement = verification.get("statement")
    if not isinstance(statement, dict):
        return False
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        return False
    return any(_subject_matches(subject, name, digest) for subject in subjects)


def _subject_matches(subject: object, name: str, digest: str) -> bool:
    """Check one statement subject's exact filename and SHA-256."""
    if not isinstance(subject, dict) or subject.get("name") != name:
        return False
    subject_digest = subject.get("digest")
    if not isinstance(subject_digest, dict):
        return False
    sha256 = subject_digest.get("sha256")
    return isinstance(sha256, str) and sha256.lower() == digest.lower()
