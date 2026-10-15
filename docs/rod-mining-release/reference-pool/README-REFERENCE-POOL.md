# Reference pool implementation (`pool.py`)

The pool software running in production on the ShitcoinBTC pool, included so the
release is complete: an external operator can run the same stack instead of (or
alongside) their own pool software.

- **File:** `pool.py` — sha256 `1d15b29aa22f3dab66e2b138b4a27bf140b047cbacb7f7be33d73e36ed1b09ce`,
  byte-identical to the build deployed on node2 on 2026-10-14 (verified by hash at
  staging time). 2,880 lines, Python 3, no credentials embedded — all secrets come
  from `pool.conf` (see `pool.conf.example`; never publish a filled-in `pool.conf`).
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
