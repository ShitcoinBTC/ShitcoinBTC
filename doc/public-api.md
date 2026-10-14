# ShitcoinBTC Public APIs (v1)

Public, read-only HTTP APIs for the ShitcoinBTC chain, the ShityDEX exchange,
Shity Swap, and the mining pool, served from https://shitcoinbtc.xyz.

The canonical, rendered version of this document is the public docs page at
https://shitcoinbtc.xyz/api-docs. This file mirrors it for the repository.

> Status (2026-10-14): the signed Exchange Trading API v1 (section 6) is live.
> The Chain, Exchange Market Data, CoinGecko, Pool, and Swap APIs
> (sections 2–5 and 7) are built and in staged deployment — endpoint contracts
> below are as built by the owning services, and each section goes live as its
> deployment is verified.
>
> This document is about the website's public HTTP APIs. It is not the node
> daemon's own interfaces — those are [JSON-RPC](JSON-RPC-interface.md) and the
> daemon [REST interface](REST-interface.md) (enabled with `-rest`, port 8370).

## 1. Common rules

- Base URL: `https://shitcoinbtc.xyz` (HTTPS only).
- Versioning: public APIs live under `/api/v1/`. Breaking changes ship as
  `/api/v2/`; v1 paths are not silently repurposed. The CoinGecko-schema
  aliases under `/api/coingecko/` follow CoinGecko's unversioned convention.
- Methods: `GET` / `HEAD` only on the public read APIs; other methods return 405.
- Timestamps: every success response from the `/api/v1/` public APIs carries a
  top-level `timestamp` field — UTC, ISO-8601 with `Z`, e.g.
  `2026-10-14T18:40:47Z` — generated when the response is produced.
  CoinGecko-schema responses keep CoinGecko's shape. The signed trading API
  uses Unix milliseconds in its CCXT-shaped payloads.
- Rate limits (public read APIs): 60 requests/minute per IP, short bursts to
  20. Responses are cached at the edge for 15 seconds. Over the limit: HTTP 429
  in the error shape below. The signed trading API has its own limits (section 6).
- CORS: public read endpoints send `Access-Control-Allow-Origin: *`. No
  credentials are used or accepted on public reads.
- Errors (public read APIs) always use this shape, never an HTML page:

```json
{"error": "human-readable message", "code": 404, "timestamp": "2026-10-14T18:40:47Z"}
```

400 = bad parameter, 404 = unknown route or missing block/transaction,
405 = wrong method, 429 = rate limited, 502/503 = data source unavailable.

Data rule for every API below: numbers come only from the system described —
chain data from the node's own RPC, exchange data from the exchange's own
executed trades and open orders, pool data from the pool's own records. Where
there is no data, the APIs return zero, empty, or null.

## 2. Chain API — `/api/v1/chain/`

Read-only. Source: the node's own RPC only.

| Endpoint | Description |
|---|---|
| `GET /api/v1/chain/status` | Tip (height, hash), difficulty, peers, node/protocol version, and NEVM reporting state. The `nevm` object reports both sources (node NEVM info; execution-client chain id and head) exactly as they answer — fields can be null. NEVM chain id is 20026. |
| `GET /api/v1/chain/block/{hashOrHeight}` | One block by height or 64-char hex hash, as `{"block": {...}, "timestamp": ...}` (verbose node block object, raw AuxPoW omitted). Out-of-range height → 404; malformed id → 400. |
| `GET /api/v1/chain/tx/{txid}` | One decoded transaction as `{"tx": {...}, "timestamp": ...}`. Unknown txid → 404. |
| `GET /api/v1/chain/supply` | Supply with methods stated in the response. `circulating_supply` = sum of all unspent outputs at the tip (UTXO-set total — the only on-chain-measured figure). `total_issued_supply` = 8,400,000,000 block-1 allocation (3.78B presale + 2.94B team + 0.42B coin support + 1.26B wrapping reserve) + per-block coinbase issuance from block 2, decaying 2% every 1,051,200 blocks. `max_supply` = 21,000,000,000. The 17% vault yield reserve (3.57B) is protocol-tracked, not in the UTXO set, and excluded from both figures. Burned/unspendable outputs are issued but not circulating; the response reports the difference. |
| `GET /api/v1/chain/stats` | Height, difficulty, chainwork, total tx count/rate, UTXO counts and total, network hash rate as reported by the node, mempool size/bytes/total fee, 30-second target block time. |

