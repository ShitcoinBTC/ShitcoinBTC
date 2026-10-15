# Integration — how merge mining SHIT actually works

This is the standard AuxPoW flow, the same arrangement Dogecoin/Litecoin merged mining
uses. It requires **no consensus change** and works on the production network today.

## 1. The one rule everything follows from

**The node builds the SHIT block. Your pool never does.**

The SHIT block header carries the chain ID (16) and is constructed inside `syscoind`.
Your pool's job is only: (a) ask the node for merge-mining work, (b) put the returned
block hash into your Bitcoin parent coinbase, (c) mine the parent with your ASICs,
(d) hand the winning parent proof back to the node.

## 2. The flow

1. **Get child work** — call on your SHIT node:
   ```
   syscoin-cli createauxblock <your-shit-payout-address>
   → { "hash", "chainid": 16, "previousblockhash", "coinbasevalue",
       "coinbasescript", "bits", "height", "_target" }
   ```
   Note what is **not** in the response: there is no header and no `nVersion` to hash,
   roll, or modify. The node keeps the header it built (version `0x20100180` — chain ID
   16 + auxpow flag + NEVM flag + BIP9 top bits).
2. **Build the parent coinbase** — your Bitcoin (parent) coinbase transaction must carry
   the merged-mining commitment in its scriptSig, immediately in this order:
   - the 4-byte header `fabe6d6d` (`0xfabe` + "mm"),
   - the 32-byte child block hash returned in step 1,
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
4. **Submit the proof** — when a parent header meets the SHIT target (from step 1), call:
   ```
   syscoin-cli submitauxblock <hash-from-step-1> <auxpow-hex>
   ```
   where the auxpow is: parent coinbase transaction + parent block hash + coinbase
   merkle branch + chain merkle branch + parent header (the standard
   `CAuxPow` serialization every AuxPoW pool library already produces).
   If the parent also meets the Bitcoin target, submit it to Bitcoin Core
   (`submitblock`) as usual — the two submissions are independent.
5. **Payouts** — the SHIT coinbase pays the address you passed in step 1
   (`coinbasevalue` from the template). Bitcoin pays per your own pool's coinbase.
   Nothing about SHIT changes your Bitcoin side.

## 3. Configuration requirements

- SHIT node fully synced and peered (check `getblockchaininfo`: `blocks == headers`,
  and compare the tip hash with any public SHIT explorer or a known peer).
- Pool software must call `createauxblock` **per new tip** (the hash is only valid for
  the template it was issued for) and must treat the returned hash as opaque bytes.
- **Never** construct, mutate, or version-roll a SHIT header in pool software. If your
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

- Your `createauxblock` result shows `"chainid": 16` and a fresh `"height"` = tip + 1.
- A winning parent submitted via `submitauxblock` returns without a chain-ID error,
  and the block appears in `getblocktemplate`'s chain (verify with `getblockhash`).
- If you ever see `block does not have our old chain ID (got 0, expected 4096, …)`:
  your software built or rolled a child header. See `ROOT-CAUSE-NOTE.md`.
