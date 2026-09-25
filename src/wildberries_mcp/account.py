"""Optional account mode: log in once in a visible browser, then use the
account's access token for the cart.

How WB sessions work (observed on the live site):
- Login is phone + one-time code on www.wildberries.ru. The site keeps the
  result in its own storage: ``localStorage.wbx__tokenData.token`` is a JWT
  access token (valid for ~30 days) sent as ``Authorization: Bearer``; the
  ``wbid-*`` cookies let the site renew it.
- So the login happens in a persistent Chromium profile the user types into
  themselves — the phone number and code never pass through the MCP client —
  and the server copies only the token, the device id and the delivery
  region out of it. Near expiry the same profile is opened headless and the
  site renews the token on its own.

The token grants access to the whole account, so it is stored with 0600
permissions and never returned by any tool.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import asdict, dataclass

from . import antibot
from .client import WildberriesError

log = logging.getLogger(__name__)

LOGIN_URL = "https://www.wildberries.ru/security/login"
HOME_URL = "https://www.wildberries.ru/"
REFRESH_BEFORE = 3 * 24 * 3600  # renew the token when less than 3 days are left
LOGIN_TIMEOUT = 600


class NotLoggedIn(WildberriesError):
    pass


@dataclass
class AccountSession:
    token: str
    token_expires: float
    device_id: str
    dest: int | None
    wallet_discount_percent: float | None
    updated_at: float

    def public(self) -> dict:
        """What tools may show: no token, no personal data."""
        return {
            "logged_in": True,
            "token_valid_until": time.strftime("%Y-%m-%d", time.localtime(self.token_expires)),
            "delivery_region_dest": self.dest,
            "wallet_discount_percent": self.wallet_discount_percent,
            "session_updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated_at)),
        }


class Account:
    def __init__(self, cache_dir, proxy: str | None = None) -> None:
        self._file = cache_dir / "account.json"
        self._profile = cache_dir / "profile"
        self._proxy = proxy
        self._lock = threading.Lock()  # one browser on the profile at a time
        self._session: AccountSession | None = self._load()

    # ---- persistence -------------------------------------------------------

    def _load(self) -> AccountSession | None:
        try:
            return AccountSession(**json.loads(self._file.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def _save(self, session: AccountSession) -> None:
        fd = os.open(self._file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(session), f)
        self._session = session

    # ---- public API ----------------------------------------------------------

    def current(self) -> AccountSession | None:
        """The stored session as is — no renewal, no browser."""
        return self._session

    def status(self) -> dict:
        if self._session is None:
            return {"logged_in": False, "how_to_log_in": "call account_login (opens a browser window)"}
        return self._session.public()

    def session(self) -> AccountSession:
        """A session with a usable token, renewing it near expiry."""
        if self._session is None:
            raise NotLoggedIn(
                "Not logged in to Wildberries. Call account_login: it opens a browser window "
                "where you sign in with your phone number yourself."
            )
        if self._session.token_expires - time.time() < REFRESH_BEFORE:
            self.refresh()
        return self._session

    def login(self) -> dict:
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise NotLoggedIn("account_login needs a desktop session to show the browser window (no DISPLAY).")
        with self._lock:
            session = self._with_profile(headless=False, url=LOGIN_URL, wait_for_login=LOGIN_TIMEOUT)
        self._save(session)
        return session.public()

    def refresh(self) -> None:
        """Let the site renew the token in the saved profile (headless)."""
        log.info("Renewing the Wildberries account token")
        with self._lock:
            session = self._with_profile(headless=True, url=HOME_URL, wait_for_login=20)
        self._save(session)

    def logout(self) -> dict:
        """Forget the local session. The site session itself is not revoked."""
        with self._lock:
            self._session = None
            try:
                self._file.unlink()
            except FileNotFoundError:
                pass
            shutil.rmtree(self._profile, ignore_errors=True)
        return {"logged_in": False, "note": "Local session and browser profile deleted. To end the session "
                "on WB's side as well, use 'Log out on all devices' in your account."}

    def forget_token(self) -> None:
        """The token was rejected (401): force a renewal on next use."""
        if self._session is not None:
            self._session.token_expires = 0

    # ---- browser -------------------------------------------------------------

    def _with_profile(self, headless: bool, url: str, wait_for_login: float) -> AccountSession:
        from playwright.sync_api import sync_playwright

        self._profile.mkdir(parents=True, exist_ok=True)
        self._profile.chmod(0o700)
        with sync_playwright() as pw:
            user_agent = antibot.real_user_agent(pw)
            ctx = pw.chromium.launch_persistent_context(
                str(self._profile), headless=headless, channel="chromium", locale="ru-RU",
                user_agent=user_agent, viewport={"width": 1280, "height": 900},
                proxy={"server": self._proxy} if self._proxy else None,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                _goto(page, url)
                deadline = time.time() + wait_for_login
                while not _logged_in(page):
                    if time.time() > deadline:
                        raise NotLoggedIn(
                            "Login was not completed in time." if not headless else
                            "The saved Wildberries login has expired. Call account_login again."
                        )
                    page.wait_for_timeout(1500)
                # Let the site finish its post-login sync (token, basket, region).
                _goto(page, HOME_URL)
                page.wait_for_timeout(5000)
                storage = page.evaluate("() => Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)]))")
                device_id = next((c["value"] for c in ctx.cookies() if c["name"] == "device_id"), None)
            finally:
                ctx.close()
        return _session_from_storage(storage, device_id)


def _goto(page, url: str) -> None:
    for attempt in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception as e:  # the site sometimes redirects mid-navigation
            log.debug("navigation to %s retried: %s", url, e)
            page.wait_for_timeout(2000)


def _logged_in(page) -> bool:
    try:
        state = page.evaluate("localStorage.getItem('_sys_auth')")
    except Exception:
        return False
    return bool(state) and state != "unauth"


def _session_from_storage(storage: dict, device_id: str | None) -> AccountSession:
    token = (json.loads(storage.get("wbx__tokenData") or "{}")).get("token")
    if not token:
        raise NotLoggedIn("Logged in, but the site did not expose an access token; try account_login again.")
    return AccountSession(
        token=token,
        token_expires=jwt_expiry(token) or time.time() + 7 * 24 * 3600,
        device_id=device_id or "",
        dest=_dest(storage),
        wallet_discount_percent=_wallet_discount(storage),
        updated_at=time.time(),
    )


def jwt_expiry(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None


def _dest(storage: dict) -> int | None:
    """Delivery region of the account's selected pickup point / address."""
    for key, value in storage.items():
        if key.startswith("geo-data-") and key != "geo-data-v1-0":
            try:
                xinfo = (json.loads(value).get("data") or {}).get("xinfo") or ""
            except ValueError:
                continue
            match = re.search(r"dest=(-?\d+)", xinfo)
            if match:
                return int(match.group(1))
    return None


def _wallet_discount(storage: dict) -> float | None:
    """Extra discount for paying with WB Wallet, as the site's basket shows it."""
    for key, value in storage.items():
        if not key.startswith("wb_basket_"):
            continue
        try:
            basket = json.loads(value)
        except ValueError:
            continue
        for pt in basket.get("paymentTypes") or []:
            if pt.get("codeLower") == "wlt" and pt.get("extraDiscount") is not None:
                return float(pt["extraDiscount"])
    return None
