"""
OpenRouter Chat Completions client.

- Uses the official /chat/completions endpoint.
- Retries temporary failures (timeouts, 429, 5xx) with exponential backoff.
- Fails fast on permanent errors (bad key, bad model).
- Raises AIError so callers can show a friendly fallback.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from config import Settings

logger = logging.getLogger(__name__)

# HTTP codes that are worth retrying
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class AIError(Exception):
    """The AI provider could not produce a usable reply."""


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter: about 2s, 4s, 8s, capped at 20s."""
    return min(2**attempt, 20) + random.uniform(0, 1)


class OpenRouterClient:
    """Async client for OpenRouter. Create once and reuse."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._url = f"{settings.openrouter_base_url.rstrip('/')}/chat/completions"
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, connect=10.0),
            headers={
                # The key lives only in this header; it is never logged
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": settings.openrouter_site_url,
                "X-Title": settings.openrouter_app_name,
            },
        )

    async def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._http.aclose()

    async def chat(self, messages: list[dict[str, str]]) -> str:
        """Send messages and return the assistant's reply text."""
        payload: dict[str, Any] = {
            "model": self._settings.openrouter_model,
            "messages": messages,
            "temperature": self._settings.openrouter_temperature,
            "max_tokens": self._settings.openrouter_max_tokens,
        }

        attempts = self._settings.max_retries + 1
        reason = "unknown"

        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.post(self._url, json=payload)
            except httpx.HTTPError as exc:  # timeouts and network failures
                reason = type(exc).__name__
                logger.warning("OpenRouter network error (attempt %d/%d): %s", attempt, attempts, reason)
            else:
                if response.status_code == 200:
                    return self._extract_text(response)

                if response.status_code not in RETRYABLE_STATUS:
                    # Permanent problem (401 bad key, 404 bad model, 400 bad request)
                    logger.error(
                        "OpenRouter rejected the request: HTTP %s %s",
                        response.status_code,
                        response.text[:300],
                    )
                    raise AIError(f"OpenRouter returned HTTP {response.status_code}")

                reason = f"HTTP {response.status_code}"
                logger.warning("OpenRouter temporary error %s (attempt %d/%d)", reason, attempt, attempts)

            if attempt < attempts:
                await asyncio.sleep(_backoff(attempt))

        raise AIError(f"OpenRouter unavailable after {attempts} attempts ({reason})")

    @staticmethod
    def _extract_text(response: httpx.Response) -> str:
        """Pull the reply text out of a successful response, validating its shape."""
        try:
            data = response.json()
        except ValueError as exc:
            raise AIError("OpenRouter returned invalid JSON") from exc

        # OpenRouter can return an error object even with HTTP 200
        if isinstance(data, dict) and data.get("error"):
            raise AIError(f"OpenRouter error object: {str(data['error'])[:200]}")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError("OpenRouter response had an unexpected shape") from exc

        if not isinstance(content, str) or not content.strip():
            raise AIError("OpenRouter returned an empty reply")
        return content.strip()