"""Minimal Anthropic Messages API client.

Deliberately dependency-light (``httpx`` only) so the whole backend stays small.
Every call has a timeout, retries with backoff on 429/5xx, and raises
``LLMUnavailable`` - callers always have a non-LLM fallback path.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

import httpx

from .config import settings
from .errors import LLMUnavailable, log_event


def is_enabled() -> bool:
    return settings.llm_enabled


class LLMClient:
    def __init__(self) -> None:
        self._client: httpx.Client | None = None

    # -- internals -------------------------------------------------------- #
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=settings.anthropic_base_url,
                timeout=settings.llm_timeout_seconds,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": settings.anthropic_version,
            "content-type": "application/json",
        }

    # -- public API ------------------------------------------------------- #
    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        json_prefill: bool = False,
    ) -> str:
        """One request/response round trip. Raises ``LLMUnavailable`` on failure."""
        if not is_enabled():
            raise LLMUnavailable("no_api_key")

        payload_messages = list(messages)
        if json_prefill:
            # Prefilling the assistant turn is the cheapest reliable way to get
            # bare JSON back out of the model.
            payload_messages.append({"role": "assistant", "content": "{"})

        body = {
            "model": model or settings.anthropic_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": payload_messages,
        }

        last_error = "unknown"
        for attempt in range(settings.llm_max_retries + 1):
            started = time.monotonic()
            try:
                response = self.client.post(
                    "/v1/messages", headers=self._headers(), json=body
                )
            except httpx.TimeoutException:
                last_error = "timeout"
            except httpx.HTTPError as exc:  # network/DNS/TLS
                last_error = f"network_error:{type(exc).__name__}"
            else:
                if response.status_code == 200:
                    data = response.json()
                    text = "".join(
                        block.get("text", "")
                        for block in data.get("content", [])
                        if block.get("type") == "text"
                    )
                    log_event(
                        "llm_call_ok",
                        model=body["model"],
                        seconds=round(time.monotonic() - started, 2),
                        attempts=attempt + 1,
                    )
                    return ("{" + text) if json_prefill else text
                if response.status_code in (429, 500, 502, 503, 529):
                    last_error = f"http_{response.status_code}"
                else:
                    # 4xx other than 429 will not be fixed by retrying.
                    log_event(
                        "llm_call_failed",
                        status=response.status_code,
                        body=response.text[:200],
                    )
                    raise LLMUnavailable(f"http_{response.status_code}")

            if attempt < settings.llm_max_retries:
                time.sleep(1.5 * (2**attempt))
            log_event("llm_call_retry", attempt=attempt + 1, error=last_error)

        log_event("llm_call_failed", error=last_error)
        raise LLMUnavailable(last_error)


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model response."""
    if text is None:
        raise ValueError("empty response")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to bracket matching for "here is the JSON: {...} thanks" replies.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        if start == -1:
            continue
        depth = 0
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return json.loads(cleaned[start : index + 1])
    raise ValueError("no JSON found in model response")


def image_block(media_type: str, raw: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(raw).decode("ascii"),
        },
    }


def document_block(raw: bytes) -> dict[str, Any]:
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(raw).decode("ascii"),
        },
    }


llm = LLMClient()
