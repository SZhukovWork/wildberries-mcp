"""Passing Wildberries' anti-bot check ("wbaas") with a real browser.

The storefront API (search, live product cards) rejects any client that does
not carry an ``x_wbaas_token`` cookie. The token is minted by a JavaScript
challenge on www.wildberries.ru, so a short headless-Chromium visit is needed
once; after that plain HTTP with the same User-Agent is accepted until WB
revokes the token (on its own schedule, often sooner under heavy use).

Two details decide whether the challenge passes:
- the "new" headless mode (``channel="chromium"``) — the old headless shell is
  recognised and loops on HTTP 498 forever;
- a User-Agent that matches the real browser build without the "Headless"
  marker, because the token is bound to it.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

HOME_URL = "https://www.wildberries.ru/"
TOKEN_COOKIE = "x_wbaas_token"


class AntibotError(RuntimeError):
    """The browser could not obtain a token (blocked IP, captcha, no browser)."""


@dataclass
class BrowserSession:
    user_agent: str
    browser_version: str
    platform: str
    brands: str  # Sec-CH-UA value the browser itself sends
    cookies: dict[str, str]
    device_id: str = field(default_factory=lambda: "site_" + uuid.uuid4().hex)
    minted_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BrowserSession":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def mint(proxy: str | None = None, headless: bool = True, timeout: float = 40.0) -> BrowserSession:
    """Open the storefront in Chromium until the anti-bot token is issued."""
    try:
        return _mint(proxy, headless, timeout)
    except _BrowserMissing:
        _install_chromium()
        return _mint(proxy, headless, timeout)


class _BrowserMissing(Exception):
    pass


def _mint(proxy: str | None, headless: bool, timeout: float) -> BrowserSession:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                headless=headless,
                channel="chromium",
                proxy={"server": proxy} if proxy else None,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except PlaywrightError as e:
            if "Executable doesn't exist" in str(e):
                raise _BrowserMissing() from e
            raise AntibotError(f"Could not start Chromium: {e}") from e
        try:
            probe = browser.new_page()
            user_agent = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            probe.close()

            ctx = browser.new_context(
                user_agent=user_agent, locale="ru-RU", viewport={"width": 1440, "height": 900}
            )
            page = ctx.new_page()
            passed = False
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            while time.monotonic() - started < timeout:
                cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                # The challenge reloads the page once the token is set; the
                # real storefront has a proper <title>, the challenge page "...".
                if TOKEN_COOKIE in cookies and page.title() not in ("", "..."):
                    passed = True
                    break
                page.wait_for_timeout(500)
            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            # Client hints as this (UA-overridden) context sends them — the API
            # requests must look like they come from the same browser.
            platform = page.evaluate("navigator.userAgentData ? navigator.userAgentData.platform : ''")
            brands = page.evaluate(
                "navigator.userAgentData ? navigator.userAgentData.brands"
                ".map(b => `\"${b.brand}\";v=\"${b.version}\"`).join(', ') : ''"
            )
        finally:
            browser.close()

    if not passed:
        raise AntibotError(
            "Wildberries did not issue an anti-bot token within "
            f"{timeout:.0f}s. WB blocks some foreign/VPN/datacenter IPs — try a "
            "Russian residential IP or set WB_PROXY."
        )
    log.info("Wildberries anti-bot token minted in %.1fs", time.monotonic() - started)
    return BrowserSession(
        user_agent=user_agent,
        browser_version=user_agent.split("Chrome/")[1].split(" ")[0],
        platform=platform or "Linux",
        brands=brands,
        cookies=cookies,
    )


def _install_chromium() -> None:
    """First run under uvx has no browser yet: fetch Playwright's Chromium.

    Output goes to stderr — stdout belongs to the MCP stdio transport.
    """
    log.warning("Playwright Chromium is not installed; installing it (one-time, ~150 MB)")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        stdout=sys.stderr, stderr=sys.stderr, check=False,
    )
    if result.returncode != 0:
        raise AntibotError(
            "Chromium is required for the Wildberries anti-bot check and could not be "
            "installed automatically. Run: python -m playwright install chromium"
        )
