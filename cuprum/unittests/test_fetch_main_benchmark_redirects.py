"""Properties and examples for benchmark artefact redirect policy.

The redirect argument binder accepts every valid positional-or-keyword split
of urllib's five callback arguments, while rejecting malformed call shapes and
values that violate its runtime type contract. These properties cover those
open-ended input domains; the finite tests retain readable protocol examples.
"""

from __future__ import annotations

import http.client
import io
import typing as typ
import urllib.request
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from benchmarks._github_http import (
    _ArtefactArchiveRedirectHandler,
    _download_bytes,
    _load_json_response,
    _redirect_request_arguments,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


_REDIRECT_CODES = st.sampled_from((301, 302, 303, 307, 308))
_REDIRECT_TEXT = st.text(max_size=64)
_REDIRECT_HEADERS = st.builds(http.client.HTTPMessage)
_REDIRECT_FILE_POINTERS = st.builds(io.BytesIO, st.binary(max_size=256))
_REDIRECT_POSITIONAL_COUNTS = st.integers(min_value=0, max_value=5)
_INVALID_REDIRECT_FIELD_VALUES = st.one_of(
    st.tuples(st.just("code"), st.one_of(st.booleans(), st.none(), _REDIRECT_TEXT)),
    st.tuples(
        st.just("msg"),
        st.one_of(st.none(), st.integers(), st.binary(max_size=64)),
    ),
    st.tuples(
        st.just("headers"),
        st.one_of(
            st.none(),
            st.integers(),
            st.dictionaries(_REDIRECT_TEXT, _REDIRECT_TEXT, max_size=4),
        ),
    ),
    st.tuples(
        st.just("newurl"),
        st.one_of(st.none(), st.integers(), st.binary(max_size=64)),
    ),
)
_MALFORMED_POSITIONAL_ARGUMENTS = st.one_of(
    st.lists(st.one_of(st.none(), st.integers(), _REDIRECT_TEXT), max_size=4),
    st.lists(
        st.one_of(st.none(), st.integers(), _REDIRECT_TEXT), min_size=6, max_size=10
    ),
).map(tuple)


def _make_github_artefact_request() -> urllib.request.Request:
    """Build a GitHub artefact download request with standard auth headers."""
    return urllib.request.Request(
        "https://api.github.com/repos/leynos/cuprum/actions/artifacts/1/zip",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer token",
            "User-Agent": "cuprum-benchmark-ratchet",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


@pytest.mark.parametrize("loader", [_load_json_response, _download_bytes])
def test_authenticated_requests_require_https(
    loader: cabc.Callable[..., object],
) -> None:
    """Authenticated requests should reject plaintext HTTP targets."""
    with pytest.raises(ValueError, match="must use HTTPS"):
        loader(url="http://example.invalid/resource", token="".join(("sec", "ret")))


def test_archive_download_rejects_response_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archive downloads should stop once the configured limit is exceeded."""

    class _Response:
        """Return full chunks so the archive limit is exceeded."""

        def __enter__(self) -> _Response:
            """Return this response context."""
            return self

        def __exit__(self, *_: object) -> None:
            """Leave the response context without suppressing errors."""

        @staticmethod
        def read(size: int) -> bytes:
            """Return one full requested chunk."""
            return b"x" * size

    opener = mock.Mock()
    opener.open.return_value = _Response()
    monkeypatch.setattr(
        "benchmarks._github_http.urllib.request.build_opener",
        lambda *_: opener,
    )
    monkeypatch.setattr("benchmarks._github_http._MAX_ARCHIVE_BYTES", 1)
    monkeypatch.setattr("benchmarks._github_http._ARCHIVE_READ_CHUNK_BYTES", 2)

    with pytest.raises(ValueError, match="exceeds 1 bytes"):
        _download_bytes(
            url="https://example.invalid/archive",
            token="".join(("sec", "ret")),
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


@given(
    fp=_REDIRECT_FILE_POINTERS,
    code=_REDIRECT_CODES,
    msg=_REDIRECT_TEXT,
    headers=_REDIRECT_HEADERS,
    newurl=_REDIRECT_TEXT,
    positional_argument_count=_REDIRECT_POSITIONAL_COUNTS,
)
def test_redirect_arguments_bind_every_valid_mixed_call(
    fp: io.BytesIO,
    code: int,
    msg: str,
    headers: http.client.HTTPMessage,
    newurl: str,
    positional_argument_count: int,
) -> None:
    """Every valid positional-or-keyword split retains the supplied values."""
    values: tuple[object, ...] = (fp, code, msg, headers, newurl)
    names = ("fp", "code", "msg", "headers", "newurl")
    kwargs = dict(
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

    assert actual[0] is fp, "The response stream should be retained"
    assert actual[1] == code, "The redirect status code should be retained"
    assert actual[2] == msg, "The redirect message should be retained"
    assert actual[3] is headers, "The redirect headers should be retained"
    assert actual[4] == newurl, "The redirect URL should be retained"
    assert isinstance(actual[1], int), "The status code should remain an integer"
    assert isinstance(actual[2], str), "The message should remain a string"
    assert isinstance(actual[3], http.client.HTTPMessage), (
        "The headers should remain an HTTPMessage"
    )
    assert isinstance(actual[4], str), "The redirect URL should remain a string"


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


@given(field_and_value=_INVALID_REDIRECT_FIELD_VALUES)
def test_redirect_arguments_reject_every_generated_invalid_field_type(
    field_and_value: tuple[str, object],
) -> None:
    """Each type-checked redirect field rejects arbitrary invalid values."""
    field, invalid_value = field_and_value
    kwargs: dict[str, object] = {
        "fp": io.BytesIO(),
        "code": 302,
        "msg": "Found",
        "headers": http.client.HTTPMessage(),
        "newurl": "https://example.com/archive.zip",
    }
    kwargs[field] = invalid_value

    with pytest.raises(TypeError):
        _redirect_request_arguments((), kwargs)


@given(args=_MALFORMED_POSITIONAL_ARGUMENTS)
def test_redirect_arguments_reject_generated_malformed_arity(
    args: tuple[object, ...],
) -> None:
    """Incomplete and excessive positional calls always fail binding."""
    with pytest.raises(TypeError):
        _redirect_request_arguments(args, {})


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
def test_artefact_redirect_handler_header_policy(
    newurl: str,
    expected_headers: dict[str, str | None],
) -> None:
    """Redirect handler strips auth on cross-origin, preserves on same-origin."""
    handler = _ArtefactArchiveRedirectHandler()
    request = _make_github_artefact_request()

    redirected_request = handler.redirect_request(
        request,
        io.BytesIO(),
        code=302,
        msg="Found",
        headers=http.client.HTTPMessage(),
        newurl=newurl,
    )

    assert redirected_request is not None, "redirect policy should create a request"
    for header, expected_value in expected_headers.items():
        assert redirected_request.get_header(header) == expected_value, (
            f"Header {header!r}: expected {expected_value!r}"
        )
