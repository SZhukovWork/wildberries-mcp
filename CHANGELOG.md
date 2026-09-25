# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org): patch releases fix breakage caused
by changes on Wildberries' side, minor releases add features.

## [Unreleased]

### Added
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
