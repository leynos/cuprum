"""Binary tool installation must reject mismatches and never source-build.

The installer entry points are exercised end to end against synthetic release
archives served through a stubbed download seam, so the tests cover what
``main()`` installs — and how it fails — without network access.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import typing as typ
import zipfile

import pytest

from scripts import install_boundary_kani as installer
from scripts import install_boundary_z3 as z3_installer

if typ.TYPE_CHECKING:
    from pathlib import Path

# The root environment variable that isolates both installers' pin file and
# cache. Spelled literally so the tests exercise the documented contract
# rather than the constant that implements it.
ROOT_ENV = "CUPRUM_BOUNDARY_ROOT"

KANI_FRONTEND_MEMBERS = {
    "cargo-kani": b"cargo-kani frontend executable",
    "kani": b"kani frontend executable",
}
KANI_BUNDLE_MEMBERS = {"kani": b"bundled kani driver"}


def _gzip_tar(members: dict[str, bytes]) -> bytes:
    """Build a gzip-compressed tar containing exactly the given members."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _zip_archive(members: dict[str, bytes]) -> bytes:
    """Build a zip containing exactly the given members."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _seed_kani_pin(root: Path, version: str = installer.VERSION) -> None:
    """Write the Kani version pin the isolated root is installed from."""
    pin = root / "tools/kani/VERSION"
    pin.parent.mkdir(parents=True)
    pin.write_text(f"{version}\n", encoding="utf-8")


def _serve_assets(
    monkeypatch: pytest.MonkeyPatch, assets: dict[str, bytes]
) -> list[str]:
    """Serve synthetic release bytes for `assets` and record every request.

    Each asset is keyed by the release URL it stands in for. The pinned digest
    constants are re-pointed at the synthetic bytes, so ``main()`` verifies and
    caches them exactly as it would a real release. The stub then checks each
    requested digest against the content it is asked to supply, and fails the
    test if the installer ever requests a URL the fixture does not cover.

    Returns
    -------
    list[str]
        The URLs the installer requested, in request order.
    """
    digests: dict[str, str] = {}
    for url, content in assets.items():
        digests[url] = hashlib.sha256(content).hexdigest()
    for url, attribute in (
        (installer.FRONTEND_URL, "FRONTEND_DIGEST"),
        (installer.BUNDLE_URL, "BUNDLE_DIGEST"),
    ):
        if url in digests:
            monkeypatch.setattr(installer, attribute, digests[url])
    if z3_installer.URL in digests:
        monkeypatch.setattr(z3_installer, "DIGEST", digests[z3_installer.URL])

    requested: list[str] = []

    def checked_download(url: str, destination: Path, digest: str) -> None:
        """Supply the synthetic release body without network access."""
        requested.append(url)
        content = assets.get(url)
        assert content is not None, f"no synthetic asset for {url}"
        assert hashlib.sha256(content).hexdigest() == digest, (
            "the installer must verify the patched expected digest"
        )
        destination.write_bytes(content)

    monkeypatch.setattr(installer, "checked_download", checked_download)
    monkeypatch.setattr(z3_installer, "checked_download", checked_download)
    return requested


class TestBoundaryBinaryInstall:
    """Installer entry points install only pinned, verified release content."""

    def test_cached_binary_must_match_digest(self, tmp_path: Path) -> None:
        """A verified cache is reusable without performing a download."""
        archive = tmp_path / "binary.tar.gz"
        archive.write_bytes(b"verified archive")
        digest = hashlib.sha256(b"verified archive").hexdigest()
        installer.checked_download("https://example.invalid/asset", archive, digest)
        assert archive.read_bytes() == b"verified archive", (
            "verified cache was modified"
        )

    def test_mismatch_preserves_existing_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corrupt downloads cannot replace a cached artefact or leave a partial."""
        archive = tmp_path / "binary.tar.gz"
        archive.write_bytes(b"old cache")

        def fetch(_url: str, destination: Path) -> None:
            """Supply a corrupt release body without network access."""
            destination.write_bytes(b"corrupt body")

        monkeypatch.setattr(installer, "_download", fetch)
        with pytest.raises(ValueError, match="checksum mismatch"):
            installer.checked_download(
                "https://example.invalid/asset", archive, "0" * 64
            )
        assert archive.read_bytes() == b"old cache", (
            "corrupt download replaced the cache"
        )
        assert not archive.with_suffix(".download").exists(), "partial download leaked"

    def test_an_unreadable_cache_is_refused_rather_than_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cache entry that cannot be read must not be answered with a download."""
        archive = tmp_path / "binary.tar.gz"
        archive.write_bytes(b"cached archive")
        downloads: list[str] = []

        def fetch(url: str, _destination: Path) -> None:
            """Record any attempted download so the test can reject it."""
            downloads.append(url)

        def unreadable(_path: Path) -> str:
            """Stand in for a cache entry the process cannot read."""
            raise PermissionError("unreadable")

        monkeypatch.setattr(installer, "_download", fetch)
        monkeypatch.setattr(installer, "_digest", unreadable)

        with pytest.raises(PermissionError):
            installer.checked_download(
                "https://example.invalid/asset", archive, "0" * 64
            )

        assert not downloads, "an unreadable cache must not start a download"
        assert archive.read_bytes() == b"cached archive", (
            "an unreadable cache must be left in place"
        )

    def test_a_missing_cache_entry_is_a_miss_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An absent cache entry downloads normally instead of raising."""
        archive = tmp_path / "binary.tar.gz"
        content = b"fresh archive"
        digest = hashlib.sha256(content).hexdigest()

        def fetch(url: str, destination: Path) -> None:
            """Supply a fresh release body without network access."""
            destination.write_bytes(content)

        monkeypatch.setattr(installer, "_download", fetch)
        installer.checked_download("https://example.invalid/asset", archive, digest)
        assert archive.read_bytes() == content, "the missing entry was not installed"

    def test_plain_http_is_rejected(self, tmp_path: Path) -> None:
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
        self,
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
            installer.http.client,
            "HTTPSConnection",
            lambda *_args, **_kwargs: connection,
        )
        with pytest.raises(ValueError, match=message):
            installer._download("https://example.invalid/asset", tmp_path / "binary")
        connection.close.assert_called()
        assert not (tmp_path / "binary").exists(), "rejected redirect created a binary"

    def test_main_installs_only_the_pinned_kani_frontend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The installer extracts the two approved executables and nothing else."""
        _seed_kani_pin(tmp_path)
        frontend = _gzip_tar(KANI_FRONTEND_MEMBERS)
        bundle = _gzip_tar(KANI_BUNDLE_MEMBERS)
        assets = {installer.FRONTEND_URL: frontend, installer.BUNDLE_URL: bundle}
        requested = _serve_assets(monkeypatch, assets)
        monkeypatch.setenv(ROOT_ENV, str(tmp_path))

        installer.main()

        # Both pinned archives were fetched, in order, from their release URLs.
        assert requested == [installer.FRONTEND_URL, installer.BUNDLE_URL], (
            "the installer must fetch the pinned frontend and bundle, and nothing else"
        )
        cache = tmp_path / ".cache/boundary-kani"
        assert (cache / installer.FRONTEND_NAME).read_bytes() == frontend, (
            "the verified frontend archive must be cached unchanged"
        )
        assert (cache / installer.BUNDLE_NAME).read_bytes() == bundle, (
            "the verified bundle archive must be cached unchanged"
        )
        bin_dir = cache / "bin"
        assert sorted(path.name for path in bin_dir.iterdir()) == [
            "cargo-kani",
            "kani",
        ], "only the two pinned Kani executables may be extracted"
        for name, content in KANI_FRONTEND_MEMBERS.items():
            assert (bin_dir / name).read_bytes() == content, (
                f"the extracted {name} must be the archived member, byte for byte"
            )

    def test_main_installs_the_z3_solver_executable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The solver lands in the isolated cache with its exact bytes and mode."""
        content = b"z3 solver executable"
        members = {
            f"{z3_installer.NAME}/bin/z3": content,
            f"{z3_installer.NAME}/LICENSE.txt": b"z3 license",
        }
        archive = _zip_archive(members)
        requested = _serve_assets(monkeypatch, {z3_installer.URL: archive})
        monkeypatch.setenv(ROOT_ENV, str(tmp_path))

        z3_installer.main()

        assert requested == [z3_installer.URL], (
            "the installer must fetch only the pinned solver release"
        )
        cache = tmp_path / ".cache/boundary-z3"
        assert (cache / f"{z3_installer.NAME}.zip").read_bytes() == archive, (
            "the verified solver archive must be cached unchanged"
        )
        binary = cache / "z3"
        assert binary.read_bytes() == content, (
            "the solver binary must be the archived member, byte for byte"
        )
        assert binary.stat().st_mode & 0o777 == 0o755, "the solver must be executable"

    def test_main_rejects_a_stale_kani_version_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pin that disagrees with the packaged digests downloads nothing."""
        _seed_kani_pin(tmp_path, version="0.0.0-not-pinned")
        requested = _serve_assets(
            monkeypatch,
            {
                installer.FRONTEND_URL: _gzip_tar(KANI_FRONTEND_MEMBERS),
                installer.BUNDLE_URL: _gzip_tar(KANI_BUNDLE_MEMBERS),
            },
        )
        monkeypatch.setenv(ROOT_ENV, str(tmp_path))

        with pytest.raises(ValueError, match="update Kani archive names and digests"):
            installer.main()

        assert not requested, "a rejected version pin must not fetch any archive"
        assert not (tmp_path / ".cache").exists(), (
            "a rejected version pin must not create the installer cache"
        )

    def test_main_rejects_a_truncated_kani_frontend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A truncated archive fails loudly instead of installing part of a tool."""
        _seed_kani_pin(tmp_path)
        frontend = _gzip_tar(KANI_FRONTEND_MEMBERS)
        _serve_assets(
            monkeypatch,
            {
                # Truncating the compressed stream mid-archive stands in for a
                # release body that verified yet cannot be unpacked.
                installer.FRONTEND_URL: frontend[: len(frontend) // 2],
                installer.BUNDLE_URL: _gzip_tar(KANI_BUNDLE_MEMBERS),
            },
        )
        monkeypatch.setenv(ROOT_ENV, str(tmp_path))

        # Which layer reports the truncation is an implementation detail of the
        # standard library: the tar reader raises `ReadError` when it notices,
        # and the gzip reader raises `EOFError` mid-stream before it does.
        with pytest.raises((tarfile.ReadError, EOFError)):
            installer.main()

        bin_dir = tmp_path / ".cache/boundary-kani/bin"
        assert not any(bin_dir.iterdir()), (
            "a truncated archive must not install a partial frontend"
        )
