# Integration — how merge mining SHIT actually works

Merge mining SHIT is standard AuxPoW, the same arrangement Dogecoin/Litecoin
merged mining uses, and it requires **no consensus change**. There are two
ways to integrate with the daemon, and as of this release only one of them
works — stated plainly:

- **Supported today: build the SHIT block in your pool software and submit
  it with `submitblock`.** This is the path the published reference pool
  (`reference-pool/pool.py`) and the chain's own block production use in
  production. The complete, authoritative integration reference for this
  path is the merge-mining integration guide:
  https://github.com/ShitcoinBTC/ShitcoinBTC/blob/docs/public-api-v1-2026-10-14-clean/doc/merge-mining-integration.md
- **Currently unavailable: the template RPCs.** The daemon ships the classic
  AuxPoW template calls (`createauxblock` / `submitauxblock`, and the
  `getauxblock` template call), but template creation through them fails on
  the current release with
  `CreateNewBlock: TestBlockValidity failed: no-auxpow` — the daemon
  validity-tests the template before the miner has attached any proof.
  This was verified against production nodes. A fix is in progress. Until
  it ships, do not build an integration on the template RPCs. When it
  lands, this document will be updated — and nothing about the
  `submitblock` path changes.

## 1. Who builds the SHIT block

**Today, your pool does.** Your pool constructs the SHIT block — coinbase,
header, and the NEVM commitment every current block must carry — commits
its hash into your Bitcoin parent coinbase, mines the parent, assembles
the AuxPoW, and submits the finished block to `syscoind` with
`submitblock`. The node validates everything at submission: a block with a
bad subsidy, the wrong difficulty, a missing NEVM commitment, or an
invalid AuxPoW is rejected, exactly as if the node had built it.

The template arrangement — the node builds the block and hands you only a
hash — is the classic AuxPoW flow, and it is what the template RPCs will
restore once they are fixed. The block header carries the chain ID (16)
either way; in the self-build path you set it yourself: header version
`0x20100180` (chain ID 16 + auxpow flag + NEVM flag + BIP9 top bits).

## 2. The flow (supported path)

1. **Build the SHIT block** for height tip + 1:
   - the coinbase pays the block subsidy to your own SHIT address,
   - the header carries version `0x20100180` and the exact consensus
     difficulty (`bits`) for the height,
   - the coinbase carries the NEVM witness commitment, built from a NEVM
     template fetched from your local `sysgeth` (which runs alongside
     `syscoind` as a template source — see `SETUP.md`).

   Byte-for-byte construction rules, the subsidy schedule, and the
   difficulty calculation are in the merge-mining integration guide
   linked above. A complete working implementation ships in this
   package: `reference-pool/pool.py`.
2. **Build the parent coinbase** — your Bitcoin (parent) coinbase transaction must carry
   the merged-mining commitment in its scriptSig, immediately in this order:
   - the 4-byte header `fabe6d6d` (`0xfabe` + "mm"),
   - the 32-byte hash of the SHIT block you built in step 1,
   - the 4-byte little-endian tree size (`1` when SHIT is the only aux chain),
   - the 4-byte little-endian nonce (0 for a single aux chain at slot 0).
   Pay the Bitcoin coinbase outputs however your pool normally pays them — SHIT
   consensus does not inspect your parent coinbase outputs.
3. **Mine the parent** — ASICs hash the **Bitcoin parent header** exactly as they would
   for solo Bitcoin mining. Version rolling (BIP310, mask `0x1fffe000`) applies HERE, on
   the parent, and is fully safe: SHIT validation places only a *negative* condition on
   the parent version (it must not itself claim chain ID 16/4096 — a plain Bitcoin header
   never does). This was verified exhaustively: **0 of 65,536 rolled parent versions
   rejected**, in two independent test harnesses plus a red-team run.
4. **Assemble and submit** — when a parent header meets the SHIT target
   (the difficulty committed in your SHIT block's `bits`), assemble the
   AuxPoW: parent coinbase transaction + parent block hash + coinbase
   merkle branch + chain merkle branch + parent header (the standard
   `CAuxPow` serialization every AuxPoW pool library already produces).
   Append the AuxPoW and the NEVM block data to the SHIT block and submit
   the block hex to your SHIT node:
   ```
   syscoin-cli submitblock <block-hex>
   ```
   If the parent also meets the Bitcoin target, submit it to Bitcoin Core
   (`submitblock`) as usual — the two submissions are independent.
5. **Verify acceptance** — a `null` result from `submitblock` means the
   block entered processing, not that it is yours. Count the block as
   yours only when `getblockhash <height>` returns your block's hash. If
   the tip moved while you were building, the block is stale: discard it
   and rebuild on the new tip.
6. **Payouts** — the SHIT coinbase pays the address in your coinbase
   (step 1). SHIT coinbase outputs mature after **100 confirmations**
   before they can be spent. Bitcoin pays per your own pool's coinbase.
   Nothing about SHIT changes your Bitcoin side.

## 3. Configuration requirements

- SHIT node fully synced and peered (check `getblockchaininfo`: `blocks == headers`,
  and compare the tip hash with any public SHIT explorer or a known peer).
- A local `sysgeth` serving NEVM templates (see `SETUP.md`). No reachable
  template means no valid SHIT block for that height — skip the height;
  never submit a block without the NEVM commitment.
- Pool software must rebuild the SHIT block **per new tip** (prevhash,
  difficulty, and NEVM template are all height-specific). You construct
  the SHIT header, but its version is fixed (`0x20100180`) — never roll
  or mutate it; version rolling happens on the parent only (§4). If your
  pool framework wants to treat SHIT as a "primary coin" with its own stratum job,
  that framework needs an auxpow mode: the stratum job miners receive is the **parent**
  job; SHIT never appears as a stratum job of its own.
- Standalone SHIT mining does not exist: since block 1, any SHIT block without valid
  AuxPoW is rejected by every node (`no-auxpow`). There is no configuration that changes
  this — it is consensus, and this release does not change consensus.

## 4. Version rolling — including the one gap we found on OUR side

- **On your pool:** rolling is yours to grant on the parent job. The full standard mask
  is safe (§2 step 3). Disabling rolling also works; it costs roughly 13–20% ASICBoost
  efficiency, nothing more — it does not zero anyone's hashrate.
- **On OUR pool** (node2, for miners who point ASICs at us directly): we found a real
  gap during this release's end-to-end testing, and we're disclosing it rather than
  shipping around it: our pool does not yet answer BIP310 `mining.configure`, and a
  rolled version field in a share is currently ignored (shares are validated against
  the pool-pinned template version). ASICs mine on our pool today — subscribe,
  authorize, real work, standard submits — but without ASICBoost rolling. The fix is
  scoped (stratum layer only, no consensus/template/payout changes) and is on our list.
  It has no effect on your pool, where your software controls the parent job.

## 5. What "correct" looks like — quick self-checks

- Your SHIT block for the next height carries header version `0x20100180`
  (chain ID 16) and a `bits` value equal to the consensus difficulty for
  that height.
- A winning parent submitted via `submitblock` returns `null`, and
  `getblockhash` returns your block's hash at that height.
- If `submitblock` rejects with `no-auxpow`, your submission is missing
  the AuxPoW or the auxpow flag in the header version — standalone blocks
  are never accepted (§3).
- If you ever see `block does not have our old chain ID (got 0, expected 4096, …)`:
  your parent header is claiming the legacy chain ID. See `ROOT-CAUSE-NOTE.md`.
