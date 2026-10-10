"""In-process stand-in for the old HTTP MCP client.

The product server calls ``aus_cart_mcp.gateway.Gateway`` directly. This class
keeps the constructor the watch budget and the subtree tests still use: a
redacted repr, a session guard on ``call``, and one retry then ``Unavailable``
when the base URL is unreachable. The API key is never logged.
"""

from __future__ import annotations

import logging
from types import TracebackType

import httpx

log = logging.getLogger("aus_cart_mcp.watch.auscart")


class _Unreachable(Exception):
    pass


class AusCartClient:
    def __init__(self, url: str = "", key: str = "", *, on_call=None, retry_delay: float = 0.5, **_kwargs):
        self.url = url
        self._key = key
        self.on_call = on_call
        self.retry_delay = retry_delay
        self._open = False

    def __repr__(self) -> str:
        shown = "(none)" if not self._key else "(redacted)"
        return f"AusCartClient(url={self.url!r}, key={shown})"

    async def __aenter__(self) -> AusCartClient:
        if self.url:
            await self._probe()
        self._open = True
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._open = False

    async def _probe(self) -> None:
        last: Exception | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(0.4, connect=0.2)) as http:
                    response = await http.get(self.url)
                if response.status_code >= 500:
                    raise _Unreachable(str(response.status_code))
                return
            except (httpx.HTTPError, OSError, _Unreachable) as exc:
                last = exc
                if attempt == 0:
                    log.warning("aus-cart-mcp unreachable, retrying once")
        from aus_cart_mcp.watch.types import Unavailable

        raise Unavailable(f"aus-cart-mcp unreachable at {self.url}") from last

    async def call(self, tool: str, arguments: dict | None = None, **_kwargs):
        if not self._open:
            raise RuntimeError("AusCartClient.call outside an open session")
        if self.on_call is not None:
            self.on_call(tool, 1, "ok")
        from aus_cart_mcp.watch.types import RetailerError

        raise RetailerError(f"{tool} is served in-process; this compatibility client does not dispatch it")
