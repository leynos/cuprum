"""Binary tool installation must reject mismatches and never source-build."""

import hashlib
from pathlib import Path

import pytest

from scripts import install_boundary_kani as installer


def test_cached_binary_must_match_digest(tmp_path: Path) -> None:
    """A verified cache is reusable without performing a download."""
    archive = tmp_path / "binary.tar.gz"
    archive.write_bytes(b"verified archive")
    digest = hashlib.sha256(b"verified archive").hexdigest()
    installer.checked_download("https://example.invalid/asset", archive, digest)
    assert archive.read_bytes() == b"verified archive", "verified cache was modified"


def test_mismatch_preserves_existing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt downloads cannot replace a cached artefact or leave a partial."""
    archive = tmp_path / "binary.tar.gz"
    archive.write_bytes(b"old cache")

    def fetch(_url: str, destination: Path) -> None:
        """Supply a corrupt release body without network access."""
        destination.write_bytes(b"corrupt body")

    monkeypatch.setattr(installer, "_download", fetch)
    with pytest.raises(ValueError, match="checksum mismatch"):
        installer.checked_download("https://example.invalid/asset", archive, "0" * 64)
    assert archive.read_bytes() == b"old cache", "corrupt download replaced the cache"
    assert not archive.with_suffix(".download").exists(), "partial download leaked"


def test_plain_http_is_rejected(tmp_path: Path) -> None:
    """The installer cannot fetch a verifier over an unauthenticated scheme."""
    with pytest.raises(ValueError, match="HTTPS"):
        installer.checked_download(
            "http://example.invalid/asset", tmp_path / "binary", "0" * 64
        )


@pytest.mark.parametrize(
    "redirect",
    [
        ("http://example.invalid/asset", "require HTTPS"),
        (None, "no Location"),
        ("/another-redirect", "redirect limit"),
    ],
)
def test_release_redirects_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    redirect: tuple[str | None, str],
) -> None:
    """Insecure, malformed, and looping redirects cannot install a binary."""
    from unittest import mock

    location, message = redirect

    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status = 302
    response.getheader.return_value = location
    connection = mock.Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        installer.http.client, "HTTPSConnection", lambda *_args, **_kwargs: connection
    )
    with pytest.raises(ValueError, match=message):
        installer._download("https://example.invalid/asset", tmp_path / "binary")
    connection.close.assert_called()
    assert not (tmp_path / "binary").exists(), "rejected redirect created a binary"
