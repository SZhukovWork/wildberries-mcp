"""End-to-end check over the real MCP stdio transport against live Wildberries.

Deselected by default; run with:  pytest -m live
Needs a Russian IP (WB blocks many foreign/VPN addresses).
"""
import json
import sys

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.live

ARTICLE = 498414394  # Fiskars X24: an article merged into a 16-article group


async def _call_all():
    params = StdioServerParameters(command=sys.executable, args=["-m", "wildberries_mcp"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            results = {}
            for name, args in [
                ("search_products", {"query": "топор fiskars", "sort": "price_asc", "price_min": 3000, "limit": 5}),
                ("get_product", {"article": ARTICLE}),
                ("get_price_history", {"article": ARTICLE}),
                ("get_reviews", {"article": ARTICLE, "limit": 5, "scope": "group", "sort": "worst"}),
                ("compare_products", {"articles": [ARTICLE, 141304983]}),
            ]:
                res = await session.call_tool(name, args)
                assert not res.is_error, (name, res.content)
                results[name] = res.structured_content or json.loads(res.content[0].text)
            return tools, results


def test_all_tools_over_stdio():
    tools, r = anyio.run(_call_all)
    assert set(tools) == {"search_products", "get_product", "get_price_history", "get_reviews", "compare_products"}

    search = r["search_products"]
    assert search["total_found"] > 0 and search["items"]
    prices = [i["price_rub"] for i in search["items"] if i["price_rub"]]
    assert prices == sorted(prices) and min(prices) >= 3000 * 0.9

    product = r["get_product"]
    assert product["offer"]["price_rub"] and product["rating"]["group"]["articles_in_group"] > 1
    assert product["seller"]["legal_name"]
    assert product["region"]["dest"]

    assert r["get_price_history"]["current_price_rub"] == product["offer"]["price_rub"]
    assert r["get_reviews"]["reviews"]
    assert {row["article"] for row in r["compare_products"]["items"]} == {ARTICLE, 141304983}
