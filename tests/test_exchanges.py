"""Exchange fetchers: HTTP errors must surface, not silently become \"—\".

When an exchange refuses the request — e.g. HTTP 451 because the server IP is
in a geo-blocked region — ``fetch`` reports the status in ``error`` so the group
post, the add-merchant reply and ``/api/webhook?check=1`` show what is wrong.
All of it offline, through ``httpx.MockTransport``.
"""

import asyncio

import httpx

from exchanges import Merchant, fetch


def _client(status: int, payload: dict | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload or {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _binance_advertiser() -> dict:
    return {"data": [{"advertiser": {"userNo": "s1918r38", "nickName": "Fast_sonic"},
                      "adv": {"price": "1.0020", "advNo": "1122334455",
                              "tradableQuantity": "10645.56"}}]}


def test_binance_geo_block_is_reported_not_silent():
    merchant = Merchant("binance", "s1918r38", asset="USDT", fiat="USD")
    result = asyncio.run(fetch(_client(451, {"code": 0,
                                            "msg": "Service unavailable from a restricted location"}),
                               merchant))
    assert result["sell"] is None and result["buy"] is None
    assert "451" in (result["error"] or "")


def test_okx_forbidden_is_reported():
    merchant = Merchant("okx", "0dec824eed", asset="USDT", fiat="USD")
    result = asyncio.run(fetch(_client(403, {"msg": "forbidden"}), merchant))
    assert "403" in (result["error"] or "")


def test_successful_fetch_still_parses_prices_and_ad_ids():
    merchant = Merchant("binance", "s1918r38", asset="USDT", fiat="USD")
    result = asyncio.run(fetch(_client(200, _binance_advertiser()), merchant))
    assert result["error"] is None
    assert result["sell"] == 1.002 and result["buy"] == 1.002
    assert result["sell_amount"] == 10645.56
    assert result["sell_ad_id"] == "1122334455"
    assert merchant.nickname == "Fast_sonic"


def test_okx_profile_url_uses_public_merchant_id():
    from urllib.parse import parse_qs, urlparse

    urls = (
        "",
        "https://www.okx.com/p2p/market?publicUserId=0dec824eed",
        "https://www.okx.com/p2p-markets/usd/buy-usdt?publicUserId=0dec824eed",
        "https://www.okx.com/p2p/ads-merchant?publicUserId=0dec824eed",
    )
    for url in urls:
        merchant = Merchant("okx", "0dec824eed", url=url)
        assert merchant.profile_url == "https://www.okx.com/p2p/ads-merchant?publicUserId=0dec824eed"
        assert merchant.url == url  # keep the original input; do not rewrite saved data

    merchant = Merchant("okx", "id&adId=other/#")
    parsed = urlparse(merchant.profile_url)
    assert parse_qs(parsed.query) == {"publicUserId": [merchant.merchant_id]}
    assert parsed.fragment == ""


def test_parsed_okx_merchant_links_to_profile():
    from exchanges import parse_url

    merchant = parse_url("https://www.okx.com/p2p/market?publicUserId=0dec824eed", "USDT", "USD")
    assert merchant.merchant_id == "0dec824eed"
    assert merchant.profile_url == "https://www.okx.com/p2p/ads-merchant?publicUserId=0dec824eed"


def test_other_exchanges_keep_the_saved_profile_url():
    for exchange in ("binance", "bybit", "bitget"):
        url = f"https://www.{exchange}.com/profile/123"
        assert Merchant(exchange, "123", url=url).profile_url == url
        assert Merchant(exchange, "123").profile_url == ""


def test_okx_without_identity_does_not_generate_an_empty_profile_link():
    assert Merchant("okx", "").profile_url == ""