Example (values sampled from the production node, 2026-10-14):

```bash
curl https://shitcoinbtc.xyz/api/v1/chain/supply
```
```json
{
  "height": 77924, "max_supply": 21000000000, "max_supply_unit": "SHIT",
  "circulating_supply": "8413890917.76182027",
  "utxo_set": {"txouts": 114133, "transactions": 78065, "height": 77924},
  "timestamp": "2026-10-14T18:50:35Z"
}
```

## 3. Exchange market data — `/api/v1/exchange/`

Public market data for ShityDEX, built only from the exchange's own records:
its markets list, its executed trades, and its currently open orders. Prices
from other exchanges are never used as a last price, volume, bid, or ask.

Truth note: the exchange's recorded trade history is 34 executed trades, all
from 2026-09-27. Trailing-24h volume is therefore 0 on every market at the time
of writing. `last_price` is the most recent executed trade's price, always
paired with `last_trade_at`. Markets with no recorded trade report
`last_price "0"` and `last_trade_at null`. `liquidity_in_usd` is `"0"` — no
live pool backs any pair.

Responses wrap data in an envelope:
`{"timestamp", "timestamp_unix", "source": "shitydex_own_records_read_only", ..., "data": ...}`.
Prices and volumes are decimal strings.

| Endpoint | Description |
|---|---|
| `GET /api/v1/exchange/status` | Service health and record counts: markets count, total recorded trades, trades in last 24h, most recent trade time, open-order count. |
| `GET /api/v1/exchange/markets` | Markets from the same configuration the exchange loads: `market`, `ticker_id`, `base_currency`, `target_currency`, `base_name`. |
| `GET /api/v1/exchange/tickers` | Per market: `last_price`, `last_trade_at`, `last_trade_timestamp`; trailing-24h `base_volume`, `target_volume`, `trades_24h`, `high_24h`, `low_24h`; best `bid`/`ask` from the exchange's own open orders (null when a side is empty); `change_24h_pct` null when there were no trades in the window; `liquidity_in_usd`. |
| `GET /api/v1/exchange/orderbook?market=BTC/USDT` | Own open orders aggregated by price level (`{"price","size"}` string pairs), `depth_source: "shitydex_own_book_only"`. No external depth is merged. |
| `GET /api/v1/exchange/trades?ticker_id=BTC_USDT&limit=100` | Executed trades, newest first; `limit` 1–1000 (default 100). Fields: `trade_id`, `ticker_id`, `market`, `price`, `base_volume`, `target_volume`, `timestamp` (Unix seconds), `timestamp_iso`. Trade side is not recorded in the trades table and is omitted rather than guessed. |
| `GET /api/v1/exchange/assets` | Assets in the markets list with base/quote market counts. Per-asset deposit/withdrawal availability is not exposed — it is not verifiable from the sources this service reads, so it is not claimed. |

Example (real records):

```bash
curl 'https://shitcoinbtc.xyz/api/v1/exchange/trades?ticker_id=BTC_USDT&limit=2'
```
```json
{
  "timestamp": "2026-10-14T18:50:35Z", "ticker_id": "BTC_USDT",
  "market": "BTC/USDT", "count": 2, "total_recorded": 34,
  "data": [
    {"trade_id": 209, "ticker_id": "BTC_USDT", "market": "BTC/USDT",
     "price": "80000", "base_volume": "0.006", "target_volume": "480",
     "timestamp": 1790505868, "timestamp_iso": "2026-09-27T10:44:28Z"},
    {"trade_id": 208, "ticker_id": "BTC_USDT", "market": "BTC/USDT",
     "price": "80000", "base_volume": "0.004", "target_volume": "320",
     "timestamp": 1790505868, "timestamp_iso": "2026-09-27T10:44:28Z"}
  ]
}
```

## 4. CoinGecko-schema aliases — `/api/coingecko/`

The same exchange records in CoinGecko's response shapes (bare JSON, no
envelope). The data rules of section 3 apply unchanged.

