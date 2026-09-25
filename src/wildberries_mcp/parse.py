"""Pure translations of raw Wildberries payloads into tool output.

Nothing here does I/O, so every function is exercised by tests on recorded
responses (tests/fixtures). Money arrives in kopecks and leaves in rubles.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

PRODUCT_URL = "https://www.wildberries.ru/catalog/{}/detail.aspx"
SELLER_URL = "https://www.wildberries.ru/seller/{}"


def rub(kopecks: int | float | None) -> int | float | None:
    if kopecks is None:
        return None
    value = kopecks / 100
    return int(value) if value == int(value) else round(value, 2)


def discount_percent(before: float | None, now: float | None) -> int | None:
    if not before or now is None or now >= before:
        return None
    return round((before - now) / before * 100)


def iso_date(ts: int | float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def product_url(article: int) -> str:
    return PRODUCT_URL.format(article)


# ---- CDN routing ----------------------------------------------------------

def basket_host(routes: dict, vol: int) -> str | None:
    """Host that serves this `vol` in WB's published CDN route map.

    The "origin" section maps vol ranges to basket-NN hosts and is the same
    everywhere; "recommend" may instead list regional mirrors picked by
    modulo, which redirect to the origin anyway — so origin wins.
    """
    for section in ("origin", "recommend"):
        for group in (routes.get(section) or {}).get("mediabasket_route_map") or []:
            if group.get("method") not in (None, "range"):
                continue
            for host in group.get("hosts") or []:
                if host.get("vol_range_from", -1) <= vol <= host.get("vol_range_to", -1):
                    return host.get("host")
    return None


# ---- prices and offers ----------------------------------------------------

def size_offers(sizes: Iterable[dict] | None) -> list[dict]:
    offers = []
    for size in sizes or []:
        price = size.get("price") or {}
        stocks = size.get("stocks")
        offers.append({
            "size": size.get("origName") or size.get("name") or None,
            "price_rub": rub(price.get("product")),
            "price_before_discount_rub": rub(price.get("basic")),
            # Per-warehouse "qty" is not a unit count (seen: qty 1 while the
            # product total was 54), so a size only gets yes/no. Search
            # results carry no per-size stocks at all.
            "available": (bool(price) and any((s.get("qty") or 0) > 0 for s in stocks)) if stocks is not None else None,
        })
    return offers


def _price_block(sizes: Iterable[dict] | None) -> dict:
    offers = [o for o in size_offers(sizes) if o["price_rub"] is not None]
    if not offers:
        return {"price_rub": None, "price_before_discount_rub": None,
                "discount_percent": None, "price_varies_by_size": False}
    cheapest = min(offers, key=lambda o: o["price_rub"])
    return {
        "price_rub": cheapest["price_rub"],
        "price_before_discount_rub": cheapest["price_before_discount_rub"],
        "discount_percent": discount_percent(cheapest["price_before_discount_rub"], cheapest["price_rub"]),
        "price_varies_by_size": len({o["price_rub"] for o in offers}) > 1,
    }


def _rating(value: Any, count: Any) -> tuple[float | None, int]:
    count = int(count or 0)
    # WB reports 0 for "no reviews yet"; that is not a zero-star rating.
    return (float(value) if count and value else None), count


def _delivery_hours(p: dict) -> int | None:
    t1, t2 = p.get("time1"), p.get("time2")
    return t1 + t2 if isinstance(t1, int) and isinstance(t2, int) else None


def offer(p: dict) -> dict:
    """One product as it appears in search results or the live card API.

    Both payloads share the fields used here; ratings are per article
    (WB splits reviews by article), not per merged product group.
    """
    rating, reviews = _rating(p.get("reviewRating"), p.get("feedbacks"))
    total_qty = p.get("totalQuantity")
    return {
        "article": p.get("id"),
        "name": p.get("name"),
        "brand": p.get("brand") or None,
        **_price_block(p.get("sizes")),
        "rating": rating,
        "reviews": reviews,
        "in_stock": bool(total_qty),
        "in_stock_qty": total_qty,
        "seller": p.get("supplier"),
        "seller_id": p.get("supplierId"),
        "seller_rating": p.get("supplierRating") or None,
        "delivery_eta_hours": _delivery_hours(p),
        "group_id": p.get("root"),
        "url": product_url(p.get("id")),
    }


def card_details(p: dict) -> dict:
    """Live card extras that search results do not carry."""
    return {
        "sizes": size_offers(p.get("sizes")),
        "colors": [c.get("name") for c in p.get("colors") or [] if c.get("name")],
    }


# ---- static card (CDN card.json) ------------------------------------------

def static_card(card: dict) -> dict:
    selling = card.get("selling") or {}
    return {
        "name": card.get("imt_name"),
        "brand": selling.get("brand_name"),
        "vendor_code": card.get("vendor_code"),
        "category": card.get("subj_name"),
        "category_root": card.get("subj_root_name"),
        "color": card.get("nm_colors_names") or None,
        "package_contents": card.get("contents") or None,
        "description": card.get("description"),
        "characteristics": characteristics(card),
        "group_id": card.get("imt_id"),
        "group_articles": [a for a in card.get("colors") or [] if a != card.get("nm_id")],
        "listed_since": (card.get("create_date") or "")[:10] or None,
    }


def characteristics(card: dict) -> list[dict]:
    """Seller-filled attributes, keeping WB's grouping when present."""
    out = []
    groups = card.get("grouped_options")
    if groups:
        for group in groups:
            for o in group.get("options") or []:
                out.append({"group": group.get("group_name"), "name": o.get("name"), "value": o.get("value")})
        return out
    return [{"group": None, "name": o.get("name"), "value": o.get("value")} for o in card.get("options") or []]


