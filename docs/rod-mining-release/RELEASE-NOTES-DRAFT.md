# Release notes (DRAFT — for the GitHub release, not yet published)

## Merge-mining setup release — real Bitcoin template, end to end

This release publishes the corrected mining setup for ShitcoinBTC (SHIT) merge mining
and for mining on the ShitcoinBTC pool. Its point, in one sentence: **all Bitcoin work
in this setup comes from a real Bitcoin Core node on mainnet via `getblocktemplate` —
there is no synthetic template path.**

### What changed

- **Real-Bitcoin mining guide** (`8b9cdb0`): production merge mining documented against
  real Bitcoin Core on mainnet, with a read-only node check script.
- **Verified pool node guide + checker** (`db13618`): independently verified (fixes
  F1–F4) and re-tested live. The checker asserts the real Bitcoin genesis hash, so it
  fails loudly if pointed at the wrong daemon — including a ShitcoinBTC daemon, which
  also reports its chain as "main".
- **`src/mine.sh` guarded to regtest only** (`8c2835d`): it presented a
  `generatetoaddress` loop as production mining; that is impossible on mainnet, where
  SHIT has required AuxPoW since block 1.
- **Auxpow getwork wrapper marked test-only** (`45fcc08`): invalid on mainnet.
- **README** (`643c124`): mining section now separates Bitcoin mainnet from
  ShitcoinBTC.
- **External pool operator package** (`docs/rod-mining-release/`): the standard
  `createauxblock`/`submitauxblock` integration, node setup, configuration
  requirements, upgrade steps from the October onboarding stack, and a root-cause
  note for the "old chain ID" rejection an operator hit on Oct 12 — the rejection
  occurs when pool software builds the SHIT header itself instead of using the node's
  auxpow interface; version rolling on the Bitcoin parent is unaffected (65,536
  rolled parents tested, zero rejected).

### Versions

- Node: `syscoind` v5.0.5, sha256 `a60885f26b2f78954e4979738e9e4b06dc9658803f79fc80ce3bcbd57472c1c3`,
  master `984349cc32162ce655622966c84acab10b56d196` + the five commits above.
- Reference Bitcoin node: official Bitcoin Core v31.1.0.
- No consensus changes in this release. This release contains source + docs +
  config (the mining setup itself); wallet binaries live in their own release.

### Verification

End-to-end proof was run against the live network before publication: pool stratum
jobs matched the real Bitcoin tip (template, prevhash lineage, difficulty) at
multiple heights, including checks against two public explorers; share submission,
duplicate handling, block submission, and payout accounting were exercised on the
deployed code. One limitation is disclosed in the operator package: our pool does
not yet negotiate BIP310 version rolling for ASICs pointed directly at it (they
mine, without the ASICBoost rolling efficiency); this does not affect external
pools, whose software controls its own parent job.

### Known gaps

- The assumeutxo snapshot (`utxo-935000.dat`) is not distributed yet and its full
  sha256 does not currently exist (the original file was deleted after loading; the
  guide states the surviving prefix and the safe way to proceed).
- The sysgeth fork source is not yet published.
