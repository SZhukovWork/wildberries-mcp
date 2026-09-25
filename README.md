# wildberries-mcp

**English** · [Русский](README.ru.md)

An [MCP](https://modelcontextprotocol.io) server that gives LLM agents live,
honestly-labelled data from [Wildberries](https://www.wildberries.ru), the
largest Russian marketplace: search with real pages, sorting and price
windows; live product cards; per-article ratings and reviews; weekly price
history; seller legal details.

> Buyer side: searches the public storefront, no Wildberries account or token
> needed. Looking for your **seller cabinet** (orders, stocks, supplies via the
> official Seller API)? That is a different tool —
> [theYahia/wildberries-mcp](https://github.com/theYahia/wildberries-mcp).

## Why another Wildberries server

Most WB scrapers quietly return data that is not what a buyer sees. This one
is built around not doing that:

| Pitfall | What this server does |
|---|---|
| Search API rejects clients without WB's anti-bot token (HTTP 403/498) | Mints the token with a short headless-Chromium visit, re-mints it when WB revokes it |
| "Current price" taken from price history (a weekly average, often weeks old) | Live price from the same card API the site uses, per size, with pre-discount price and discount |
| Rating/reviews of the **merged product group** shown as the product's own (a group can merge 16 different models) | Article rating as shown on the product page; group rating in a separate, labelled field |
| New articles fail because CDN hosts are guessed from a hard-coded table | Hosts come from WB's published CDN route map |
| Anti-bot "decoy" answers (unrelated products, wrong region) passed on as data | Every storefront answer is sanity-checked; implausible ones are retried and, if they persist, turned into an error |

Every response carries `fetched_at` and the delivery region it was computed for.

## Tools

| Tool | What it returns |
|---|---|
| `search_products(query, page, sort, price_min, price_max, limit)` | 100 items per page; sort `popular` / `rating` / `price_asc` / `price_desc` / `newest` / `benefit`; `total_found`, `has_more`. Per item: price, pre-discount price, discount, rating & reviews (per article), stock, seller & seller rating, delivery estimate (hours), product-group id, URL |
| `get_product(article, include_variants)` | Live offer per size, stock, delivery estimate; article rating + group rating with star split; seller with legal entity, address and tax id; characteristics; description; package contents; price-history summary; other articles of the merged group with their live prices |
| `get_price_history(article)` | Weekly average prices next to the live price, with min/max and "current vs min/max" |
| `get_reviews(article, limit, scope, sort, min_rating, max_rating)` | Date, stars, text, pros, cons, bought variant, buyer tags, seller reply; `scope=article` or the whole group; `sort=worst` surfaces complaints first |
| `compare_products(articles)` | Up to 50 articles side by side in one request |

## Install

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/). On first use the
server downloads Playwright's Chromium (~300 MB, once) for the anti-bot check.

Claude Code:

```bash
claude mcp add wildberries -- uvx --from git+https://github.com/SZhukovWork/wildberries-mcp wildberries-mcp
```

Any MCP client (`claude_desktop_config.json`, `.mcp.json`, …):

```json
{
  "mcpServers": {
    "wildberries": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/SZhukovWork/wildberries-mcp", "wildberries-mcp"]
    }
  }
}
```

From a checkout: `uv venv && uv pip install -e . && .venv/bin/wildberries-mcp`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `WB_DEST` | auto | WB delivery-region id. By default WB's own IP-based detection is used (the region is reported in every answer). To match your pickup point exactly, copy `dest=` from any `__internal/u-card` request in the browser dev tools on wildberries.ru |
| `WB_PROXY` | — | Proxy URL for both HTTP and the browser, e.g. `http://user:pass@host:3128` |
| `WB_MIN_INTERVAL` | `3.0` | Seconds between storefront API calls (search, cards). WB throttles hard; lower values get 429s |
| `WB_CACHE_DIR` | `~/.cache/wildberries-mcp` | Where the anti-bot session is kept |
| `WB_HEADLESS` | `1` | `0` shows the browser window while minting the token (debugging) |

## What the numbers mean

- **Prices** are what an anonymous buyer sees in the reported region right now.
  A signed-in buyer may see a lower personal price (WB Wallet — about 2 % —
  and loyalty discounts).
- **`delivery_eta_hours`** is WB's estimate for the region (the site turns the
  same numbers into a date).
- **Ratings**: `rating`/`reviews` are per article, as shown on the product page.
  `group` fields describe the whole merged product group. The star split from
  the review feed can cover fewer ratings than the card counts — the server
  says so when it happens.
- **Review photos**: WB does not link photos to individual reviews, so no
  per-review photo count is reported (rather than a misleading 0).
- **Price history** holds weekly averages; the last point is not the current price.

## Limitations

- Unofficial: relies on the storefront's internal endpoints, which WB can change
  at any time. Parsers are isolated in `parse.py` and covered by tests on
  recorded responses.
- WB blocks many foreign, VPN and datacenter IPs. Use a Russian residential IP
  or `WB_PROXY`.
- Search is rate-limited by WB; the server spaces calls and reports a clear
  error instead of looping.
- Read-only and anonymous: no account, cart or orders (see Roadmap).

## Roadmap

- **Later: optional account mode** (off by default). Log in once in a visible
  browser window — phone number and SMS code never pass through the MCP client —
  to get your personal prices and exact delivery dates for your pickup point.
- **Later: cart** — `add_to_cart` / `get_cart` on top of the account mode.
  Checkout, payment and address changes are deliberately out of scope.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest            # offline tests on recorded responses
.venv/bin/pytest -m live    # end-to-end over MCP stdio against live WB (Russian IP)
```

## Disclaimer & credits

Not affiliated with Wildberries. Intended for personal price research; respect
Wildberries' terms of use and keep request rates low. The idea of exposing WB
as MCP tools was inspired by
[shndo1337/wildberries-mcp](https://github.com/shndo1337/wildberries-mcp); this
is an independent implementation.

License: MIT.
