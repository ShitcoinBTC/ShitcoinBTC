# ShitcoinBTC Merge-Mining Release Package — for external pool operators

Prepared 2026-10-14 by the ShitcoinBTC team. **Delivered by hand by Jodie — this package
was not emailed or sent by the team.**

## What's in here

| File | What it is |
|---|---|
| `ROOT-CAUSE-NOTE.md` | Why the Oct 12 "old chain ID" rejection happened, in one page |
| `INTEGRATION.md` | The standard merge-mining flow: `createauxblock` / `submitauxblock`, version rolling, configuration requirements |
| `SETUP.md` | Node setup: the SHIT node, and the Bitcoin node (Option A snapshot / Option B existing Core) |
| `UPGRADE-FROM-OLD-SETUP.md` | Step-by-step from the October onboarding stack to this release |
| `POOL-NODE-GUIDE.md` | The verified pool node guide (Bitcoin side), as published in the repo branch below |
| `check-node.py` | Read-only checker for a Bitcoin node (verifies real mainnet identity, not just the chain name) |
| `HANDOVER-SUMMARY.md` | One-page summary of the whole package |
| `reference-pool/` | The complete reference pool implementation: `pool.py` (reference configuration — set your own payout/tax addresses) + `pool.conf.example` + its README |
| `RELEASE-MANIFEST.md` | Every component of the release, what's in the push, what attaches at publish, and what an operator does not need |

## Exact versions and commits

**SHIT node (production, all three network nodes run this identical build):**
- Binary: `syscoind` **v5.0.5**, sha256 `a60885f26b2f78954e4979738e9e4b06dc9658803f79fc80ce3bcbd57472c1c3`
  (md5 `fe6dbbf2d89c974361e0c5a8176265ed`), built 2026-10-03 from repo master.
- Source: https://github.com/ShitcoinBTC/ShitcoinBTC, master HEAD **`984349cc32162ce655622966c84acab10b56d196`**
  ("Fix macOS Qt bundling", v1.0.1).
- Consensus parameters that matter here: auxpow chain ID **16**, `fStrictChainId = true`,
  `nAuxpowStartHeight = 1` (merge mining mandatory from block 1), NEVM start height 30,316.
  **No consensus change is part of this release.** (Three unrelated consensus security patches
  exist on a local branch and are a separate, pending network decision — they are NOT in this
  package and nothing here depends on them.)

**Documentation / pool-integration commits** (branch `real-bitcoin-mainnet-docs`, on top of
master `984349c`):
- `8b9cdb0b50a4b6be29012071326886dee2fac412` — real Bitcoin mainnet mining guide + node check script
- `8c2835d5c99bba1b6a49010b0d4e78c04f4b42c4` — `src/mine.sh` guarded to regtest-only (it presented a
  `generatetoaddress` loop as production mining; that is impossible on mainnet — do not use it)
- `643c1245822d6b0c0375b2b443df44d7a2ff01cd` — README mining section separates Bitcoin mainnet from ShitcoinBTC
- `45fcc0808c03a47abc0967f6f51ca3b11a0408d5` — auxpow getwork wrapper marked test-only, invalid on mainnet
- `db1361830c95e308917baca38ce79a2f34d0374f` — verified pool node setup guide + checker (independent
  verification fixes F1–F4 applied and re-tested live)

**Honest publication status:** these five commits are published on this branch
(`release/rod-mining-2026-10-14-clean`) in the public repo. (When this package was
first prepared on 2026-10-14, the push had not yet happened.) The files in this package are byte-identical to the branch contents
(`check-node.py` sha256 `b0f2a373404ea678b379bb9f35f7793bd991dc8b6b2669566beee3de0dc2f1f0`,
verified identical in the branch, the working cell copy, and this package).

**Bitcoin node (reference):** official Bitcoin Core **v31.1.0**, chain `main`. Verify any
Core download against the official `SHA256SUMS` + GPG signatures from bitcoincore.org —
never run a bitcoind binary sent by anyone, including us.

**Our pool (for reference — you run your own pool; you do not need this to merge-mine):**
node2 pool software `pool.py` sha256 `1d15b29aa22f3dab66e2b138b4a27bf140b047cbacb7f7be33d73e36ed1b09ce`.

## Release checks performed (2026-10-14/15)

- [x] Node binary hash + version confirmed identical on all three production nodes (disk +
  running-process `/proc` exe).
- [x] Guide + checker independently verified and re-tested live: node3 Bitcoin Core **PASS**
  (with warnings); pointed at a SHIT daemon the checker **FAILs at the chain-identity step by
  design** — a ShitcoinBTC daemon also calls its chain "main", so the checker asserts the real
  Bitcoin genesis hash.
- [x] End-to-end proof on the live network: pool stratum jobs match node3's real
  `getblocktemplate` and the public chain at multiple heights (see the team's
  MAINNET-PROOF pack + ASIC-E2E-PROOF §2a).
- [x] Version-rolling behaviour tested live against our pool and documented honestly in
  `INTEGRATION.md` §4 (including the one gap found on OUR pool).
- [x] Configuration examples in `SETUP.md`/`INTEGRATION.md` checked against the deployed
  production configuration.
- [x] No credentials, keys, or tokens anywhere in this package.

## Known gaps, stated up front

1. **Snapshot full sha256 does not exist.** Our node's `utxo-935000.dat` was deleted after it
   loaded; only the prefix `e572ddbe…` survives. `SETUP.md` Option A tells you how to stay
   safe anyway (Core itself rejects a snapshot that doesn't match its built-in data).
2. **The sysgeth fork source is not published yet.** A pool operator starting from zero needs
   the sysgeth binary (the one supplied during the October onboarding) for the NEVM side of
   template construction. Publishing that source is an open item on our side.
3. **Our own pool does not negotiate BIP310 version rolling yet** (details in `INTEGRATION.md`
   §4). This affects miners pointing ASICs at *our* pool. It does not affect your pool: on
   your pool, your software issues the Bitcoin parent job and controls rolling itself.
