import anyio

from wildberries_mcp import server


def test_tools_are_registered_read_only():
    tools = {t.name: t for t in anyio.run(server.mcp.list_tools)}
    assert set(tools) == {"search_products", "get_product", "get_price_history", "get_reviews", "compare_products"}
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.description


def test_search_sort_options_are_enumerated():
    tools = {t.name: t for t in anyio.run(server.mcp.list_tools)}
    sort = tools["search_products"].input_schema["properties"]["sort"]
    assert set(sort["enum"]) == {"popular", "rating", "price_asc", "price_desc", "newest", "benefit"}
