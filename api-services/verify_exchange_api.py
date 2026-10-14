#!/usr/bin/env python3
"""Side-by-side verifier: exchange market API (127.0.0.1:5021) vs SQL ground
truth from user-accounts.db opened mode=ro. Run ON node1.
Prints PASS/FAIL lines; exit 1 if any FAIL."""
import json, sqlite3, sys, time, urllib.request

B = "http://127.0.0.1:5021"
def get(p):
    with urllib.request.urlopen(B + p, timeout=10) as r:
        return r.status, json.load(r)

fails = []
def check(name, api, sql):
    ok = api == sql
    print("%s %-46s api=%r sql=%r" % ("PASS" if ok else "FAIL", name, api, sql))
    if not ok: fails.append(name)

con = sqlite3.connect("file:/home/shitcoin/user-accounts.db?mode=ro", uri=True)
q = lambda sql, a=(): con.execute(sql, a).fetchone()[0]

_, pairs = get("/api/coingecko/pairs")
check("pairs count == markets in exchange-markets.json",
      len(pairs), len(json.load(open("/var/www/shitcoinbtc/assets/exchange-markets.json"))["markets"]))

_, tickers = get("/api/coingecko/tickers")
btc = next(t for t in tickers if t["ticker_id"] == "BTC_USDT")
last = con.execute("SELECT price, created_at FROM trades WHERE market='BTC/USDT' ORDER BY id DESC LIMIT 1").fetchone()
check("BTC_USDT last_price == last recorded trade price (numeric)", float(btc["last_price"]), float(last[0]))
import datetime
check("BTC_USDT last_trade_at == last trade ts (UTC)",
      btc["last_trade_at"], datetime.datetime.fromtimestamp(last[1], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
check("BTC_USDT base_volume 24h == SQL SUM (0)", float(btc["base_volume"]),
      float(q("SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM trades WHERE market='BTC/USDT' AND created_at>=?", (int(time.time())-86400,))))
check("BTC_USDT target_volume 24h == 0", float(btc["target_volume"]), 0.0)
check("BTC_USDT bid == best own open bid", float(btc["bid"]),
      float(q("SELECT MAX(CAST(price AS REAL)) FROM orders WHERE market='BTC/USDT' AND side='buy' AND status='open'")))
check("BTC_USDT ask is null (no open sells)", btc["ask"],
      q("SELECT COUNT(*) FROM orders WHERE market='BTC/USDT' AND side='sell' AND status='open'") and "x" or None)
check("BTC_USDT liquidity_in_usd == 0 (pool 1 disabled)", btc["liquidity_in_usd"], "0")
eth = next(t for t in tickers if t["ticker_id"] == "ETH_USDT")
check("ETH_USDT last_price == 0 (no trades ever)", eth["last_price"],
      "0" if q("SELECT COUNT(*) FROM trades WHERE market='ETH/USDT'") == 0 else "nonzero-expected")
check("all tickers base_volume == per-market SQL 24h sum",
      all(float(t["base_volume"]) == float(q("SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM trades WHERE market=? AND created_at>=?",
          (t["ticker_id"].replace("_", "/"), int(time.time())-86400))) for t in tickers), True)

_, hist = get("/api/coingecko/historical_trades?ticker_id=BTC_USDT&limit=1000")
check("historical_trades BTC_USDT count == SQL", hist["count"], q("SELECT COUNT(*) FROM trades WHERE market='BTC/USDT'"))
check("historical_trades total_recorded == SQL", hist["total_recorded"], q("SELECT COUNT(*) FROM trades WHERE market='BTC/USDT'"))
_, hist_epic = get("/api/coingecko/historical_trades?ticker_id=EPIC_USDT")
check("historical_trades EPIC_USDT count == SQL (orphan market)", hist_epic["count"], q("SELECT COUNT(*) FROM trades WHERE market='EPIC/USDT'"))

_, ob = get("/api/v1/exchange/orderbook?market=BTC/USDT")
own = con.execute("SELECT price, SUM(CAST(amount AS REAL)-CAST(filled AS REAL)) FROM orders WHERE market='BTC/USDT' AND side='buy' AND status='open' GROUP BY price ORDER BY CAST(price AS REAL) DESC").fetchall()
check("orderbook bid levels == SQL grouped open buys", len(ob["data"]["bids"]), len(own))
check("orderbook asks empty == SQL", ob["data"]["asks"], [])
check("orderbook top bid size == SQL remaining", float(ob["data"]["bids"][0]["size"]), float(own[0][1]))

_, st = get("/api/v1/exchange/status")
d = st["data"]
check("status trades_recorded_total", d["trades_recorded_total"], q("SELECT COUNT(*) FROM trades"))
check("status trades_last_24h", d["trades_last_24h"], q("SELECT COUNT(*) FROM trades WHERE created_at>=?", (int(time.time())-86400,)))
check("status open_orders", d["open_orders"], q("SELECT COUNT(*) FROM orders WHERE status='open'"))
check("status deposits_recorded_total", d["deposits_recorded_total"], q("SELECT COUNT(*) FROM deposits"))
check("status withdrawals_recorded_total", d["withdrawals_recorded_total"], q("SELECT COUNT(*) FROM withdrawals"))
check("status has UTC timestamp", "timestamp" in st and st["timestamp"].endswith("Z"), True)
blob = json.dumps([pairs, tickers, hist, ob, st])
for bad in ("buyer_id", "seller_id", "kraken", "Kraken", "privkey", "user_id"):
    check("privacy: %r absent from public responses" % bad, bad in blob, False)

print("\nRESULT:", "ALL PASS" if not fails else "FAILURES: %s" % fails)
sys.exit(1 if fails else 0)