# ---- price history --------------------------------------------------------

def price_history(points: list[dict]) -> list[dict]:
    return [
        {"week": iso_date(pt["dt"]), "avg_price_rub": rub((pt.get("price") or {}).get("RUB"))}
        for pt in points
        if (pt.get("price") or {}).get("RUB") is not None
    ]


def history_summary(history: list[dict], current: float | None) -> dict:
    prices = [h["avg_price_rub"] for h in history]
    if not prices:
        return {"points": 0}
    low, high = min(prices), max(prices)
    summary = {
        "points": len(prices),
        "first_week": history[0]["week"],
        "last_week": history[-1]["week"],
        "min_rub": low,
        "max_rub": high,
    }
    if current is not None:
        summary["current_vs_min_percent"] = round((current - low) / low * 100, 1) if low else None
        summary["current_vs_max_percent"] = round((current - high) / high * 100, 1) if high else None
    return summary


# ---- reviews --------------------------------------------------------------

def distribution_rating(dist: dict | None) -> dict:
    counts = {int(k): int(v) for k, v in (dist or {}).items()}
    total = sum(counts.values())
    return {
        "avg": round(sum(k * v for k, v in counts.items()) / total, 2) if total else None,
        "count": total,
        "stars": {str(k): counts.get(k, 0) for k in (5, 4, 3, 2, 1)},
    }


def article_feed_stars(feedbacks: dict, article: int) -> dict | None:
    """Star distribution of the article's ratings present in the review feed.

    The feed can hold fewer ratings than the headline count on the card
    (observed: card 4.6 from 9, feed 3 ratings), so this is supporting detail,
    never a replacement for the card rating.
    """
    for entry in feedbacks.get("nmValuationDistribution") or []:
        if entry.get("nm") == article:
            return distribution_rating(entry.get("valuationDistribution"))
    return None


def group_rating(feedbacks: dict) -> dict:
    """Rating of the whole merged product group, as WB reports it."""
    valuation = feedbacks.get("valuation")
    try:
        rating = float(valuation) if valuation not in (None, "", "0") else None
    except (TypeError, ValueError):
        rating = None
    return {
        "rating": rating,
        "reviews": feedbacks.get("feedbackCount"),
        "articles_in_group": len(feedbacks.get("nmValuationDistribution") or []) or None,
        "reviews_with_text": feedbacks.get("feedbackCountWithText"),
        "reviews_with_photo": feedbacks.get("feedbackCountWithPhoto"),
        "stars": distribution_rating(feedbacks.get("valuationDistribution"))["stars"],
    }


