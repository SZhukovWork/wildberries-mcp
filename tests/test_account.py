import base64
import json
import time

import anyio
from mcp.server.mcpserver import MCPServer

from wildberries_mcp import parse, server
from wildberries_mcp.account import _session_from_storage, jwt_expiry


def fake_jwt(exp: int) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part({'exp': exp, 'iat': exp - 30 * 86400})}.sig"


SYNC = {
    "state": 0,
    "change_ts": 1790316140340,
    "result_set": [[
        {"chrt_id": 1465909194, "cod_1s": 978751317, "quantity": 2, "is_deleted": False, "ts": 1790316122731},
        {"chrt_id": 111, "cod_1s": 222, "quantity": 1, "is_deleted": True, "ts": 1790316122731},
    ]],
}


def test_cart_lines_skip_deleted_items():
    assert parse.cart_lines(SYNC) == [
        {"article": 978751317, "size_id": 1465909194, "quantity": 2, "added": "2026-09-25"},
    ]


def test_size_lookup_by_option_id():
    product = {"sizes": [{"optionId": 1465909194, "origName": "0", "price": {"product": 49600}},
                         {"optionId": 7, "origName": "42", "price": {"product": 100000}}]}
    assert parse.size_name(product, 1465909194) is None  # "0" = one-size product
    assert parse.size_name(product, 7) == "42"
    assert parse.size_price(product, 7) == 1000


def test_wallet_price_matches_the_site():
    # Site showed 481 ₽ for a 496 ₽ item with the account's 3 % WB Wallet discount.
    assert parse.with_wallet(496, 3.0) == 481
    assert parse.with_wallet(496, None) is None


def test_session_from_site_storage_keeps_no_personal_data():
    exp = int(time.time()) + 30 * 86400
    storage = {
        "wbx__tokenData": json.dumps({"token": fake_jwt(exp), "phone": "7XXXXXXXXXX"}),
        "geo-data-abc": json.dumps({"data": {"xinfo": "appType=1&curr=rub&dest=-5818883&spp=30"}}),
        "wb_basket_abc": json.dumps({"paymentTypes": [{"codeLower": "wlt", "extraDiscount": 3}]}),
    }
    session = _session_from_storage(storage, "site_device")
    assert session.token_expires == exp and session.dest == -5818883
    assert session.wallet_discount_percent == 3.0
    public = session.public()
    assert "token" not in public and "phone" not in json.dumps(public)


def test_jwt_expiry_of_garbage_is_none():
    assert jwt_expiry("not-a-jwt") is None


def test_account_tools_are_opt_in_and_marked_as_writes():
    assert "add_to_cart" not in {t.name for t in anyio.run(server.mcp.list_tools)}
    target = MCPServer("test")
    server.register_account_tools(target)
    tools = {t.name: t for t in anyio.run(target.list_tools)}
    assert set(tools) == {"account_login", "account_status", "account_logout", "get_cart", "add_to_cart", "remove_from_cart"}
    assert tools["add_to_cart"].annotations.read_only_hint is False
    assert tools["remove_from_cart"].annotations.destructive_hint is True
    assert tools["get_cart"].annotations.read_only_hint is True
