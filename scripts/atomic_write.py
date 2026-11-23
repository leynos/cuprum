"""Replace a file's contents without ever exposing a partial write.

Issue #607's evidence files are read by later analysis of the same run, and a
truncated JSON document is worse than an absent one: the analysis tool would
have to guess whether it is looking at a bug or at an interrupted write. The
write-to-a-temporary-file-then-rename pattern removes the question, because a
reader sees either the previous file or the complete new one.

The pattern appears in `docs/scripting-standards.md` and was open-coded in
three places before this module existed - a pytest plugin, the batch driver,
and the offline comparison tool. It is not a generic file utility: it exists so
those three producers agree on one durability rule, and it deliberately does
not absorb directory creation, retries, or permission handling, each of which a
caller may want to treat differently.

Errors are raised, not swallowed. A caller that treats a failed write as
harmless - the evidence plugin does, because a diagnostic must not fail the
suite it describes - catches `OSError` at its own boundary and says so there.
"""

from __future__ import annotations

import json
import os
import pathlib as pth
import tempfile

__all__ = ("write_json", "write_text")


def write_text(destination: pth.Path, text: str) -> pth.Path:
    """Write ``text`` to ``destination`` and return the written path.

    Parameters
    ----------
    destination : pathlib.Path
        Final path. Its parent directory must already exist.
    text : str
        Complete new contents, written with UTF-8 encoding.

    Returns
    -------
    pathlib.Path
        The destination, once the rename has completed.

    Raises
    ------
    OSError
        If the temporary file cannot be created or written, or the rename
        fails, or the destination's parent does not exist. A partial file is
        removed before the error propagates. The error is deliberately not
        wrapped, so a caller can distinguish a full disk from a refusal.
    """
    temporary: pth.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            encoding="utf-8",
        ) as handle:
            temporary = pth.Path(handle.name)
            handle.write(text)
            handle.flush()
            # Durability across a crash matters here because the file records a
            # run that has already finished; without the fsync the rename can
            # land while the contents are still only in the page cache.
            os.fsync(handle.fileno())
        temporary.replace(destination)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return destination


def write_json(destination: pth.Path, payload: object) -> pth.Path:
    """Serialize ``payload`` as sorted, indented JSON and write it atomically.

    Parameters
    ----------
    destination : pathlib.Path
        Final path. Its parent directory must already exist.
    payload : object
        Any JSON-serializable value.

    Returns
    -------
    pathlib.Path
        The destination, once the rename has completed.

    Raises
    ------
    OSError
        As :func:`write_text`.
    TypeError
        If ``payload`` contains a value the standard encoder cannot represent.
        Raised before anything is written, so a refusal leaves no partial file.
    """  # ruff: ignore[docstring-extraneous-exception] - TypeError propagates from `json.dumps`.
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return write_text(destination, text)
