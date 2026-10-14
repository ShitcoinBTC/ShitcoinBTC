# Public API services (v1) — deployed source

Source of the four read-only public API services live at https://shitcoinbtc.xyz since 2026-10-14. Public documentation: doc/public-api.md (and https://shitcoinbtc.xyz/api-docs).

| File | Deployed as | Host | Listen | Public base |
|---|---|---|---|---|
| chain_api.py | shitcoin-public-chain-api.service | node1 | 127.0.0.1:5020 | /api/v1/chain/ |
| shitydex_market_api.py | shitcoin-public-exchange-api.service (deployed path /home/shitcoin/public-api/exchange_api.py; see exchange-README.md) | node1 | 127.0.0.1:5021 | /api/v1/exchange/ + /api/coingecko/ |
| swap_api.py | shity-swap-api.service | node1 | 127.0.0.1:8793 | /api/v1/swap/ |
| pool_api.py | shitcoin-pool-api.service | node2 | :8091 (firewalled to node1) | /api/v1/pool/ (proxied via node1 nginx) |

All services are read-only: chain data from the node's own RPC, exchange data from the accounts DB opened mode=ro, swap quotes live-probed from the same venues the site uses (quotes are labelled as quotes, never as completed swaps), pool data as aggregates only (never miner addresses or balances). Error responses use the shared shape {error, code, timestamp}.

verify_exchange_api.py is the exchange service's on-box field-by-field verifier (checks responses against read-only SQL ground truth).

Conformance fixes 2026-10-14 (D2/D3): unknown transaction IDs return 404 (not 500); swap and pool error envelopes carry {error, code, timestamp}. These copies are byte-identical to the deployed files as of that fix (md5-verified at deploy time).
