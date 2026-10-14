#!/usr/bin/env python3
"""
ShityDEX public market-data API -- READ-ONLY, separate service.
Cell: urgent-program-2026-10-14/api/cells/exchange  (Exchange API agent)

Design rules (Jodie real-numbers rule, absolute):
- Data comes ONLY from ShityDEX's own records:
  * markets config: /var/www/shitcoinbtc/assets/exchange-markets.json
    (the exact file user-api.py load_valid_markets() reads)
  * trades / orders: /home/shitcoin/user-accounts.db opened mode=ro,
    a fresh read-only connection per request. No writes, ever.
- last_price = price of the most recent ACTUAL executed trade in the
  trades table for that market, with last_trade_at alongside it.
  Markets with no recorded trade report last_price "0", last_trade_at null.
  Reference prices (prices table / CoinGecko / Kraken) are NEVER used
  as last_price, volume, bid or ask.
- 24h volume = SUM of trades in the trailing 24h only.
- Order book = this exchange's own currently-open orders only.
  No external (Kraken) depth is merged in.
- liquidity_in_usd is always "0": the only row in the pools table
  (id 1, BTC/USDT) is in AMM_POOLS_DISABLED in user-api.py and the
  public /api/user/pools endpoint returns [] -- no live pool backs
  any pair, so no liquidity figure is claimed.
- Internal ledger balances are never read or exposed.
- Trade side (buy/sell) is not derivable from the trades table
  (no side column) and is omitted rather than guessed.
  buyer_id / seller_id are never exposed.

Endpoints:
  GET /healthz
  GET /api/coingecko/pairs
  GET /api/coingecko/tickers
  GET /api/coingecko/historical_trades?ticker_id=BTC_USDT&limit=100
  GET /api/v1/exchange/markets
  GET /api/v1/exchange/tickers
  GET /api/v1/exchange/orderbook?market=BTC/USDT
  GET /api/v1/exchange/trades?ticker_id=BTC_USDT&limit=100
  GET /api/v1/exchange/assets
  GET /api/v1/exchange/status

CoinGecko pairs/tickers return bare JSON arrays (CoinGecko schema shape).
Every other JSON response carries `timestamp` (UTC ISO-8601) and
`timestamp_unix`. All responses also carry an X-API-Timestamp header.

Stdlib only. Binds 127.0.0.1:5021 by default (MARKET_API_PORT env) --
port assigned to the exchange service by the deployment cell's
STANDARDS.md (5020=chain, 5021=exchange, 5022=swap, 5023=pool).
Conforms to STANDARDS.md: v1 success bodies carry top-level `timestamp`
(CoinGecko-schema routes are exempt and return bare arrays); ALL errors
use {"error","code","timestamp"}; GET/HEAD only, OPTIONS -> 204.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB_PATH = os.environ.get("MARKET_API_DB", "/home/shitcoin/user-accounts.db")
MARKETS_PATHS = [
    os.environ.get("MARKET_API_MARKETS", "/var/www/shitcoinbtc/assets/exchange-markets.json"),
    "/home/shitcoin/exchange-markets.json",
]
PORT = int(os.environ.get("MARKET_API_PORT", "5021"))


def now_iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dec(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def s(v):
    """Decimal -> plain string without exponent notation surprises."""
    d = dec(v)
    return format(d, "f")


def db_ro():
    con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def load_markets():
    for p in MARKETS_PATHS:
        if p and os.path.exists(p):
            with open(p) as f:
                d = json.load(f)
            out = []
            for m in d.get("markets", []):
                base = str(m["symbol"]).upper()
                quote = str(m.get("quote", "USDT")).upper()
                out.append({
                    "market": "%s/%s" % (base, quote),
                    "ticker_id": "%s_%s" % (base, quote),
                    "base_currency": base,
                    "target_currency": quote,
                    "base_name": m.get("name"),
                })
            out.sort(key=lambda x: x["market"])
            return out, p
    return [], None


def ticker_rows(con, markets):
    """One pass each: last trade, 24h vols, 24h open/high/low, own book."""
    cutoff = int(time.time()) - 86400
    last = {}
    for r in con.execute(
            "SELECT market, price, created_at FROM trades "
            "WHERE id IN (SELECT MAX(id) FROM trades GROUP BY market)"):
        last[str(r["market"]).upper()] = (str(r["price"]), int(r["created_at"]))
    vol24 = {}
    for r in con.execute(
            "SELECT market, COALESCE(SUM(CAST(amount AS REAL)),0) bv, "
            "COALESCE(SUM(CAST(amount AS REAL)*CAST(price AS REAL)),0) tv, "
            "MIN(CAST(price AS REAL)) lo, MAX(CAST(price AS REAL)) hi, "
            "COUNT(*) n FROM trades WHERE created_at >= ? GROUP BY market",
            (cutoff,)):
        vol24[str(r["market"]).upper()] = r
    book = {}
    for side, ordering in (("buy", "DESC"), ("sell", "ASC")):
        q = ("SELECT market, CAST(price AS REAL) p FROM orders "
             "WHERE status='open' AND side=? "
             "AND (type != 'stop-limit' OR stop_state='triggered') "
             "ORDER BY market, CAST(price AS REAL) " + ordering)
        for r in con.execute(q, (side,)):
            m = str(r["market"]).upper()
            book.setdefault(m, {}) .setdefault(side, r["p"])
    out = []
    for mk in markets:
        m = mk["market"]
        lp, lts = last.get(m, (None, None))
        v = vol24.get(m)
        entry = dict(mk)
        entry.update({
            "last_price": s(lp) if lp is not None else "0",
            "last_trade_at": now_iso(lts) if lts else None,
            "last_trade_timestamp": lts,
            "base_volume": s(v["bv"]) if v else "0",        # trailing 24h
            "target_volume": s(v["tv"]) if v else "0",      # trailing 24h
            "trades_24h": int(v["n"]) if v else 0,
            "high_24h": s(v["hi"]) if v else None,
            "low_24h": s(v["lo"]) if v else None,
            # No trades in window -> change is not computable: null, not 0%.
            "change_24h_pct": None,
            "bid": s(book[m]["buy"]) if m in book and "buy" in book[m] else None,
            "ask": s(book[m]["sell"]) if m in book and "sell" in book[m] else None,
            "liquidity_in_usd": "0",
        })
        out.append(entry)
    return out


def trade_list(con, market, limit):
    rows = con.execute(
        "SELECT id, market, price, amount, created_at FROM trades "
        "WHERE market=? ORDER BY id DESC LIMIT ?", (market, limit)).fetchall()
    out = []
    for r in rows:
        amt, px = dec(r["amount"]), dec(r["price"])
        out.append({
            "trade_id": int(r["id"]),
            "ticker_id": str(r["market"]).upper().replace("/", "_"),
            "market": str(r["market"]).upper(),
            "price": s(px),
            "base_volume": s(amt),
            "target_volume": s(amt * px),
            "timestamp": int(r["created_at"]),
            "timestamp_iso": now_iso(int(r["created_at"])),
        })
    return out


def parse_market_arg(qs):
    raw = (qs.get("market", [None])[0] or qs.get("ticker_id", [None])[0] or "")
    raw = raw.strip().upper().replace("_", "/")
    return raw or None


class H(BaseHTTPRequestHandler):
    server_version = "ShityDEXMarketAPI/1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200, head_only=False):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, max-age=15")
        self.send_header("X-API-Timestamp", now_iso())
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def send_error_shape(self, code, msg):
        # STANDARDS.md section 4 error shape, used for every error.
        return self.send_json({"error": msg, "code": code,
                               "timestamp": now_iso()}, code,
                              getattr(self, '_head', False))

    def envelope(self, data, **extra):
        e = {"timestamp": now_iso(), "timestamp_unix": int(time.time()),
             "source": "shitydex_own_records_read_only"}
        e.update(extra)
        e["data"] = data
        return e

    def _sj(self, obj, code=200):
        return self.send_json(obj, code, getattr(self, '_head', False))

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = parse_qs(u.query)
        try:
            limit = min(max(int(qs.get("limit", ["100"])[0]), 1), 1000)
        except ValueError:
            limit = 100
        try:
            if path == "/healthz":
                return self._sj({"ok": True, "timestamp": now_iso()})
            markets, msrc = load_markets()
            market_set = {m["market"] for m in markets}

            if path == "/api/coingecko/pairs":
                return self._sj([{
                    "ticker_id": m["ticker_id"],
                    "base": m["base_currency"],
                    "target": m["target_currency"],
                } for m in markets])

            if path in ("/api/coingecko/tickers", "/api/v1/exchange/tickers"):
                con = db_ro()
                try:
                    rows = ticker_rows(con, markets)
                finally:
                    con.close()
                if path.startswith("/api/coingecko"):
                    return self._sj([{
                        "ticker_id": r["ticker_id"],
                        "base_currency": r["base_currency"],
                        "target_currency": r["target_currency"],
                        "last_price": r["last_price"],
                        "last_trade_at": r["last_trade_at"],
                        "base_volume": r["base_volume"],
                        "target_volume": r["target_volume"],
                        "bid": r["bid"],
                        "ask": r["ask"],
                        "high": r["high_24h"],
                        "low": r["low_24h"],
                        "liquidity_in_usd": r["liquidity_in_usd"],
                    } for r in rows])
                return self._sj(self.envelope(rows, markets_source=msrc))

            if path in ("/api/coingecko/historical_trades",
                        "/api/v1/exchange/trades"):
                market = parse_market_arg(qs)
                if not market:
                    return self.send_error_shape(
                        400, "ticker_id or market parameter required")
                con = db_ro()
                try:
                    trades = trade_list(con, market, limit)
                    total = con.execute("SELECT COUNT(*) c FROM trades "
                                        "WHERE market=?", (market,)).fetchone()["c"]
                finally:
                    con.close()
                return self._sj(self.envelope(
                    trades, ticker_id=market.replace("/", "_"), market=market,
                    count=len(trades), total_recorded=int(total),
                    note="Side (buy/sell) is not recorded in the trades "
                         "table and is omitted rather than guessed."))

            if path == "/api/v1/exchange/markets":
                return self._sj(self.envelope(
                    markets, count=len(markets), markets_source=msrc))

            if path == "/api/v1/exchange/orderbook":
                market = parse_market_arg(qs) or "BTC/USDT"
                if market not in market_set and market != "EPIC/USDT":
                    return self.send_error_shape(400, "unknown market")
                con = db_ro()
                try:
                    def levels(side, ordering):
                        return [{"price": s(r["price"]), "size": s(r["size"])}
                                for r in con.execute(
                                    "SELECT price, SUM(CAST(amount AS REAL)"
                                    " - CAST(filled AS REAL)) AS size FROM orders "
                                    "WHERE market=? AND side=? AND status='open' "
                                    "AND (type != 'stop-limit' OR "
                                    "stop_state='triggered') GROUP BY price "
                                    "ORDER BY CAST(price AS REAL) " + ordering +
                                    " LIMIT ?", (market, side, limit))]
                    data = {"market": market,
                            "ticker_id": market.replace("/", "_"),
                            "bids": levels("buy", "DESC"),
                            "asks": levels("sell", "ASC"),
                            "depth_source": "shitydex_own_book_only"}
                finally:
                    con.close()
                return self._sj(self.envelope(data))

            if path == "/api/v1/exchange/assets":
                assets = {}
                for m in markets:
                    b = assets.setdefault(m["base_currency"], {
                        "symbol": m["base_currency"], "name": m["base_name"],
                        "base_in_markets": 0, "quote_in_markets": 0})
                    b["base_in_markets"] += 1
                    q = assets.setdefault(m["target_currency"], {
                        "symbol": m["target_currency"], "name": None,
                        "base_in_markets": 0, "quote_in_markets": 0})
                    q["quote_in_markets"] += 1
                rows = sorted(assets.values(), key=lambda x: x["symbol"])
                return self._sj(self.envelope(
                    rows, count=len(rows),
                    note="No per-asset deposit/withdraw flags are exposed: "
                         "none are verifiable from the markets config or a "
                         "read-only DB state (dex_tokens holds no enabled "
                         "tokens). Networks are not declared in "
                         "coin-coverage.json (it maps external reference "
                         "coverage only)."))

            if path == "/api/v1/exchange/status":
                con = db_ro()
                try:
                    def one(q, *a):
                        return con.execute(q, a).fetchone()[0]
                    total_trades = one("SELECT COUNT(*) FROM trades")
                    last_ts = one("SELECT MAX(created_at) FROM trades")
                    data = {
                        "status": "ok",
                        "service": "shitydex-market-data",
                        "api_version": "1",
                        "database": "readable_read_only",
                        "markets_count": len(markets),
                        "trades_recorded_total": int(total_trades),
                        "trades_last_24h": int(one(
                            "SELECT COUNT(*) FROM trades WHERE created_at>=?",
                            int(time.time()) - 86400)),
                        "last_trade_at": now_iso(last_ts) if last_ts else None,
                        "open_orders": int(one(
                            "SELECT COUNT(*) FROM orders WHERE status='open'")),
                        "orders_recorded_total": int(one(
                            "SELECT COUNT(*) FROM orders")),
                        "deposits_recorded_total": int(one(
                            "SELECT COUNT(*) FROM deposits")),
                        "withdrawals_recorded_total": int(one(
                            "SELECT COUNT(*) FROM withdrawals")),
                        "omitted_fields": "deposits_enabled / "
                            "withdrawals_enabled / trading_enabled are "
                            "omitted: this read-only service cannot verify "
                            "them from live state per request.",
                    }
                finally:
                    con.close()
                return self._sj(self.envelope(data))

            return self.send_error_shape(404, "not found")
        except Exception as e:  # never leak tracebacks / internals
            return self.send_error_shape(
                500, "internal error: %s" % type(e).__name__)

    def do_HEAD(self):
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        return self.send_error_shape(405, "read-only API: GET/HEAD only")

    do_PUT = do_DELETE = do_PATCH = do_POST


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
