"""HTTP client for the Wildberries storefront.

Three kinds of hosts, three policies:

- ``www.wildberries.ru/__internal/*`` — search and live product cards. They
  require the anti-bot token (see ``antibot``) and WB throttles them hard, so
  calls are serialised and spaced a few seconds apart. A revoked token
  (HTTP 498, or 403 from the ``wbaas`` edge) is re-minted once per call.
- The CDN (``cdn.wbbasket.ru`` route map, ``basket-NN`` hosts) — static card
  JSON and price history, no token.
- ``feedbacks*.wb.ru`` — reviews, no token.

Every response from the storefront API is sanity-checked before it is used:
under suspicion WB can answer with a plausible-looking but unrelated result
set (observed: sneakers for the query "axe", tagged with another region).
Such answers are retried with a fresh token and, if they persist, turned into
an error rather than passed on as data.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests

from . import antibot, parse

log = logging.getLogger(__name__)

SITE = "https://www.wildberries.ru"
SEARCH_URL = f"{SITE}/__internal/u-search/exactmatch/ru/common/v18/search"
CARD_URL = f"{SITE}/__internal/u-card/cards/v4/detail"
GEO_URL = "https://user-geo-data.wildberries.ru/get-geo-info"
ROUTES_URL = "https://cdn.wbbasket.ru/api/v3/upstreams"
FEEDBACK_HOST_URL = "https://feedback-bt.wildberries.ru/feedback/api/v2/host"
FEEDBACK_FALLBACK_HOSTS = ("https://feedbacks1.wb.ru", "https://feedbacks2.wb.ru")
SELLER_URL = "https://static-basket-01.wbbasket.ru/vol0/data/supplier-by-id/{}.json"
CART_SYNC_URL = f"{SITE}/__internal/cart-storage-api/api/basket/sync"

MOSCOW_DEST = -1257786
ROUTES_TTL = 6 * 3600
CARD_BATCH = 50

SORTS = {
    "popular": "popular",
    "rating": "rate",
    "price_asc": "priceup",
    "price_desc": "pricedown",
    "newest": "newly",
    "benefit": "benefit",
}


class WildberriesError(RuntimeError):
    """Anything that stops a tool from returning trustworthy data."""


class RateLimited(WildberriesError):
    pass


class Blocked(WildberriesError):
    pass


class Unauthorized(WildberriesError):
    """The account token was rejected (HTTP 401)."""


def cache_dir() -> Path:
    root = os.environ.get("WB_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "wildberries-mcp"
    )
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


class WildberriesClient:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proxy = os.environ.get("WB_PROXY") or None
        self._headless = os.environ.get("WB_HEADLESS", "1") != "0"
        self._min_interval = float(os.environ.get("WB_MIN_INTERVAL", "3.0"))
        self._session_file = cache_dir() / "session.json"
        self._browser: antibot.BrowserSession | None = self._load_browser_session()
        self._site: requests.Session | None = None
        self._last_site_call = 0.0

        self._cdn = requests.Session()
        self._cdn.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        if self._proxy:
            self._cdn.proxies.update({"http": self._proxy, "https": self._proxy})
        self._routes: dict | None = None
        self._routes_at = 0.0
        self._dest: tuple[int, str] | None = None

    # ---- region ----------------------------------------------------------

    def region(self) -> tuple[int, str]:
        """WB's delivery region id ("dest") and where it came from.

        Prices, stock and delivery estimates depend on it. By default it is
        whatever WB assigns to this machine's IP — the same region a person
        opening the site from here would get.
        """
        if self._dest is None:
            override = os.environ.get("WB_DEST")
            if override:
                self._dest = (int(override), "WB_DEST environment variable")
            else:
                self._dest = self._detect_region()
        return self._dest

    def use_account_region(self, dest: int) -> None:
        """In account mode the account's own delivery address wins over the
        IP-based guess (an explicit WB_DEST still wins over both)."""
        if not os.environ.get("WB_DEST"):
            self._dest = (dest, "your Wildberries account's delivery address")

    def _detect_region(self) -> tuple[int, str]:
        try:
            geo = self._cdn.get(GEO_URL, params={"currency": "RUB", "locale": "ru", "dt": 0}, timeout=10).json()
            match = re.search(r"dest=(-?\d+)", geo.get("xinfo") or "")
            if match:
                return int(match.group(1)), f"auto-detected from IP ({geo.get('address') or 'unknown place'})"
        except (requests.RequestException, ValueError) as e:
            log.warning("WB region auto-detection failed: %s", e)
        return MOSCOW_DEST, "default (Moscow) — auto-detection failed"

    # ---- anti-bot session ------------------------------------------------

    def _load_browser_session(self) -> antibot.BrowserSession | None:
        try:
            return antibot.BrowserSession.from_dict(json.loads(self._session_file.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def _mint(self) -> None:
        log.info("Minting a Wildberries anti-bot token (headless=%s)", self._headless)
        try:
            self._browser = antibot.mint(proxy=self._proxy, headless=self._headless)
        except antibot.AntibotError as e:
            raise Blocked(str(e)) from e
        self._site = None
        try:
            self._session_file.write_text(json.dumps(self._browser.to_dict()))
            self._session_file.chmod(0o600)
        except OSError as e:
            log.warning("Could not persist WB session: %s", e)

    def _site_session(self) -> requests.Session:
        if self._browser is None:
            self._mint()
        if self._site is None:
            b = self._browser
            s = requests.Session()
            s.headers.update({
                "Accept": "*/*",
                "Accept-Language": "ru-RU",
                "User-Agent": b.user_agent,
                "Referer": SITE + "/",
                "X-Requested-With": "XMLHttpRequest",
                "deviceid": b.device_id,
                "x-userid": "0",
                "Sec-CH-UA": b.brands,
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": f'"{b.platform}"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            })
            for name, value in b.cookies.items():
                s.cookies.set(name, value, domain="www.wildberries.ru")
            if self._proxy:
                s.proxies.update({"http": self._proxy, "https": self._proxy})
            self._site = s
        return self._site

    def _pace(self) -> None:
        wait = self._min_interval + random.uniform(0, 0.7) - (time.monotonic() - self._last_site_call)
        if wait > 0:
            time.sleep(wait)

    def _site_get(self, url: str, params: dict, expect: str) -> dict:
        return self._site_request("GET", url, params, expect)

    def _site_request(self, method: str, url: str, params: dict, expect: str,
                      body: Any = None, headers: dict | None = None) -> dict:
        """Call a storefront API endpoint and return a validated JSON body."""
        with self._lock:
            reminted = False
            anomalies = 0
            throttles = 0
            empty_seen = False
            while True:
                session = self._site_session()
                self._pace()
                try:
                    resp = session.request(method, url, params=params, json=body, headers=headers, timeout=25)
                except requests.RequestException as e:
                    raise WildberriesError(f"Network error talking to Wildberries: {e}") from e
                finally:
                    self._last_site_call = time.monotonic()

                if resp.status_code == 498 or (
                    resp.status_code == 403 and "wbaas" in resp.headers.get("server", "")
                ):
                    if reminted:
                        raise Blocked(
                            "Wildberries rejects even a freshly issued anti-bot token "
                            f"(HTTP {resp.status_code}). This usually means the IP is "
                            "blocked (VPN, foreign or datacenter address)."
                        )
                    log.info("WB token rejected (HTTP %s), minting a new one", resp.status_code)
                    self._mint()
                    reminted = True
                    continue
                if resp.status_code == 401:
                    raise Unauthorized("Wildberries rejected the account token (HTTP 401)")
                if resp.status_code == 429:
                    throttles += 1
                    if throttles > 2:
                        raise RateLimited(
                            "Wildberries is rate limiting this IP (HTTP 429). Wait a few "
                            "minutes before searching again; retrying in a loop makes it worse."
                        )
                    time.sleep(15 * throttles)
                    continue
                if resp.status_code != 200:
                    raise WildberriesError(f"Wildberries answered HTTP {resp.status_code} for {url}")

                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if expect == "products" and _empty_search(data):
                    # WB's "nothing found" answer. Throttled requests have been
                    # seen to come back without products too, so a single empty
                    # answer is not trusted: confirm it once before reporting 0.
                    if empty_seen:
                        return {"products": [], "total": 0, "empty_confirmed": True}
                    empty_seen = True
                    time.sleep(8)
                    continue
                problem = _implausible(data, expect, params.get("dest"))
                if problem is None:
                    return _unwrap(data)
                anomalies += 1
                log.warning("Implausible WB response (%s), attempt %d", problem, anomalies)
                if anomalies == 1:
                    time.sleep(8)
                elif anomalies == 2 and not reminted:
                    self._mint()
                    reminted = True
                else:
                    raise WildberriesError(
                        f"Wildberries returned an implausible response ({problem}) "
                        "and it did not go away on retry; not passing it on as data."
                    )

    # ---- storefront API ----------------------------------------------------

    def search(self, query: str, page: int, sort: str, price_min: int | None,
               price_max: int | None) -> dict:
        dest, _ = self.region()
        params: dict[str, Any] = {
            "ab_testing": "false", "appType": 1, "curr": "rub", "dest": dest,
            "lang": "ru", "locale": "ru", "page": page, "query": query,
            "resultset": "catalog", "sort": SORTS[sort], "spp": 30,
            "suppressSpellcheck": "false",
        }
        if price_min is not None or price_max is not None:
            low = max(0, price_min or 0) * 100
            high = (price_max if price_max is not None else 10_000_000) * 100
            params["priceU"] = f"{low};{high}"
        return self._site_get(SEARCH_URL, params, expect="products")

    def cards(self, articles: list[int]) -> dict[int, dict]:
        """Live offers (price, stock, rating, seller) keyed by article."""
        dest, _ = self.region()
        found: dict[int, dict] = {}
        for i in range(0, len(articles), CARD_BATCH):
            batch = articles[i:i + CARD_BATCH]
            data = self._site_get(CARD_URL, {
                "appType": 1, "curr": "rub", "dest": dest, "spp": 30, "lang": "ru",
                "ab_testing": "false", "nm": ";".join(str(a) for a in batch),
            }, expect="products")
            for p in data.get("products") or []:
                found[p.get("id")] = p
        return found

    # ---- account cart ------------------------------------------------------

    def cart_sync(self, token: str, device_id: str, ts: int, ops: list[dict], full: bool = False) -> dict:
        """The site's cart endpoint: apply `ops` and return changes since `ts`.

        With `full` (the site's `remember_me=true`) and ts=0 it returns the
        whole cart, which is how a freshly logged-in browser loads it.
        """
        params: dict[str, Any] = {"ts": ts, "device_id": device_id}
        if full:
            params["remember_me"] = "true"
        return self._site_request(
            "POST", CART_SYNC_URL, params, expect="state", body=ops,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    # ---- CDN ---------------------------------------------------------------

    def _cdn_get(self, url: str, params: dict | None = None) -> requests.Response | None:
        for attempt in range(3):
            try:
                resp = self._cdn.get(url, params=params, timeout=15)
            except requests.RequestException as e:
                if attempt == 2:
                    raise WildberriesError(f"Network error fetching {url}: {e}") from e
                time.sleep(1 + attempt)
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 + 2 * attempt)
                continue
            if resp.status_code != 200:
                raise WildberriesError(f"HTTP {resp.status_code} from {url}")
            return resp
        return None

    def _routes_map(self, refresh: bool = False) -> dict:
        stale = time.time() - self._routes_at > ROUTES_TTL
        if self._routes is None or stale or refresh:
            resp = self._cdn_get(ROUTES_URL)
            if resp is None:
                raise WildberriesError("Wildberries CDN route map is unavailable")
            self._routes, self._routes_at = resp.json(), time.time()
        return self._routes

    def _basket(self, article: int) -> str:
        vol, part = article // 100000, article // 1000
        host = parse.basket_host(self._routes_map(), vol)
        if host is None and time.time() - self._routes_at > 60:
            host = parse.basket_host(self._routes_map(refresh=True), vol)
        if host is None:
            raise WildberriesError(
                f"No Wildberries CDN host serves article {article} (vol {vol}); "
                "the article number is probably wrong"
            )
        return f"https://{host}/vol{vol}/part{part}/{article}/info"

    def static_card(self, article: int) -> dict | None:
        resp = self._cdn_get(f"{self._basket(article)}/ru/card.json")
        return resp.json() if resp is not None else None

    def price_history(self, article: int) -> list[dict]:
        resp = self._cdn_get(f"{self._basket(article)}/price-history.json")
        return resp.json() if resp is not None else []

    def seller(self, seller_id: int) -> dict | None:
        resp = self._cdn_get(SELLER_URL.format(seller_id))
        return resp.json() if resp is not None else None

    # ---- reviews -------------------------------------------------------------

    def feedbacks(self, group_id: int) -> dict:
        hosts: list[str] = []
        resp = self._cdn_get(FEEDBACK_HOST_URL, params={"imt": group_id})
        if resp is not None:
            try:
                hosts = [h.rstrip("/") for h in resp.json() if isinstance(h, str)]
            except ValueError:
                pass
        for host in [*hosts, *FEEDBACK_FALLBACK_HOSTS]:
            resp = self._cdn_get(f"{host}/feedbacks/v2/{group_id}")
            if resp is not None:
                data = resp.json()
                if data.get("feedbackCount") is not None or data.get("feedbacks") is not None:
                    return data
        raise WildberriesError(f"Reviews for product group {group_id} are unavailable")


def _unwrap(data: dict) -> dict:
    """Some WB backends wrap the payload in {"data": {...}}; flatten it."""
    inner = data.get("data")
    if isinstance(inner, dict) and "products" in inner and "products" not in data:
        return {**inner, "total": data.get("total", inner.get("total")), "metadata": data.get("metadata")}
    return data


def _empty_search(data: Any) -> bool:
    """WB's answer to a query with no matches: query analysis, no products."""
    return (
        isinstance(data, dict)
        and "search_result" in data
        and "products" not in data
        and not isinstance(data.get("data"), dict)
    )


def _implausible(data: Any, expect: str, dest: Any) -> str | None:
    if not isinstance(data, dict):
        return "body is not a JSON object"
    echoed = (data.get("params") or {}).get("dest")
    if echoed is not None and dest is not None and str(echoed) != str(dest):
        return f"answer is for region {echoed}, not the requested {dest}"
    body = _unwrap(data)
    if expect not in body and body.get("total") != 0:
        return f"no '{expect}' field"
    return None