| Endpoint | Description |
|---|---|
| `GET /api/coingecko/pairs` | `[{"ticker_id": "BTC_USDT", "base": "BTC", "target": "USDT"}, ...]` |
| `GET /api/coingecko/tickers` | Per ticker: `ticker_id`, `base_currency`, `target_currency`, `last_price`, `last_trade_at`, `base_volume`, `target_volume`, `bid`, `ask`, `high`, `low`, `liquidity_in_usd`. |
| `GET /api/coingecko/historical_trades?ticker_id=BTC_USDT&limit=100` | Same records and parameters as `/api/v1/exchange/trades`. |

## 5. Mining pool API — `/api/v1/pool/`

Aggregate statistics for the mining pool: a Bitcoin stratum pool (stratum host
and port are in the `/status` response) with SHIT merge-mining. Aggregates from
the pool's own records only. This API never returns miner or payout addresses,
per-miner balances or earnings, worker names, block finders, or connection IPs.

Current state, stated plainly (2026-10-14): 0 miners online, 0 hashrate,
payouts are NOT enabled (`payouts_enabled: false` — no payout will occur), the
SHIT merge-mining aux feed reports `waiting` (NEVM feed unreachable), and the
Bitcoin feed state can move between ready and down during upstream RPC timeout
windows; `/status` includes the feed's `last_ready_at` so a timeout window is
not mistaken for a dead feed. Share history is limited by the pool's own 7-day
share retention.

