"""httpx-клиент с таймаутом, понятным User-Agent, паузой между запросами и ретраями."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import HttpConfig
from app.sources.base import SourceError

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class PoliteClient:
    def __init__(
        self,
        cfg: HttpConfig,
        *,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(
            timeout=cfg.timeout_seconds,
            headers={"User-Agent": cfg.user_agent, "Accept": "application/json", **(headers or {})},
            follow_redirects=True,
        )
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self.cfg.delay_seconds - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return (await self.get(url, params)).json()

    async def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        return (await self.get(url, params)).text

    async def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.retries + 1):
            await self._throttle()
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code in RETRY_STATUS:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                return resp
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
                if status and status not in RETRY_STATUS:
                    break  # 4xx — повтор не поможет
                if attempt < self.cfg.retries:
                    pause = 2 ** (attempt - 1)
                    log.warning("GET %s failed (%s), retry in %ss", url, exc, pause)
                    await asyncio.sleep(pause)
        raise SourceError(f"GET {url}: {last_exc}") from last_exc

    async def aclose(self) -> None:
        await self._client.aclose()
