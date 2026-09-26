import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs, quote
import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36",
           "Accept": "application/json", "Content-Type": "application/json"}

@dataclass
class Merchant:
    exchange: str; merchant_id: str; nickname: str = ""; asset: str = "USDT"; fiat: str = "USD"; url: str = ""
    @property
    def key(self): return f"{self.exchange}:{self.merchant_id}:{self.asset}:{self.fiat}"

    @property
    def profile_url(self) -> str:
        """Public profile, not a marketplace URL with a merchant-id hint.

        Build OKX's profile route from the same publicUserId used to fetch
        prices. This also fixes older saved marketplace links without changing
        the merchant identity or overwriting the URL originally pasted.
        """
        if self.exchange.lower() == "okx" and self.merchant_id:
            public_id = quote(str(self.merchant_id), safe="")
            return f"https://www.okx.com/p2p/ads-merchant?publicUserId={public_id}"
        return self.url or ""

# ---------- URL -> Merchant (paste any public merchant profile link) ----------
def parse_url(url: str, asset: str, fiat: str):
    raw = url.strip()
    p = urlparse(raw)
    host = p.netloc.lower()
    qs = parse_qs(p.query)
    # also parse fragment for query-like params (some apps put params in #)
    frag_qs = parse_qs(p.fragment) if p.fragment else {}
    # merge fragment into qs for convenience
    for k, v in frag_qs.items():
        if k not in qs:
            qs[k] = v

    if "binance" in host and qs.get("advertiserNo"):
        return Merchant("binance", qs["advertiserNo"][0], asset=asset, fiat=fiat, url=raw)
    if "bybit" in host:
        m = re.search(r"/profile/(\d+)(?:/([A-Z]+)/([A-Z]+))?", p.path)
        if m:
            return Merchant("bybit", m.group(1), asset=m.group(2) or asset, fiat=m.group(3) or fiat, url=raw)
    if "okx" in host and qs.get("publicUserId"):
        return Merchant("okx", qs["publicUserId"][0], asset=asset, fiat=fiat, url=raw)

    if "bitget" in host:
        # Bitget URL variants:
        # - https://www.bitget.com/p2p/merchant/<id> or /en/p2p/merchant/<id>
        # - https://www.bitget.com/p2p/merchant-detail/<id>
        # - https://www.bitget.com/p2p/merchant-detail?userId=xxx
        # - https://www.bitget.com/en/p2p-trade/merchant?userId=xxx
        # - https://www.bitget.com/p2p/merchantDetail?merchantId=xxx
        # Try path regexes first
        patterns = [
            r"/merchant/(?:detail/)?([\w\-]{5,})",          # /merchant/<id> or /merchant/detail/<id>
            r"/merchant-detail/([\w\-]{5,})",
            r"/merchantDetail/([\w\-]{5,})",
            r"/p2p/[^/]+/([\w\-]{5,})$",  # last segment may be id
        ]
        for pat in patterns:
            m = re.search(pat, p.path, re.I)
            if m:
                mid = m.group(1)
                if mid.lower() not in ("detail", "merchant", "p2p", "trade", "en", "ru", "es", "pt", "tr"):
                    return Merchant("bitget", mid, asset=asset, fiat=fiat, url=raw)

        # Query param variants
        for key in ("userId", "merchantId", "id", "user_id", "merchant_id", "uid"):
            if qs.get(key):
                mid = qs[key][0].strip()
                if mid and len(mid) >= 3:
                    return Merchant("bitget", mid, asset=asset, fiat=fiat, url=raw)

        # Sometimes id is in full URL as plain param even if urlparse missed (e.g. /?userId=)
        # Fallback regex on raw URL
        m = re.search(r"[?&](?:userId|merchantId|id)=([\w\-]{3,})", raw, re.I)
        if m:
            return Merchant("bitget", m.group(1), asset=asset, fiat=fiat, url=raw)

        # Last resort: if URL path ends with something that looks like merchant id (alphanumeric 6+ chars)
        # and host is bitget, accept last segment
        parts = [seg for seg in p.path.split("/") if seg]
        if parts:
            last = parts[-1]
            # avoid language codes and generic words
            blacklist = {"p2p", "p2p-trade", "trade", "merchant", "merchant-detail", "en", "ru", "es", "pt", "tr", "zh", "vi", "id", "detail"}
            if last.lower() not in blacklist and re.match(r"^[\w\-]{5,}$", last):
                return Merchant("bitget", last, asset=asset, fiat=fiat, url=raw)

    return None

# ---------- Fetch ----------
# Returns, for both sides:
#   {sell, sell_amount, sell_ad_id, buy, buy_amount, buy_ad_id, error}
# "sell" = merchant SELLS (you buy) = best selling price ; "buy" = merchant BUYS (you sell)
# *_ad_id is the id of the ad the price was taken from — it powers the
# "open the exact ad" Buy/Sell buttons (see adlinks.py).
# HTTP errors (e.g. 451 when the server IP is geo-blocked) land in "error".
async def fetch(c: httpx.AsyncClient, m: Merchant) -> dict:
    try:
        fn = {"binance": _binance, "bybit": _bybit, "okx": _okx, "bitget": _bitget}[m.exchange]
        sell_price, sell_amt, sell_ad = await fn(c, m, "sell")
        buy_price, buy_amt, buy_ad = await fn(c, m, "buy")
        return {"sell": sell_price, "sell_amount": sell_amt, "sell_ad_id": sell_ad,
                "buy": buy_price, "buy_amount": buy_amt, "buy_ad_id": buy_ad,
                "error": None}
    except Exception as e:
        return {"sell": None, "sell_amount": None, "sell_ad_id": None,
                "buy": None, "buy_amount": None, "buy_ad_id": None,
                "error": str(e)[:120]}

