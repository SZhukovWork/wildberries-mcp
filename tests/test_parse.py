import json
from pathlib import Path

import pytest

from wildberries_mcp import parse
from wildberries_mcp.client import _empty_search, _implausible, _unwrap

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def card_v4(article):
    return next(p for p in load("card_v4_detail.json")["products"] if p["id"] == article)


def test_rub_converts_kopecks():
    assert parse.rub(918700) == 9187
    assert parse.rub(45975) == 459.75
    assert parse.rub(None) is None


@pytest.mark.parametrize("vol, host", [(4984, "basket-27"), (9787, "basket-41"), (11841, "basket-43")])
def test_basket_host_from_route_map(vol, host):
    assert parse.basket_host(load("upstreams.json"), vol).startswith(host + ".")


def test_basket_host_unknown_vol():
    assert parse.basket_host(load("upstreams.json"), 10**7) is None


def test_live_card_offer():
    offer = parse.offer(card_v4(498414394))
    assert offer["price_rub"] == 9187
    assert offer["price_before_discount_rub"] == 12269
    assert offer["discount_percent"] == 25
    assert offer["rating"] == 5.0 and offer["reviews"] == 14
    assert offer["in_stock"] and offer["in_stock_qty"] == 19
    assert offer["seller"] == "Winsell" and offer["seller_rating"] == 4.9
    assert offer["group_id"] == 296051189
    assert offer["url"] == "https://www.wildberries.ru/catalog/498414394/detail.aspx"


def test_card_details_sizes_have_stock():
    details = parse.card_details(card_v4(498414394))
    assert details["sizes"][0]["price_rub"] == 9187
    assert details["sizes"][0]["available"] is True
    assert details["colors"] == ["оранжевый"]


def test_search_items():
    data = load("search_topor_fiskars.json")
    items = [parse.offer(p) for p in data["products"]]
    assert data["total"] > 0 and items
    for item in items:
        assert isinstance(item["article"], int)
        assert item["price_rub"] is None or item["price_rub"] > 0
        # Search results carry no per-size stock: never report a fake zero.
        assert item["in_stock_qty"] == data["products"][items.index(item)].get("totalQuantity")


def test_zero_reviews_is_not_a_zero_rating():
    p = dict(card_v4(498414394), reviewRating=0, feedbacks=0)
    offer = parse.offer(p)
    assert offer["rating"] is None and offer["reviews"] == 0


def test_static_card():
    card = parse.static_card(load("basket_card_498414394.json"))
    assert card["brand"] == "FISKARS"
    assert card["group_id"] == 296051189
    assert 498414394 not in card["group_articles"] and len(card["group_articles"]) == 15
    assert any(c["name"] == "Цвет" and c["group"] == "Основная информация" for c in card["characteristics"])
    assert card["listed_since"] == "2025-08-19"


def test_article_feed_stars_are_separate_from_group():
    fb = load("feedbacks_296051189.json")
    article = parse.article_feed_stars(fb, 498414394)
    group = parse.group_rating(fb)
    assert article == {"avg": 5.0, "count": 14, "stars": {"5": 14, "4": 0, "3": 0, "2": 0, "1": 0}}
    assert group["rating"] == 4.9 and group["reviews"] == 678
    assert group["articles_in_group"] == 16
    assert parse.article_feed_stars(fb, 1) is None


def test_select_reviews_scope_and_order():
    raw = load("feedbacks_296051189.json")["feedbacks"]
    own = parse.select_reviews(raw, 498414394, 1, 5, "newest", with_text_only=False)
    assert own and all(r["article"] == 498414394 for r in own)
    dates = [r["date"] for r in own]
    assert dates == sorted(dates, reverse=True)
    worst = parse.select_reviews(raw, None, 1, 5, "worst", with_text_only=True)
    assert [r["rating"] for r in worst] == sorted(r["rating"] for r in worst)
    assert all(r["text"] or r["pros"] or r["cons"] for r in worst)
    only_bad = parse.select_reviews(raw, None, 1, 3, "newest", with_text_only=False)
    assert all(r["rating"] <= 3 for r in only_bad)


def test_review_has_no_invented_photo_count():
    r = parse.review(load("feedbacks_296051189.json")["feedbacks"][0])
    assert "photos" not in r
    assert set(r) >= {"date", "rating", "text", "pros", "cons", "variant", "tags", "seller_answer"}


def test_price_history_is_weekly_average():
    history = parse.price_history(load("price_history_978751317.json"))
    assert history[0] == {"week": "2026-07-05", "avg_price_rub": 317}
    summary = parse.history_summary(history, 498)
    assert summary["points"] == len(history)
    assert summary["min_rub"] == 317
    assert summary["current_vs_min_percent"] == round((498 - 317) / 317 * 100, 1)


def test_history_summary_empty():
    assert parse.history_summary([], 100) == {"points": 0}


def test_seller_legal_info():
    s = parse.seller(load("supplier_4056581.json"))
    assert s["legal_name"].startswith("Общество")
    assert s["tax_ids"] == {"UNP (BY)": "193748057"}
    assert "Минск" in s["legal_address"]


def test_implausible_region_mismatch_is_rejected():
    junk = {"params": {"dest": 123585734}, "data": {"products": [{"id": 1}]}}
    assert "region" in _implausible(junk, "products", -1257786)


def test_implausible_missing_products():
    assert _implausible({"metadata": {}}, "products", -1) == "no 'products' field"
    assert _implausible({"metadata": {}, "total": 0}, "products", -1) is None
    assert _implausible("<html>", "products", -1) == "body is not a JSON object"


def test_wrapped_payload_is_unwrapped():
    wrapped = {"params": {"dest": -1}, "data": {"products": [{"id": 1}], "total": 1}}
    assert _implausible(wrapped, "products", -1) is None
    assert _unwrap(wrapped)["products"] == [{"id": 1}]


def test_empty_search_shape_is_recognised():
    empty = {"name": "q", "query": "_st0=x", "shardKey": "merger", "rs": 70, "search_result": {}}
    assert _empty_search(empty)
    assert not _empty_search({"metadata": {}, "products": [], "total": 0})
    assert not _empty_search(load("search_topor_fiskars.json"))
