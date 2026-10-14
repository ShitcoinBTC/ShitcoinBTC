# shitydex_market_api / exchange_api.py — public read-only market-data service

Production path on node1: `/home/shitcoin/public-api/exchange_api.py`
Binds `127.0.0.1:5021` only (deploy STANDARDS.md §1). Stdlib only, no deps.
Unit (deployment cell installs): `shitcoin-public-exchange-api.service`,
start: `cd /home/shitcoin/public-api && MARKET_API_PORT=5021 python3 exchange_api.py`
Run as user `shitcoin` (needs read access to the DB + markets file; opens
the DB `mode=ro` per request and never writes).

## Data rules (non-negotiable)
- Sources: ShityDEX's own records only — markets file
  `/var/www/shitcoinbtc/assets/exchange-markets.json` (the same file
  user-api.py loads) + `trades`/`orders`/`deposits`/`withdrawals` counts
  in `/home/shitcoin/user-accounts.db` (read-only).
- `last_price` = most recent executed trade in `trades` for that market,
  with `last_trade_at`. No trade ever → `"0"` / null. Reference prices
  (prices table, CoinGecko, Kraken) are never used.
- 24h volumes = trades in the trailing 24h only. `change_24h_pct` is null
  (not computable without trades in the window).
- Order book = own open orders only; no external depth merged.
- `liquidity_in_usd` = `"0"` always in v1: the only `pools` row (id 1)
  is disabled in user-api.py (`AMM_POOLS_DISABLED`) and the public pools
  endpoint returns `[]` — no live pool backs any pair.
- Prices/amounts are strings (decimal precision, CoinGecko convention);
  counts are numbers. CoinGecko routes return bare arrays (their schema);
  `/api/v1/exchange/*` bodies carry top-level UTC `timestamp`.
  All errors: `{"error","code","timestamp"}`. GET/HEAD only; OPTIONS 204.
- Trade side is not in the `trades` table and is omitted; user ids in
  trade/order rows are never exposed. Ledger balances are never read.

## Routes
- `GET /api/coingecko/pairs` · `/api/coingecko/tickers` ·
  `/api/coingecko/historical_trades?ticker_id=BTC_USDT&limit=100`
- `GET /api/v1/exchange/markets|tickers|orderbook?market=BTC/USDT|trades?ticker_id=BTC_USDT|assets|status`
- `GET /healthz`
