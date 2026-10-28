"""Parse `gh attestation verify --format json` output for release provenance.

Match the exact asset filename and SHA-256 subject. GitHub CLI policy flags
establish repository, signer-workflow and tag identity; this parser does not
independently verify cryptographic trust. `stage_pypi()` uses this check before
carrying GitHub asset bytes.
"""

from __future__ import annotations

import json
import typing as typ

if typ.TYPE_CHECKING:
    import pathlib as pth


class ProvenanceRecordError(Exception):
    """A verifier record is absent, unreadable, malformed, or mismatching.

    Parameters
    ----------
    message : str
        Safe diagnostic that identifies the record failure category.

    Notes
    -----
    Missing records are represented by ``None`` from
    :func:`read_provenance_record`, then rejected by
    :func:`require_matching_record`.
    """


def read_provenance_record(record: pth.Path) -> object | None:
    """Read one verifier record, returning ``None`` when it is absent.

    Parameters
    ----------
    record : pathlib.Path
        Verifier result file to read.

    Returns
    -------
    object | None
        Decoded JSON from the verifier, or ``None`` when the file is absent.

    Raises
    ------
    ProvenanceRecordError
        If the file cannot be read, is not UTF-8, or contains invalid JSON.

    Examples
    --------
    >>> from pathlib import Path
    >>> read_provenance_record(Path("missing.json")) is None
    True
    """
    try:
        content = record.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        msg = "cannot read provenance record"
        raise ProvenanceRecordError(msg) from error
    except UnicodeError as error:
        msg = "provenance record is not valid UTF-8"
        raise ProvenanceRecordError(msg) from error

    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        msg = "provenance record is not valid JSON"
        raise ProvenanceRecordError(msg) from error


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


def require_matching_record(record_path: pth.Path, name: str, digest: str) -> None:
    """Require one verifier record to bind an asset name to its SHA-256.

    Parameters
    ----------
    record_path : pathlib.Path
        JSON file returned by ``gh attestation verify --format json``.
    name : str
        Exact release asset filename to find in the record's subject.
    digest : str
        Expected SHA-256 hexadecimal digest; comparison ignores letter case.

    Raises
    ------
    ProvenanceRecordError
        If the record is absent, unreadable, malformed, or does not match.
    """
    record = read_provenance_record(record_path)
    if record is None:
        msg = "no verified build provenance record"
        raise ProvenanceRecordError(msg)
    if not has_matching_subject(record, name, digest):
        msg = "no matching verified build provenance"
        raise ProvenanceRecordError(msg)


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
