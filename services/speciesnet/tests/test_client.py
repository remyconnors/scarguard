"""Tests for SpeciesNetClient — upload-url, PUT, and result polling."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from client import (
    Prediction,
    SpeciesNetClient,
    SpeciesNetError,
    SpeciesNetTimeout,
    scrub_secrets,
)


def _resp(status: int, body: dict | None = None, text: str = "") -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.json.return_value = body or {}
    resp.text = text
    return resp


@pytest.fixture
def client():
    """SpeciesNetClient with url_safety bypassed for the lifetime of the test.

    Tests should not depend on DNS reachability — all HTTP traffic is
    mocked, but url_safety.validate_external_url calls getaddrinfo on
    bare hostnames.  We yield (rather than return) so the patch stays
    active throughout the test, including on PUT to the (also fake)
    upload URL.

    The one exception is the explicit "internal URL is rejected" test
    in TestPutImage, which patches the symbol back to the real
    function locally so the rejection logic actually runs.
    """
    with patch("client.validate_external_url"):
        yield SpeciesNetClient(
            "https://api.example.com",
            timeout=1.0,
            poll_interval=0.0,
            max_polls=3,
        )


class TestRequestUploadUrl:
    def test_success(self, client: SpeciesNetClient) -> None:
        with patch("client.requests.get") as get:
            get.return_value = _resp(200, {
                "upload_url": "https://s3.example.com/up?sig=x",
                "image_id": "abc-123",
                "key": "uploads/abc-123.jpg",
            })
            result = client._request_upload_url("foo.jpg")
        assert result["image_id"] == "abc-123"

    def test_error_status_raises(self, client: SpeciesNetClient) -> None:
        with patch("client.requests.get") as get:
            get.return_value = _resp(500, text="boom")
            with pytest.raises(SpeciesNetError, match="500"):
                client._request_upload_url("foo.jpg")

    def test_malformed_response_raises(self, client: SpeciesNetClient) -> None:
        with patch("client.requests.get") as get:
            get.return_value = _resp(200, {"only_image_id": "abc"})
            with pytest.raises(SpeciesNetError, match="malformed"):
                client._request_upload_url("foo.jpg")


class TestPutImage:
    def test_internal_url_rejected(self, client: SpeciesNetClient) -> None:
        # Run the real validator for this assertion — internal hosts must
        # be rejected.  Literal IPs short-circuit before any DNS lookup,
        # so this works without network access.
        from url_safety import validate_external_url
        with patch("client.validate_external_url", validate_external_url):
            with pytest.raises(SpeciesNetError, match="internal"):
                client._put_image("http://127.0.0.1/upload", b"jpegbytes")

    def test_success(self, client: SpeciesNetClient) -> None:
        with patch("client.requests.put") as put:
            put.return_value = _resp(200)
            client._put_image("https://s3.example.com/up?sig=x", b"jpeg")
        # Must send image/jpeg content-type
        kwargs = put.call_args.kwargs
        assert kwargs["headers"]["Content-Type"] == "image/jpeg"

    def test_error_raises(self, client: SpeciesNetClient) -> None:
        with patch("client.requests.put") as put:
            put.return_value = _resp(403, text="forbidden")
            with pytest.raises(SpeciesNetError, match="403"):
                client._put_image("https://s3.example.com/up?sig=x", b"jpeg")


class TestPollResult:
    def test_returns_on_first_200(self, client: SpeciesNetClient) -> None:
        body = {"predictions": [{"common_name": "American Crow", "score": 0.9}]}
        with patch("client.requests.get") as get:
            get.return_value = _resp(200, body)
            out = client._poll_result("img-1")
        assert out == body

    def test_polls_through_202s(self, client: SpeciesNetClient) -> None:
        responses = [_resp(202), _resp(202), _resp(200, {"predictions": []})]
        with patch("client.requests.get") as get:
            get.side_effect = responses
            out = client._poll_result("img-1")
        assert out == {"predictions": []}
        assert get.call_count == 3

    def test_max_polls_raises_timeout(self, client: SpeciesNetClient) -> None:
        with patch("client.requests.get") as get:
            get.return_value = _resp(202)
            with pytest.raises(SpeciesNetTimeout):
                client._poll_result("img-1")
        # Polled exactly max_polls times
        assert get.call_count == client._max_polls

    def test_unexpected_status_raises_error(self, client: SpeciesNetClient) -> None:
        with patch("client.requests.get") as get:
            get.return_value = _resp(500, text="oops")
            with pytest.raises(SpeciesNetError, match="500"):
                client._poll_result("img-1")


class TestClassify:
    def test_full_happy_path(self, client: SpeciesNetClient) -> None:
        upload_body = {"upload_url": "https://s3.example.com/up?sig=x", "image_id": "abc"}
        result_body = {"predictions": [{
            "common_name": "Great Blue Heron",
            "species": "Ardea herodias",
            "genus": "Ardea",
            "family": "Ardeidae",
            "order": "Pelecaniformes",
            "class": "Aves",
            "score": 0.87,
            "geofenced": False,
        }]}

        get_calls: list[Any] = []

        def fake_get(url: str, **kwargs: Any) -> Any:
            get_calls.append(url)
            if "/upload-url" in url:
                return _resp(200, upload_body)
            if "/result" in url:
                return _resp(200, result_body)
            raise AssertionError(f"unexpected GET {url}")

        with patch("client.requests.get", side_effect=fake_get), \
             patch("client.requests.put", return_value=_resp(200)):
            preds = client.classify(b"jpeg-bytes", filename="bird.jpg")

        assert len(preds) == 1
        assert isinstance(preds[0], Prediction)
        assert preds[0].common_name == "Great Blue Heron"
        assert preds[0].score == pytest.approx(0.87)


class TestPrediction:
    def test_from_dict_with_missing_fields(self) -> None:
        p = Prediction.from_dict({"common_name": "Fox", "score": 0.5})
        assert p.species == ""
        assert p.score == 0.5
        assert p.geofenced is False


class TestScrubSecrets:
    def test_strips_bare_bearer_token(self) -> None:
        cleaned = scrub_secrets("got 401 with Bearer abc.def.ghi from upstream")
        assert "abc.def.ghi" not in cleaned
        assert "[redacted bearer]" in cleaned

    def test_strips_authorization_header_form(self) -> None:
        cleaned = scrub_secrets("Authorization: Bearer secret-token")
        assert "secret-token" not in cleaned
        assert "[redacted bearer]" in cleaned

    def test_case_insensitive(self) -> None:
        cleaned = scrub_secrets("BEARER MySecretToken")
        assert "MySecretToken" not in cleaned

    def test_leaves_other_text_alone(self) -> None:
        assert scrub_secrets("HTTP 502 Bad Gateway") == "HTTP 502 Bad Gateway"

    def test_handles_empty_string(self) -> None:
        assert scrub_secrets("") == ""
