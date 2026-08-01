"""Unit tests for benchmark artifact redirect argument and header policy."""

from __future__ import annotations

import http.client
import io
import urllib.request

import pytest

from benchmarks._github_http import (
    _ArtifactArchiveRedirectHandler,
    _redirect_request_arguments,
)


def _make_github_artifact_request() -> urllib.request.Request:
    """Build a GitHub artifact download request with standard auth headers."""
    return urllib.request.Request(
        "https://api.github.com/repos/leynos/cuprum/actions/artifacts/1/zip",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer token",
            "User-Agent": "cuprum-benchmark-ratchet",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


@pytest.mark.parametrize("positional_argument_count", [0, 1, 2, 3, 4, 5])
def test_redirect_arguments_support_mixed_binding(
    positional_argument_count: int,
) -> None:
    """Redirect arguments should follow positional-or-keyword binding rules."""
    fp = io.BytesIO()
    headers = http.client.HTTPMessage()
    values: tuple[object, ...] = (
        fp,
        302,
        "Found",
        headers,
        "https://example.com/archive.zip",
    )
    names = ("fp", "code", "msg", "headers", "newurl")
    kwargs: dict[str, object] = dict(
        zip(
            names[positional_argument_count:],
            values[positional_argument_count:],
            strict=True,
        )
    )

    actual = _redirect_request_arguments(
        values[:positional_argument_count],
        kwargs,
    )

    assert actual == values, "Mixed redirect arguments were bound incorrectly"


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        pytest.param(
            (io.BytesIO(),),
            {"code": 302, "msg": "Found", "headers": http.client.HTTPMessage()},
            id="missing",
        ),
        pytest.param(
            (io.BytesIO(),),
            {
                "fp": io.BytesIO(),
                "code": 302,
                "msg": "Found",
                "headers": http.client.HTTPMessage(),
                "newurl": "https://example.com/archive.zip",
            },
            id="duplicate",
        ),
        pytest.param(
            (),
            {
                "fp": io.BytesIO(),
                "code": 302,
                "msg": "Found",
                "headers": http.client.HTTPMessage(),
                "newurl": "https://example.com/archive.zip",
                "unexpected": None,
            },
            id="unexpected",
        ),
        pytest.param(
            (
                io.BytesIO(),
                302,
                "Found",
                http.client.HTTPMessage(),
                "https://example.com/archive.zip",
                None,
            ),
            {},
            id="too-many-positional",
        ),
    ],
)
def test_redirect_arguments_reject_invalid_binding_shapes(
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    """Redirect argument binding should reject invalid Python call shapes."""
    with pytest.raises(TypeError):
        _redirect_request_arguments(args, kwargs)


@pytest.mark.parametrize(
    ("name", "invalid_value"),
    [
        pytest.param("code", "302", id="code-string"),
        pytest.param("code", True, id="code-bool"),
        pytest.param("msg", b"Found", id="message-bytes"),
        pytest.param("headers", {}, id="headers-mapping"),
        pytest.param("newurl", b"https://example.com", id="url-bytes"),
    ],
)
def test_redirect_arguments_reject_invalid_types(
    name: str,
    invalid_value: object,
) -> None:
    """Redirect argument binding should retain its runtime type contract."""
    kwargs: dict[str, object] = {
        "fp": io.BytesIO(),
        "code": 302,
        "msg": "Found",
        "headers": http.client.HTTPMessage(),
        "newurl": "https://example.com/archive.zip",
    }
    kwargs[name] = invalid_value

    with pytest.raises(TypeError):
        _redirect_request_arguments((), kwargs)


@pytest.mark.parametrize(
    ("newurl", "expected_headers"),
    [
        pytest.param(
            "https://api.github.com/repos/leynos/cuprum/actions/artifacts/2/zip",
            {
                "Authorization": "Bearer token",
                "X-github-api-version": "2022-11-28",
                "Accept": "application/vnd.github+json",
                "User-agent": "cuprum-benchmark-ratchet",
            },
            id="same-origin-preserves-auth",
        ),
        pytest.param(
            "https://pipelines.actions.githubusercontent.com/archive.zip?sig=abc",
            {
                "Authorization": None,
                "X-github-api-version": None,
                "Accept": "application/vnd.github+json",
                "User-agent": "cuprum-benchmark-ratchet",
            },
            id="cross-origin-strips-auth",
        ),
    ],
)
def test_artifact_redirect_handler_header_policy(
    newurl: str,
    expected_headers: dict[str, str | None],
) -> None:
    """Redirect handler strips auth on cross-origin, preserves on same-origin."""
    handler = _ArtifactArchiveRedirectHandler()
    request = _make_github_artifact_request()

    redirected_request = handler.redirect_request(
        request,
        io.BytesIO(),
        code=302,
        msg="Found",
        headers=http.client.HTTPMessage(),
        newurl=newurl,
    )

    assert redirected_request is not None
    for header, expected_value in expected_headers.items():
        assert redirected_request.get_header(header) == expected_value, (
            f"Header {header!r}: expected {expected_value!r}"
        )
