# Reference pool implementation (`pool.py`)

The pool software running in production on the ShitcoinBTC pool, included so the
release is complete: an external operator can run the same stack instead of (or
alongside) their own pool software.

- **File:** `pool.py` — sha256 `c62401c35252f153f7ef529edaa5d72b9728f74cbe6afa5a6bdaee854936e34b`.
  The production pool software, adapted for publication as a reference copy:
  the stratum, accounting and payout logic is the deployed code; the only
  differences are configuration. Daemon RPC endpoints default to localhost
  with placeholder credentials, and the coinbase payout/tax addresses are
  YOUR addresses, set in `pool.conf` — this copy pins no project address,
  and the pool refuses to start until you set both `POOL_BTC_ADDRESS` and
  `TAX_BTC_ADDRESS` to your own valid Bitcoin addresses (they may be the
  same address). 2,890 lines, Python 3, no credentials embedded — all
  secrets come from `pool.conf` (see `pool.conf.example`; never publish a
  filled-in `pool.conf`).
- **What it does:** stratum server (miner = BTC address, password `x`, optional
  `shit=<address>` in the password field to register a SHIT payout address —
  first registration wins and cannot be overwritten), real `getblocktemplate` work
  from your Bitcoin Core, PPLNS accounting for BTC + SHIT separately, hourly payout
  runs above threshold (0.001 BTC / 100 SHIT) behind the `PAYOUTS_ENABLED` flag,
  an exactly-once payout state machine (queued → sending → sent/unknown, reconciled
  against the wallet, never blindly resent), and a small dashboard.
- **Before you enable payouts:** create + fund your payout wallet, decide your
  solvency/maturity policy, and secure the payout RPC path. Our own production
  still runs with `PAYOUTS_ENABLED=false` pending exactly those operator decisions.
- **Known limitation, disclosed:** the stratum layer does not yet negotiate BIP310
  version rolling (`mining.configure` is not implemented; shares are validated
  against the pool-pinned template version). ASICs mine normally without it, at
  the usual un-rolled ASICBoost efficiency cost. See `INTEGRATION.md` §4.
- Templates for the dashboard live with the pool deployment (`templates/` in the
  pool directory on the host); the stratum + accounting core is this single file.
