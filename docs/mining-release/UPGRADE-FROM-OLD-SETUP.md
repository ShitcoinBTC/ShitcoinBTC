# Upgrade steps — from the October onboarding stack to this release

Starting point (the last configuration we have a record of, 2026-10-05): SHIT node
synced from the fast-sync snapshot (height 50,760) with the sysgeth wrapper `"$@"`
passthrough fixed, no `zmqpubnevm` on the pool node, peered with the network. If your
current stack differs, step 1 tells you how to check before changing anything.

## Steps

1. **Confirm what you are running.**
   ```
   syscoin-cli getnetworkinfo     # note "version" / "subversion"
   sha256sum $(which syscoind)    # this release: a60885f26b2f78954e4979738e9e4b06dc9658803f79fc80ce3bcbd57472c1c3
   syscoin-cli getblockchaininfo  # blocks == headers? tip matching the public chain?
   ```
   If the hash differs, you are on a different build than the network's reference
   build — replace it with the release build above before continuing (back up your
   datadir and wallet first; a binary swap needs no re-sync).
2. **Keep the node configuration from October** — it was correct once the two wrapper
   bugs were fixed: no `zmqpubnevm` on the pool node; if a sysgeth wrapper exists,
   verify it forwards `"$@"`. Do not re-enable ZMQ/NEVM validation on the pool node;
   that was the block-48,000 stall, not a requirement.
3. **Change the pool integration — this is the actual upgrade.** Remove any code path
   that constructs a SHIT block header, a SHIT stratum job, or a SHIT `nVersion` in
   your pool software. Replace it with the standard flow in `INTEGRATION.md`:
   `createauxblock <payout-address>` → put the returned hash in your Bitcoin parent
   coinbase after the `fabe6d6d` header → mine the parent → `submitauxblock`.
   There is no version field anywhere in your SHIT handling after this change, so
   there is nothing left for version rolling to break.
4. **Leave your Bitcoin side alone.** Your ASICs keep hashing your Bitcoin parent job;
   BIP310 version rolling on the parent stays enabled with whatever mask your
   software grants (the full `0x1fffe000` is safe — see `INTEGRATION.md` §2 step 3).
   If your Bitcoin node is new or was ever pointed at a test chain, run
   `check-node.py` once (it fails loudly on a non-Bitcoin daemon — including a SHIT
   daemon, which also calls itself "main").
5. **Do not use the old shortcuts.** `generatetoaddress`-style "mining" and the
   getwork auxpow wrapper in the repo are test tools; on mainnet they cannot produce
   a valid block (SHIT has required AuxPoW since block 1). The release commits guard
   `src/mine.sh` to regtest and mark the wrapper test-only.
6. **Verify end to end before pointing real hashrate at it:**
   - `createauxblock` returns `"chainid": 16` and `height` = tip + 1.
   - Mine a parent at low difficulty on your side if your software allows a test
     target; submit via `submitauxblock`; confirm the SHIT block is accepted
     (`getblockhash <height>` returns it on your node and a second node/explorer).
   - Confirm a rolled parent version on a normal Bitcoin share is accepted by your
     own pool as usual (nothing about SHIT touches this path).
7. **Drop the standalone plan.** A SHIT pool without AuxPoW produces blocks every
   node rejects (`no-auxpow`). There is nothing to configure around it — merge mining
   through step 3 is the integration.

## What did NOT change (so you don't go looking for it)

- Consensus: chain ID 16, strict mode, AuxPoW mandatory from block 1 — identical in
  the October build and this release. No fork, no activation height, nothing to
  coordinate. (A separate consensus *security* patch set exists on our side; it is
  not part of this release and merge mining works the same with or without it.)
- Your grant, your payout address, and your pool's economics: untouched by this
  release. The merge-mined Bitcoin is still yours per your own coinbase; SHIT pays
  the address you pass to `createauxblock`.
