"""Small retry helper for read-only ingest HTTP calls.

Transient failures (network blips, 429 rate limits, 5xx) are retried with
exponential backoff; everything else returns immediately for the caller to map
to its own error. Read-only GETs are safe to retry.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import httpx

logger = logging.getLogger("meter.ingest")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def http_get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    attempts: int = 3,
    base_delay: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """GET with retry on transient errors. Returns the final response."""
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            response = client.get(url, params=params, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt == attempts - 1:
                raise
            logger.warning("GET %s failed (%s); retry %d/%d", url, exc, attempt + 1, attempts - 1)
            sleep(base_delay * (2**attempt))
            continue

        if response.status_code in RETRYABLE_STATUS and attempt < attempts - 1:
            logger.warning(
                "GET %s -> %s; retry %d/%d", url, response.status_code, attempt + 1, attempts - 1
            )
            sleep(base_delay * (2**attempt))
            continue
        return response

    # Only reached if the loop exits without returning (shouldn't happen).
    assert last_exc is not None
    raise last_exc
