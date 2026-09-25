# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org): patch releases fix breakage caused
by changes on Wildberries' side, minor releases add features.

## [Unreleased]

### Added
- Optional account mode (`WB_ACCOUNT=1`): login in a visible browser window
  (`wildberries-mcp login` or `account_login`), `account_status`,
  `account_logout`, and cart tools `get_cart`, `add_to_cart`,
  `remove_from_cart`; `price_with_wallet_rub` from the account's WB Wallet
  discount. No ordering or payment tools by design.

### Fixed
- Tool errors now reach the agent with their reason (MCP 2.x forwards only
  `ToolError` text).
- Per-size stock is reported as available yes/no: WB's per-warehouse `qty`
  is not a unit count.

### Added (initial)
- Search with real pages (100 items), six sort orders and a price window;
  `total_found` and `has_more`.
- Live product card: per-size price, pre-discount price, stock, delivery
  estimate, article rating next to the merged-group rating, seller legal
  details, characteristics, package contents, price-history summary and the
  other articles of the merged group with live prices.
- Reviews with date, stars, text, pros, cons, bought variant, buyer tags and
  seller reply; article or group scope; sorting including worst-first.
- Weekly price history next to the live price; side-by-side comparison of up
  to 50 articles in one request.
- Automatic anti-bot token (headless Chromium), re-minted when revoked;
  sanity checks that refuse implausible storefront answers.
