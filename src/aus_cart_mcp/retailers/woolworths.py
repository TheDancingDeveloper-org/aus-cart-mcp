"""Woolworths (Australia): the endpoints its own website calls (verified 2026-10-02).

  POST /apis/ui/Search/products   product search
  GET  /apis/ui/products/<id,id,…> products by stockcode, batched (verified 2026-10-03, read-only)
  GET  /apis/ui/Trolley            trolley contents and totals
  POST /api/v3/ui/trolley/update   set absolute quantities (0 removes)
  GET  /api/ui/v2/bootstrap        ShopperRequest.IsGuest / FirstName: who is logged in
  GET  cdn0.woolworths.media/content/wowproductimages/medium/<stockcode>.jpg   product photo

Unofficial: Woolworths publishes no API for this and its site terms forbid
automated access. See docs/LEGAL.md before offering this to anyone else.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx

from aus_cart_mcp import config
from aus_cart_mcp.retailers.base import (
    Blocked,
    Cart,
    CartItem,
    Product,
    RetailerError,
    RetailerInfo,
    SessionRequired,
    Shopper,
)

MAX_ITEMS_PER_UPDATE = 30
# Ids per products-by-stockcode request. The site's own maximum is unknown; 20 is conservative.
MAX_IDS_PER_PRODUCTS_CALL = 20
IMAGE_BASE_URL = "https://cdn0.woolworths.media"
MAX_IMAGE_BYTES = 1_000_000
_API_HEADERS = {"Accept": "application/json, text/plain, */*"}


class Woolworths:
    info = RetailerInfo(
        key="woolworths",
        name="Woolworths",
        country="AU",
        status="supported",
        capabilities=("search", "products.read", "cart.read", "cart.write"),
        site="https://www.woolworths.com.au",
        login_url="https://www.woolworths.com.au/shop/securelogin",
        cookie_url="https://www.woolworths.com.au/",
        verified="2026-10-02",
    )
    base_url = "https://www.woolworths.com.au"
    # The site serves its data API only to browsers, so we send a stock mobile
    # browser user agent. That is the whole extent of it: no fingerprint spoofing,
    # no proxies, no challenge solving (docs/LEGAL.md).
    user_agent = (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Mobile Safari/537.36"
    )

    @property
    def key(self) -> str:
        return self.info.key

    def has_bot_cookies(self, cookies: dict[str, str]) -> bool:
        return any(k.startswith(("bm_", "_abck")) for k in cookies)

    async def warm(self, http: httpx.AsyncClient) -> None:
        try:
            await http.get("/")
        except httpx.HTTPError as exc:
            raise RetailerError(f"Woolworths unreachable: {exc}") from exc

    async def _json(self, http: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
        headers = {**_API_HEADERS, "Origin": self.base_url, "Referer": f"{self.base_url}/shop/cart"}
        try:
            response = await http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise RetailerError(f"Woolworths unreachable: {exc}") from exc
        if response.status_code == 401:
            raise SessionRequired("Woolworths refused the session; reconnect Woolworths")
        if response.status_code in (403, 429):
            raise Blocked(f"Woolworths is refusing requests (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise RetailerError(f"Woolworths returned HTTP {response.status_code}")
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise Blocked("Woolworths returned a web page instead of data (a bot challenge?)") from exc
        if not isinstance(data, dict):
            raise RetailerError("Woolworths returned an unexpected response shape")
        return data

    async def shopper(self, http: httpx.AsyncClient) -> Shopper:
        shopper = (await self._json(http, "GET", "/api/ui/v2/bootstrap")).get("ShopperRequest") or {}
        return Shopper(logged_in=not shopper.get("IsGuest", True), first_name=str(shopper.get("FirstName") or ""))

    async def search(self, http: httpx.AsyncClient, query: str, *, limit: int, specials_only: bool) -> list[Product]:
        body = {
            "searchTerm": query,
            "pageNumber": 1,
            "pageSize": max(1, min(limit, 36)),
            "sortType": "TraderRelevance",
            "location": f"/shop/search/products?searchTerm={quote(query)}",
            "formatObject": json.dumps({"name": query}),
            "isSpecial": specials_only,
            "isBundle": False,
            "isMobile": False,
            "filters": [],
            "groupEdmVariants": False,
        }
        data = await self._json(http, "POST", "/apis/ui/Search/products", json=body)
        out = [
            self._product(p)
            for group in data.get("Products") or []
            for p in group.get("Products") or []
            if p.get("Stockcode")
        ]
        return out[:limit]

    def _product(self, p: dict) -> Product:
        code = str(int(p["Stockcode"]))
        price, was = p.get("Price"), p.get("WasPrice")
        return Product(
            product_id=code,
            name=str(p.get("DisplayName") or p.get("Name") or ""),
            price=price,
            unit_price=str(p.get("CupString") or ""),
            size=str(p.get("PackageSize") or ""),
            available=bool(p.get("IsAvailable", True)),
            on_special=bool(p.get("IsOnSpecial", False)),
            url=f"{self.base_url}/shop/productdetails/{code}",
            image_url=str(p.get("MediumImageFile") or p.get("SmallImageFile") or self.image_url(code)),
            was_price=was if isinstance(was, int | float) else None,
        )

    async def products(self, http: httpx.AsyncClient, product_ids: list[str]) -> list[Product]:
        ids = list(dict.fromkeys(str(i) for i in product_ids))
        for product_id in ids:
            if not product_id.isdigit():
                raise RetailerError(f"'{product_id}' is not a Woolworths product id")
        out: list[Product] = []
        for start in range(0, len(ids), MAX_IDS_PER_PRODUCTS_CALL):
            chunk = ids[start : start + MAX_IDS_PER_PRODUCTS_CALL]
            data = await self._json_list(http, f"/apis/ui/products/{','.join(chunk)}")
            out += [self._product(p) for p in data if isinstance(p, dict) and p.get("Stockcode")]
        return out

    async def _json_list(self, http: httpx.AsyncClient, path: str) -> list:
        headers = {**_API_HEADERS, "Referer": f"{self.base_url}/"}
        try:
            response = await http.get(path, headers=headers)
        except httpx.HTTPError as exc:
            raise RetailerError(f"Woolworths unreachable: {exc}") from exc
        if response.status_code in (403, 429):
            raise Blocked(f"Woolworths is refusing requests (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise RetailerError(f"Woolworths returned HTTP {response.status_code}")
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise Blocked("Woolworths returned a web page instead of data (a bot challenge?)") from exc
        if not isinstance(data, list):
            raise RetailerError("Woolworths returned an unexpected response shape")
        return data

    async def cart(self, http: httpx.AsyncClient) -> Cart:
        data = await self._json(http, "GET", "/apis/ui/Trolley")
        totals = data.get("Totals") if isinstance(data.get("Totals"), dict) else {}
        items = [
            CartItem(
                product_id=str(int(i["Stockcode"])),
                name=str(i.get("DisplayName") or i.get("Name") or ""),
                quantity=float(i.get("QuantityInTrolley") or 0),
                price=i.get("SalePrice") if i.get("SalePrice") is not None else i.get("Price"),
            )
            for i in data.get("AvailableItems") or []
            if i.get("Stockcode")
        ]
        return Cart(
            items=items, subtotal=totals.get("SubTotal"), total=totals.get("Total"), savings=totals.get("TotalSavings")
        )

    def image_url(self, product_id: str) -> str:
        base = (config.image_base_url_override(self.key) or IMAGE_BASE_URL).rstrip("/")
        return f"{base}/content/wowproductimages/medium/{int(product_id):06d}.jpg"

    async def image(self, http: httpx.AsyncClient, product_id: str) -> tuple[bytes, str]:
        if not str(product_id).isdigit():
            raise RetailerError(f"'{product_id}' is not a Woolworths product id")
        headers = {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8", "Referer": f"{self.base_url}/"}
        try:
            response = await http.get(self.image_url(product_id), headers=headers)
        except httpx.HTTPError as exc:
            raise RetailerError(f"Woolworths image unreachable: {exc}") from exc
        if response.status_code in (403, 404, 429):
            # The image CDN is not the data API: a refused photo must not trip the breaker for everything else.
            raise RetailerError(f"Woolworths has no photo for {product_id} (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise RetailerError(f"Woolworths returned HTTP {response.status_code} for the photo")
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            raise RetailerError("Woolworths returned something that is not an image")
        if len(response.content) > MAX_IMAGE_BYTES:
            raise RetailerError("the photo is too large")
        return response.content, content_type

    async def set_quantities(self, http: httpx.AsyncClient, quantities: dict[str, float]) -> None:
        if not quantities:
            return
        if len(quantities) > MAX_ITEMS_PER_UPDATE:
            raise RetailerError(f"at most {MAX_ITEMS_PER_UPDATE} products per update")
        items = []
        for product_id, quantity in quantities.items():
            if not str(product_id).isdigit():
                raise RetailerError(f"'{product_id}' is not a Woolworths product id")
            items.append(
                {
                    "stockcode": int(product_id),
                    "quantity": max(0, quantity),
                    "source": "ProductDetail",
                    "diagnostics": "0",
                    "searchTerm": None,
                    "evaluateRewardPoints": False,
                    "offerId": None,
                    "profileId": None,
                    "priceLevel": None,
                }
            )
        data = await self._json(http, "POST", "/api/v3/ui/trolley/update", json={"items": items})
        if data.get("Message"):
            raise RetailerError(f"Woolworths: {data['Message']}")