def review(f: dict) -> dict:
    votes = f.get("votes") or {}
    answer = f.get("answer") or {}
    variant = ", ".join(v for v in (f.get("color"), f.get("size")) if v and v != "0") or None
    return {
        "date": (f.get("createdDate") or "")[:10] or None,
        "rating": f.get("productValuation"),
        "text": f.get("text") or None,
        "pros": f.get("pros") or None,
        "cons": f.get("cons") or None,
        "article": f.get("nmId"),
        "variant": variant,
        "size_fit": f.get("matchingSize") or None,
        "tags": f.get("bables") or [],
        # Review photos come as one flat list for the whole group, not linked
        # to reviews, so a per-review photo count is not available.
        "has_video": bool(f.get("video")),
        "helpful": votes.get("pluses") or 0,
        "unhelpful": votes.get("minuses") or 0,
        "excluded_from_rating": bool(f.get("excludedFromRating")),
        "seller_answer": answer.get("text") or None,
    }


REVIEW_SORTS = {
    "newest": (lambda r: r["date"] or "", True),
    "oldest": (lambda r: r["date"] or "", False),
    "worst": (lambda r: (r["rating"] or 0, r["date"] or ""), False),
    "best": (lambda r: (r["rating"] or 0, r["date"] or ""), True),
    "helpful": (lambda r: (r["helpful"] - r["unhelpful"], r["date"] or ""), True),
}


def select_reviews(raw: list[dict], article: int | None, min_rating: int, max_rating: int,
                   sort: str, with_text_only: bool) -> list[dict]:
    reviews = [review(f) for f in raw]
    reviews = [
        r for r in reviews
        if (article is None or r["article"] == article)
        and r["rating"] is not None and min_rating <= r["rating"] <= max_rating
        and (not with_text_only or r["text"] or r["pros"] or r["cons"])
    ]
    key, reverse = REVIEW_SORTS[sort]
    return sorted(reviews, key=key, reverse=reverse)


# ---- seller ---------------------------------------------------------------

def seller(info: dict) -> dict:
    ids = [(kind, info.get(key)) for kind, key in
           (("INN (RU)", "inn"), ("UNP (BY)", "unp"), ("BIN (KZ)", "bin"), ("UNN", "unn"))]
    tax_ids = {kind: value for kind, value in ids if value}
    return {
        "seller_id": info.get("supplierId"),
        "name": info.get("supplierName"),
        "legal_name": info.get("supplierFullName") or None,
        "trademark": info.get("trademark") or None,
        "legal_address": info.get("legalAddress") or None,
        "tax_ids": tax_ids,
        "ogrn": info.get("ogrn") or info.get("ogrnip") or None,
        "url": SELLER_URL.format(info.get("supplierId")),
    }


# ---- account cart ----------------------------------------------------------

def cart_lines(sync: dict) -> list[dict]:
    """Items of a full cart sync. WB nests them as a list of lists."""
    lines = []
    for group in sync.get("result_set") or []:
        for item in group if isinstance(group, list) else [group]:
            if item.get("is_deleted") or not item.get("quantity"):
                continue
            lines.append({
                "article": item.get("cod_1s"),
                "size_id": item.get("chrt_id"),
                "quantity": item.get("quantity"),
                "added": iso_date(item["ts"] / 1000) if item.get("ts") else None,
            })
    return lines


def size_name(product: dict, size_id: int) -> str | None:
    for size in product.get("sizes") or []:
        if size.get("optionId") == size_id:
            name = size.get("origName") or size.get("name")
            return None if name in (None, "", "0") else name
    return None


def size_price(product: dict, size_id: int) -> float | None:
    for size in product.get("sizes") or []:
        if size.get("optionId") == size_id:
            return rub((size.get("price") or {}).get("product"))
    return None


def with_wallet(price: float | None, wallet_percent: float | None) -> float | None:
    """Price when paying with WB Wallet — an estimate from the account's
    wallet discount; the site applies it at checkout."""
    if price is None or not wallet_percent:
        return None
    return round(price * (1 - wallet_percent / 100))