def _parse_float(v):
    try:
        if v is None: return None
        return float(str(v).replace(",", ""))
    except:
        return None

def _best(items, side):
    """items: list of {price, amount, ad_id}. Return (price, amount, ad_id) of the
    best ad — cheapest for the merchant's sell side, highest for its buy side."""
    if not items:
        return None, None, None
    if side == "sell":
        best = min(items, key=lambda x: x["price"])
    else:
        best = max(items, key=lambda x: x["price"])
    return best["price"], best["amount"], best.get("ad_id")

async def _binance(c, m, side):
    tt = "BUY" if side == "sell" else "SELL"          # taker view
    items = []
    for page in (1, 2, 3):
        r = await c.post("https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
                         json={"asset": m.asset, "fiat": m.fiat, "tradeType": tt, "page": page, "rows": 20, "payTypes": []})
        r.raise_for_status()   # e.g. HTTP 451 when the server IP is in a geo-blocked region (US)
        data = r.json().get("data") or []
        for a in data:
            if a["advertiser"]["userNo"] == m.merchant_id:
                m.nickname = m.nickname or a["advertiser"]["nickName"]
                adv = a.get("adv", {})
                price = _parse_float(adv.get("price"))
                if price is None:
                    continue
                # amount fields: tradableQuantity, surplusAmount, dynamicMaxSingleTransAmount, maxSingleTransAmount
                amt = None
                for k in ("tradableQuantity", "surplusAmount", "dynamicMaxSingleTransAmount", "maxSingleTransAmount", "tradableAmount"):
                    if adv.get(k) is not None:
                        amt = _parse_float(adv.get(k))
                        if amt is not None:
                            break
                items.append({"price": price, "amount": amt, "ad_id": adv.get("advNo")})
        if len(data) < 20:
            break
    return _best(items, side)

async def _bybit(c, m, side):
    r = await c.post("https://api2.bybit.com/fiat/otc/item/online",
                     json={"userId": m.merchant_id, "tokenId": m.asset, "currencyId": m.fiat, "payment": [],
                           "side": "1" if side == "sell" else "0", "size": "50", "page": "1", "amount": ""})
    r.raise_for_status()   # geo-blocked regions answer 403 here instead of prices
    raw = (r.json().get("result") or {}).get("items") or []
    items = []
    for it in raw:
        if not m.nickname:
            m.nickname = it.get("nickName", "") or m.nickname
        price = _parse_float(it.get("price"))
        if price is None:
            continue
        amt = None
        for k in ("quantity", "amount", "remainingQuantity", "origQuantity"):
            if it.get(k) is not None:
                amt = _parse_float(it.get(k))
                if amt is not None:
                    break
        items.append({"price": price, "amount": amt,
                      "ad_id": it.get("id") or it.get("itemId") or it.get("adId")})
    return _best(items, side)

async def _okx(c, m, side):
    r = await c.get("https://www.okx.com/v3/c2c/tradingOrders/books",
                    params={"quoteCurrency": m.fiat, "baseCurrency": m.asset, "side": side, "paymentMethod": "all",
                            "userType": "all", "showTrade": "false", "receivingAds": "false"})
    r.raise_for_status()   # geo-blocked regions are refused here instead of served prices
    ads = [a for a in (r.json().get("data") or {}).get(side, []) if a.get("publicUserId") == m.merchant_id]
    items = []
    for a in ads:
        if not m.nickname:
            m.nickname = a.get("nickName", "") or m.nickname
        price = _parse_float(a.get("price"))
        if price is None:
            continue
        amt = None
        for k in ("availableAmount", "amount", "quantity", "remainingAmount", "tradableAmount", "buyAmount", "sellAmount"):
            if a.get(k) is not None:
                amt = _parse_float(a.get(k))
                if amt is not None:
                    break
        items.append({"price": price, "amount": amt,
                      "ad_id": a.get("id") or a.get("adId")})
    return _best(items, side)

async def _bitget(c, m, side):
    r = await c.post("https://www.bitget.com/v1/p2p/pub/adv/queryAdvList",
                     json={"side": 1 if side == "sell" else 2, "pageNo": 1, "pageSize": 50,
                           "coinCode": m.asset, "fiatCode": m.fiat, "languageType": 6})
    r.raise_for_status()   # geo-blocked regions are refused here instead of served prices
    raw = (r.json().get("data") or {}).get("dataList", []) or []
    ads = [a for a in raw if str(a.get("userId")) == m.merchant_id or str(a.get("merchantId")) == m.merchant_id or m.merchant_id in str(a.get("userId",""))]
    # fallback if filter too strict: try all if none matched but merchant present in list structure that uses id differently
    if not ads:
        # try matching without strict equality for merchant string names
        ads = [a for a in raw if m.merchant_id.lower() in str(a.get("nickName","")).lower() or m.merchant_id == str(a.get("userId"))]
        if not ads:
            # if raw contains our merchant but filter missed, include raw that has same merchant id pattern? just use raw filtered by nickname already set?
            # As last resort, if raw length small and merchant id appears in any ad's userId, use raw
            ads = [a for a in raw if str(a.get("userId")) == m.merchant_id]
    items = []
    for a in ads:
        if not m.nickname:
            m.nickname = a.get("nickName", "") or a.get("merchantName","") or m.nickname
        price = _parse_float(a.get("price") or a.get("unitPrice"))
        if price is None:
            continue
        amt = None
        for k in ("quantity", "amount", "availableAmount", "coinAmount", "tradableAmount", "surplusAmount"):
            if a.get(k) is not None:
                amt = _parse_float(a.get(k))
                if amt is not None:
                    break
        items.append({"price": price, "amount": amt,
                      "ad_id": a.get("advId") or a.get("advNo") or a.get("id")})
    return _best(items, side)