| Endpoint | Description |
|---|---|
| `GET /api/v1/pool/status` | Pool identity, stratum host/port, Bitcoin feed health (`state`, `btc_tip_height`, `last_ready_at`), current job, SHIT aux state, `payouts_enabled`. |
| `GET /api/v1/pool/hashrate` | `hashrate_hps` (600 s window), `hashrate_1h_hps`, `accepted_shares_in_window`, `window_seconds`, `method`. Method = sum of accepted share difficulty in window × 2^32 ÷ window seconds (the pool's own definition). No accepted shares → 0. |
| `GET /api/v1/pool/hashrate/history?bucket=3600` | Bucketed history; `bucket` ∈ {600, 3600, 86400}, default 3600. Coverage ≤ 7-day share retention. |
| `GET /api/v1/pool/workers` | Aggregate counts only: distinct miners online, workers online (pool's 600 s last-seen window), total worker rows (includes test-era registrations; not a miner count). |
| `GET /api/v1/pool/shares` | Recorded accepted/rejected totals (all-time and 24h) plus the pool process's in-memory counters; the response explains why they can differ. Rejects are recorded without a reason, so none are reported. |
| `GET /api/v1/pool/blocks?limit=50` | Recorded pool blocks: chain, height, hash, time; totals by chain. `limit` 1–200, default 50. Finder data is not returned. |
| `GET /api/v1/pool/fees` | The pool's configured fee and payout parameters as data fields, exactly as configured in the pool software. |
| `GET /api/v1/pool/payouts` | Payout configuration and the live `payouts_enabled` flag. No per-miner balances or payout rows. |

Note: `/api/mine/` on the website is a separate, older service for the SHIT
web-miner pool — not this stratum pool API.

## 6. Exchange trading API v1 — signed (live)

The existing authenticated trading API. Base URL
`https://shitcoinbtc.xyz/api/v1`. Spot only. SHIT is not listed on the exchange.
Response shapes follow the CCXT structure; a CCXT exchange class is provided at
https://shitcoinbtc.xyz/assets/shitcoinbtc_ccxt.py.

- Keys: log in (`POST /api/user/login`) for a session token, then
  `POST /api/v1/keys` with `{name, permissions}` (`trade` default, or `read`).
  The response's `secret` (prefix `scbsk_`; key id prefix `scb_live_`) is shown
  exactly once. List/revoke: `GET` / `DELETE /api/v1/keys`.
- Signing: headers `X-API-KEY`, `X-API-TIMESTAMP` (Unix ms), `X-API-SIGN` =
  hex HMAC-SHA256 over `TIMESTAMP + "\n" + METHOD + "\n" + PATH + "\n" + QUERY + "\n" + BODY`.
  Timestamp must be within ±60 s of server time and strictly greater than the
  key's last used timestamp (replay protection).
- Limits: public 300/min per IP; private 600/min per key; order placement
  120/min per user; cancellation 240/min per user; key management 30/hour per
  user. An edge rate limit also applies to the whole `/api/v1/` path.
  Errors: `{"ok": false, "error": "code", "message": "..."}` with HTTP
  400/401/403/404/429 as applicable.
- Public: `GET /api/v1/markets` (70 markets at the time of writing; per-market
  fee and limit values are returned as data fields — read them from the
  response), `GET /api/v1/ticker?symbol=BTC/USDT`,
  `GET /api/v1/orderbook?symbol=BTC/USDT&limit=50` (1–200, default 50),
  `GET /api/v1/trades?symbol=BTC/USDT&limit=50` (1–500, default 50; public
  trades carry `side: null`).
  - Legacy ticker caveat: `high`/`low`/`open` and volumes come from the
    exchange's own trades (null/0 when none in the window), but `last`/`close`
    on this legacy endpoint can reflect a reference price rather than the most
    recent executed trade. For an executed-trade last price with its trade
    time, use `/api/v1/exchange/tickers` (section 3).
- Private (signed): `GET /api/v1/account`; `POST /api/v1/order` (limit orders
  only; fields `symbol`, `side`, `amount`, `price`, optional `type: "limit"`,
  `timeInForce` GTC/IOC/FOK); `DELETE /api/v1/order?id=...`;
  `DELETE /api/v1/orders?symbol=...`; `GET /api/v1/order?id=...`;
  `GET /api/v1/orders`; `GET /api/v1/orders/history`;
  `GET /api/v1/trades/mine` (own fills with per-fill fees).

Corrections recorded 2026-10-14: the markets count is 70 (an earlier docs
version said 58, and its ticker example showed numbers that did not match the
live tape — examples are now real sampled responses).

## 7. Swap API — `/api/v1/swap/`

Shity Swap is non-custodial: swaps execute in the user's own wallet against
external aggregator venues. This read-only API reports quote availability and
live quotes from the same venues the swap page uses. It never builds
transactions, signs, or places orders.

Quotes are not swaps. Every quote response is labeled `"type": "quote"` and
`"is_completed_swap": false`. The platform keeps no swap ledger, so there is
no completed-swaps feed to publish (see `/swaps` below). A chain is listed as
available only when a live probe quote for it returned a non-zero output
amount at probe time (probes cached 60 s); every other chain is listed as
unavailable, with the probe failure as the reason.

| Endpoint | Description |
|---|---|
| `GET /api/v1/swap/chains` | Per-chain availability, probed live: status (`available`/`unavailable`), answering venue, probe result or failure reason; plus `available_count` / `unavailable_count`. Availability is a probe result — it changes; do not cache it as permanent truth. |
| `GET /api/v1/swap/pairs?chain=ethereum` | Only pairs verified quotable by a live probe, each marked with that basis. Not an enumeration of every pair the venues can price. |
| `GET /api/v1/swap/quote?chain=&tokenIn=&tokenOut=&amountIn=` | A live quote from the chain's venue. `tokenIn` is `native` or a token address (a mint on Solana); `amountIn` is an integer in smallest units. A quote is a price offer at that moment — not a completed swap, not a trade record, not a volume figure. |
| `GET /api/v1/swap/liquidity?chain=` | On chains with a Shity Swap own AMM factory: the factory's pair count read on-chain (0 = no own-pool liquidity). Elsewhere: an explanation that aggregator liquidity is not exposed as a single verifiable number. No TVL figure is published, because none can be verified. |
| `GET /api/v1/swap/fees` | Per-venue fee configuration as data fields only, each with the source it was read from. This API charges nothing and executes nothing. |
| `GET /api/v1/swap/swaps` | Completed swaps: an empty list, with the explanation that no swap ledger exists (non-custodial; the page's "recent swaps" list is per-browser storage), plus the verifiable on-chain state of the own-router factories — zero created pairs on Monad, HyperEVM, and Avalanche, so no own-router swap can have occurred there. |

Probe record, 2026-10-14: Velora returned quotes on Ethereum, Base, BNB
Chain, Arbitrum, Avalanche, Optimism, Gnosis, Unichain, Polygon, and
Robinhood Chain (Sonic returned a service error); Kyber returned quotes on
Polygon, Optimism, Unichain, Linea, and Robinhood Chain; Jupiter returned a
quote on Solana. One probed aggregator (OpenOcean) returned access errors, so
chains whose only quote path is OpenOcean report unavailable. Shity Swap's
own-router contracts on the chains sampled report zero created pairs, so no
own-router pairs are claimed.
