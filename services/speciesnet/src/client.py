"""HTTP client for the SpeciesNet AWS Lambda API.

The deployed Lambda exposes two endpoints behind API Gateway:

* ``GET /upload-url?filename=foo.jpg`` — returns ``{upload_url, image_id, key}``;
  the caller PUTs the JPEG bytes to the presigned ``upload_url``.
* ``GET /result?image_id=<uuid>`` — returns 202 ``{status: "processing"}``
  while inference is still running, or 200 with the prediction list when
  the result JSON has landed in S3.

Inference is async because cold starts on the cropped 200 MB SpeciesNet
model run 60-180s.  Callers must poll ``/result`` with a backoff.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests
from url_safety import UnsafeURLError, validate_external_url

logger = logging.getLogger(__name__)

# Strip "Bearer <token>" or "Authorization: Bearer <token>" substrings from
# upstream response bodies before they end up in stored / published error
# strings.  A misconfigured api_url that echoes the request (or a captive-
# portal page reflecting headers) could otherwise leak the operator's API
# token into the events page tooltip.
_TOKEN_LEAK_PATTERN = re.compile(
    r"(?i)(authorization\s*:\s*)?bearer\s+\S+",
)


def scrub_secrets(text: str) -> str:
    """Replace Bearer tokens in *text* with a redaction placeholder."""
    return _TOKEN_LEAK_PATTERN.sub("[redacted bearer]", text)


class SpeciesNetError(Exception):
    """Raised when the SpeciesNet API returns an error response."""


class SpeciesNetTimeout(Exception):
    """Raised when polling for a result exceeds the configured budget."""


@dataclass
class Prediction:
    """One row from the SpeciesNet ``predictions`` array."""

    common_name: str
    species: str
    genus: str
    family: str
    order: str
    class_: str
    score: float
    geofenced: bool

    @classmethod
    def from_dict(cls, d: dict) -> "Prediction":
        return cls(
            common_name=d.get("common_name", "") or "",
            species=d.get("species", "") or "",
            genus=d.get("genus", "") or "",
            family=d.get("family", "") or "",
            order=d.get("order", "") or "",
            class_=d.get("class", "") or "",
            score=float(d.get("score", 0.0) or 0.0),
            geofenced=bool(d.get("geofenced", False)),
        )


class SpeciesNetClient:
    """Thin wrapper over the SpeciesNet Lambda API."""

    def __init__(
        self,
        api_url: str,
        *,
        api_token: str = "",
        timeout: float = 15.0,
        poll_interval: float = 3.0,
        max_polls: int = 60,
    ) -> None:
        # Defence in depth: even though the API URL comes from config (and the
        # web layer validates it), ensure we never PUT/GET against a
        # loopback/private host that might be a misconfigured internal service.
        validate_external_url(api_url)
        self._base = api_url.rstrip("/")
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._max_polls = max_polls
        self._headers: dict[str, str] = {}
        if api_token:
            self._headers["Authorization"] = f"Bearer {api_token}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, image_bytes: bytes, filename: str = "crop.jpg") -> list[Prediction]:
        """Upload *image_bytes* and poll until predictions arrive.

        Raises ``SpeciesNetError`` on a non-success HTTP response and
        ``SpeciesNetTimeout`` when polling exceeds ``max_polls``.
        """
        upload = self._request_upload_url(filename)
        self._put_image(upload["upload_url"], image_bytes)
        result = self._poll_result(upload["image_id"])
        raw_preds = result.get("predictions") or []
        return [Prediction.from_dict(p) for p in raw_preds]

    # ------------------------------------------------------------------
    # Internals (broken out so tests can mock individual stages)
    # ------------------------------------------------------------------

    def _request_upload_url(self, filename: str) -> dict[str, Any]:
        resp = requests.get(
            f"{self._base}/upload-url",
            params={"filename": filename},
            headers=self._headers,
            timeout=self._timeout,
        )
        if not resp.ok:
            raise SpeciesNetError(f"/upload-url returned {resp.status_code}: {scrub_secrets(resp.text[:200])}")
        try:
            body = resp.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise SpeciesNetError(f"/upload-url returned non-JSON body: {exc}") from exc
        if not isinstance(body, dict) or "upload_url" not in body or "image_id" not in body:
            raise SpeciesNetError(f"/upload-url malformed response: {body!r}")
        return body

    def _put_image(self, upload_url: str, image_bytes: bytes) -> None:
        # Presigned S3 URLs must be PUT with the content-type that was
        # signed; the Lambda's /upload-url signs for image/jpeg.
        try:
            validate_external_url(upload_url)
        except UnsafeURLError as exc:
            raise SpeciesNetError(f"refusing to PUT to internal upload URL: {exc}") from exc
        resp = requests.put(
            upload_url,
            data=image_bytes,
            headers={"Content-Type": "image/jpeg"},
            timeout=self._timeout,
        )
        if not resp.ok:
            raise SpeciesNetError(f"S3 PUT returned {resp.status_code}: {scrub_secrets(resp.text[:200])}")

    def _poll_result(self, image_id: str) -> dict[str, Any]:
        for attempt in range(self._max_polls):
            resp = requests.get(
                f"{self._base}/result",
                params={"image_id": image_id},
                headers=self._headers,
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                try:
                    return resp.json()
                except (ValueError, requests.JSONDecodeError) as exc:
                    raise SpeciesNetError(
                        f"/result returned non-JSON body: {exc}",
                    ) from exc
            if resp.status_code == 202:
                logger.debug(
                    "SpeciesNet result not ready (attempt %d/%d) — sleeping %.1fs",
                    attempt + 1, self._max_polls, self._poll_interval,
                )
                time.sleep(self._poll_interval)
                continue
            raise SpeciesNetError(
                f"/result returned {resp.status_code}: {scrub_secrets(resp.text[:200])}"
            )
        raise SpeciesNetTimeout(
            f"SpeciesNet did not return a result for {image_id} after "
            f"{self._max_polls} polls ({self._max_polls * self._poll_interval:.0f}s)"
        )
