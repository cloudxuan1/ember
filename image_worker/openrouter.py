"""OpenRouter image API (POST /api/v1/images). Only used when a job names a whitelisted model.

The key lives in this process only: never logged, stored, returned, or passed to the Codex child.
"""

import base64
import binascii
import io
import json
import re
import urllib.error
import urllib.request

from PIL import Image

from image_worker.models import API_BASE, request_body
from image_worker.runner import GenerationError, check_storage, reference_files, save_image

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
STATUS_ERRORS = {401: "openrouter_auth", 402: "openrouter_credit", 403: "openrouter_refused",
                 400: "openrouter_rejected", 404: "openrouter_rejected", 413: "openrouter_rejected",
                 422: "openrouter_rejected"}


def reference_url(path):
    """A data URL of a stored original, scaled to at most 2048 px so a request stays small."""
    with Image.open(path) as image:
        image.load()
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    image.thumbnail((2048, 2048))
    data = io.BytesIO()
    image.save(data, format="PNG")
    return "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()


class OpenRouterRunner:
    def __init__(self, store, key, timeout=420, base_url=API_BASE):
        self.store = store
        self.key = key or ""
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    def redact(self, text):
        text = str(text)
        if self.key:
            text = text.replace(self.key, "[已隐藏]")
        text = re.sub(r"sk-or-[A-Za-z0-9_-]+", "[已隐藏]", text)
        return " ".join(text.split())[:300]

    def call(self, method, path, body=None, timeout=None):
        request = urllib.request.Request(
            self.base_url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GenerationError("no_image")
        return json.loads(raw)

    def failure(self, status, payload):
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        code = STATUS_ERRORS.get(status, "openrouter_unavailable")
        return GenerationError(code, self.redact(message) if message else None)

    def remaining(self):
        """The key's own spending limit left, in USD. Purely informational: failure never fails the image."""
        try:
            value = self.call("GET", "/key", timeout=15)["data"]["limit_remaining"]
        except Exception:
            return None
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def generate(self, job, stop):
        if not self.key:
            raise GenerationError("openrouter_not_configured")
        check_storage(self.store)
        try:
            references = [reference_url(path) for path in reference_files(self.store, job)]
        except (OSError, ValueError, Image.DecompressionBombError):
            raise GenerationError("reference_missing") from None
        try:
            payload = self.call("POST", "/images", request_body(job["model"], job["prompt"], references))
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read(64 * 1024))
            except (OSError, ValueError):
                body = None
            raise self.failure(error.code, body) from None
        except TimeoutError:
            raise GenerationError("generation_timeout") from None
        except (urllib.error.URLError, OSError):
            raise GenerationError("openrouter_unavailable") from None
        except ValueError:
            raise GenerationError("no_image") from None
        data =payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                raise self.failure(payload["error"].get("code"), payload)
            raise GenerationError("no_image")
        encoded = data[0].get("b64_json")
        if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
            raise GenerationError("no_image")
        if encoded.startswith("data:"):
            encoded = encoded.partition(",")[2]
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise GenerationError("no_image") from None
        metadata = save_image(self.store, job["id"], io.BytesIO(raw))
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            metadata["cost_usd"] = float(cost)
        metadata["key_limit_remaining_usd"] = self.remaining()
        return metadata
