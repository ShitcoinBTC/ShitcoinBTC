# Merge-Mining Integration Guide — ShitcoinBTC (SHIT)

**For:** any Bitcoin mining pool operator who wants to merge-mine ShitcoinBTC.
**Status:** October 2026. This guide describes the integration path that is
verified working in production today: you build the SHIT block yourself and
submit it with its AuxPoW attached via `submitblock`. It is the exact path
used by the published reference pool
(`docs/rod-mining-release/reference-pool/pool.py` on the
`release/rod-mining-2026-10-14-clean` branch of this repository) and by the
chain's own block production.

ShitcoinBTC is merge-mined only. Since block 1, consensus rejects every SHIT
block that does not carry a valid AuxPoW anchored in a Bitcoin parent block —
there is no standalone SHIT mining. You mine Bitcoin, you get SHIT for free:
no extra hash, no extra energy, one additional block to build and submit per
parent solution.

> **About `createauxblock` / `getauxblock`:** the daemon ships the classic
> AuxPoW template RPCs (`src/rpc/auxpow_miner.cpp`), and the mining release's
> `INTEGRATION.md` describes that flow. On the current release, however,
> template creation through those RPCs fails inside the daemon
> (`CreateNewBlock: TestBlockValidity failed: no-auxpow` — the template is
> validity-tested before the miner has attached any proof). A fix is in
> progress separately. Until it lands, use the `submitblock` path in this
> guide. That path remains fully supported either way; nothing in this guide
> becomes obsolete when the template RPCs are fixed.

---

## 1. What you run

| Component | Notes |
|---|---|
| `syscoind` v5.0.5 | From the mining release (see `docs/rod-mining-release/SETUP.md` on the release branch for the build and `syscoin.conf`). Fully synced: `getblockchaininfo` shows `blocks == headers`. P2P port **8369**, RPC port **8370** (`src/kernel/chainparams.cpp:222`, `src/chainparamsbase.cpp:43`). Keep RPC on localhost or firewalled to your pool host only. |
| `sysgeth` (ships with the release) | Runs alongside `syscoind` on the same host and follows the chain. Your block producer needs it as the source of NEVM block templates (§4). Start it with `--syscoin --nevmpub tcp://127.0.0.1:9377` so it serves the template endpoint. |
| Your own Bitcoin Core | Your normal parent-work source: `getblocktemplate` for the parent coinbase and header, `submitblock` for any parent that also meets Bitcoin difficulty. Stock Core, no patches. |
| Your pool software | Builds one extra coinbase commitment (for SHIT) into your Bitcoin coinbase, builds the SHIT block when a parent header solves the SHIT target, and submits it. The reference pool in this repository is a complete working example. |

**Operational caution (from the release SETUP doc):** do not enable
`-zmqpubnevm` full NEVM state validation on the `syscoind` that serves your
pool — a pool node validates SHIT blocks from the P2P network in lockstep
without it, and that mode wedged a node at block 48,000 in October 2026.
`sysgeth` is needed as a *template source for your producer* (§4), not as a
validation dependency of the daemon.

---

## 2. Consensus constants (verify in source)

| Constant | Value | Source |
|---|---|---|
| AuxPoW chain ID | **16** decimal, **0x10** hex | `src/kernel/chainparams.cpp:172` (`nAuxpowChainId = 16`), declared `src/consensus/params.h:304`; also `src/primitives/pureheader.h:21` |
| Legacy chain ID (rejected as parent) | 4096 (0x1000) | `src/kernel/chainparams.cpp:173` (`nAuxpowOldChainId`) |
| AuxPoW mandatory from height | 1 | `src/kernel/chainparams.cpp:174` (`nAuxpowStartHeight`) |
| Merged-mining header bytes | `fa be 6d 6d` (`0xfa 0xbe 'm' 'm'`) | `src/auxpow.h:31` |
| SHIT block header version (current) | `0x20100180` | base `0x20000000` \| chain ID 16 << 16 \| auxpow flag `0x100` \| NEVM flag `0x80` |
| Block time target | 30 seconds | `src/kernel/chainparams.cpp` (`nPowTargetSpacing = 30`) |
| Subsidy reduction interval | 1,051,200 blocks | `src/kernel/chainparams.cpp:146` |
| Coinbase maturity | **100 blocks** | `src/consensus/consensus.h:20` |
| NEVM activation height | 30,316 (long active) | `src/kernel/chainparams.cpp:206` |
| NEVM chain ID (for `sysgeth`) | 20026 | `src/kernel/chainparams.cpp` (`nNEVMChainID`) |
| Nexus activation height (future, §8) | 2,010,345 | `src/kernel/chainparams.cpp:210` |

The SHIT block hash used everywhere below is the double-SHA256 of the 80-byte
SHIT block header, quoted in display byte order (the order `getblockhash`
returns) unless stated otherwise. Hashes inside serializations are in
internal (little-endian) byte order.

---

## 3. Build the SHIT block

For each new SHIT height `H = tip + 1`:

### 3.1 Coinbase transaction

- Transaction version 1, segwit serialization (marker `00`, flag `01`) —
  the coinbase always carries a witness (the commitment in §4).
- One input: null prevout (32 zero bytes, index `0xffffffff`), sequence
  `0xffffffff`.
- `scriptSig`: the BIP34 block height as a minimal `CScriptNum` push.
  Anything after the height push is free-form; both reference
  implementations append one `OP_0` byte. Total `scriptSig` ≤ 100 bytes.
- Outputs, in order:
  1. **Subsidy output** — the full block subsidy (§3.2) to **your pool's
     own SHIT address** (`shit1q…` bech32, HRP `shit`, P2WPKH:
     `00 14 <20-byte witness program>`). You may split the subsidy across
     your own outputs (fee splits etc.) as long as the total stays within
     the cap below.
  2. **Witness commitment output** — 0 value, built in §4.
- Witness for the single input: one 32-byte all-zero item.
- Locktime 0.
- The coinbase **txid** (used in the block's merkle root) is the
  double-SHA256 of the witness-*stripped* serialization, as usual.
- Cap: total coinbase output value must not exceed subsidy + fees of the
  transactions you include, or the block is rejected at connect time with
  `bad-cb-amount` (`src/validation.cpp:3178`). Coinbase-only blocks (the
  reference pool's mode) have no fees: pay exactly the subsidy.

### 3.2 Subsidy schedule (exact — overpaying by 1 sat invalidates the block)

Consensus: `GetBlockSubsidy`, `src/validation.cpp:1936`.

```
n = 19,807,185,401 sats                       # nMiningSubsidyBase
reductions = H // 1,051,200
if reductions >= 100: subsidy = 0
repeat reductions times: n -= n // 50         # 2% decay per interval
withheld = int(n * 0.1)                       # superblock part, not payable
subsidy = n - withheld
```

At the current schedule (reductions = 0) the payable subsidy is
**17,826,466,861 sats = 178.26466861 SHIT per block**. The withheld 10% is
never issued to anyone — it is not escrowed and cannot be claimed; the
payable figure above is the whole coinbase budget. A verbatim port of this
schedule ships in the reference pool (`shit_get_subsidy`) and was verified
sat-for-sat against real blocks.

### 3.3 Difficulty (`nBits`)

Your header's `nBits` must equal the consensus value *exactly* — the daemon
compares it against `GetNextWorkRequired` and rejects with `bad-diffbits`
(`src/validation.cpp:4829`). There is no "close enough".

Current regime (all heights above 30,950), `CalculatePerBlockWorkRequired`,
`src/pow.cpp:75`, constants `src/pow.cpp:44–68`:

1. Take the previous block's target (from its `nBits`).
2. `dt = prev.time − prevprev.time`, clamped to `[30/4, 30*4]` seconds
   (spacing is 30 s in the current era).
3. `u = dt − 30`. If `u > 0`, multiply the target by `(49/48)` `u` times
   (integer math, K = 48); if `u < 0`, multiply by `(48/49)` `|u|` times.
4. Floor (heights ≥ 48,460): the target may not exceed the target of bits
   `0x1d63ffff` (difficulty never drops below ≈ 0.01).
5. Cap: the target may not exceed the target of the genesis bits
   `0x1e0fffff`.

You need only the last two block headers (via `getblockhash` +
`getblockheader` RPC) to compute this. Working ports ship in the reference
pool (`ShitAuxChain._bits_for_height`) and reproduce the on-chain `nBits`
exactly. At the time of writing (October 2026) SHIT difficulty is below 0.1,
so any parent header that meets full Bitcoin difficulty meets the SHIT
target many orders of magnitude over — in practice, every Bitcoin block
your pool finds can carry a SHIT block.

### 3.4 Header

80 bytes, standard layout:

| Field | Value |
|---|---|
| version | `0x20100180` (little-endian int32) |
| prevhash | current SHIT tip hash, internal byte order |
| merkle root | over the block's txids (coinbase first), internal byte order |
| time | must be > median-time-past of the previous block (`time-too-old`) and ≤ now + 2 h (`time-too-new`) |
| bits | per §3.3 |
| nonce | arbitrary — the proof of work is the parent's, not this header's; production code writes 0 |

If you include mempool transactions (optional; the chain's own miner
does), fetch them from your `syscoind` (`getrawmempool` /
`getrawtransaction`), place them after the coinbase, add their fees to
your coinbase cap (§3.1), and build the witness commitment over **all**
transactions' wtxids (§4). The reference pool builds coinbase-only blocks;
both shapes are consensus-valid.

---

## 4. NEVM commitment (mandatory at every current height)

Since height 30,316 every SHIT block must carry a NEVM (EVM-side)
commitment, or it is rejected. This is the one part of SHIT block
production with no Bitcoin-side analogue; both reference implementations
implement exactly the steps below.

1. **Version bit.** The header version must be `0x20100180` (NEVM flag
   `0x80` set), or the block is rejected with `nevm-version-expected`
   (`src/validation.cpp:4854`).
2. **Fetch the NEVM template** from your `sysgeth`:
   - ZMQ REQ socket to `tcp://127.0.0.1:9377`.
   - Send multipart: `[b"nevmblock", b""]`.
   - The reply is 4 frames: `[b"nevmblock", b"", b"nevmblock", payload]`.
   - `payload` = 96-byte NEVM header ‖ CompactSize length ‖ RLP-encoded
     NEVM block. The 96-byte header is `block hash ‖ tx root ‖ receipt
     root` (3 × 32 bytes).
   - Reference implementations: `ShitAuxChain._nevm_template` /
     `get_template` in the published reference pool.
   - No reachable template ⇒ you cannot produce a valid SHIT block for
     that height. Skip the height; do not submit a block without it.
3. **Witness commitment output** (coinbase output 2):
   - Compute the witness merkle root over the block's wtxids: coinbase
     wtxid = 32 zero bytes; every other transaction's wtxid =
     double-SHA256 of its full (witness-including) serialization.
   - `commitment = sha256d(witness_root ‖ 32 zero bytes)` (the coinbase's
     witness nonce).
   - Output scriptPubKey:
     `6a 24 aa21a9ed ‖ commitment` (OP_RETURN, 36-byte push, BIP141
     header), immediately followed by a push of the 100-byte NEVM extra:
     `4c 64` (OP_PUSHDATA1, 100) ‖ `nevm` (4 ASCII bytes) ‖ the 96-byte
     NEVM header from step 2.
4. **Append the NEVM block data to the block.** After the transaction
   list, append: CompactSize length ‖ the RLP NEVM block bytes from
   step 2 (the block's `vchNEVMBlockData`, `src/primitives/block.h`). A
   NEVM-flagged block with empty data is rejected with
   `nevm-data-not-found` (`src/validation.cpp:4692`).

---

## 5. Parent (Bitcoin) side and AuxPoW assembly

### 5.1 Commit SHIT in your Bitcoin coinbase

Call `getblocktemplate` on your Bitcoin Core and build your parent
coinbase as usual — SHIT consensus never inspects your parent coinbase
*outputs*; pay yourself however your pool pays. The one addition is in the
parent coinbase's `scriptSig`: a merged-mining commitment with this exact
byte layout (`CAuxPow::check`, `src/auxpow.cpp:38–123`):

```
[anything]  fa be 6d 6d  <chain merkle root: 32 bytes>  <tree size: uint32 LE>  <nonce: uint32 LE>  [anything]
```

Rules enforced by consensus:

- The 4-byte header `fabe6d6d` must occur **exactly once** in the
  scriptSig and must sit **immediately before** the chain merkle root.
  (Rejections: "Missing auxpow header", "Multiple merged mining headers
  in coinbase", "Merged mining header is not just before chain merkle
  root", "Aux POW missing chain merkle root in parent coinbase" —
  `src/auxpow.cpp:78–103`.)
- The chain merkle root is written in **display byte order** (the reverse
  of the internal uint256 order).
- After the root come 4-byte little-endian **tree size** and 4-byte
  little-endian **nonce**; a truncated tail is rejected ("Aux POW missing
  chain merkle tree size and nonce in parent coinbase").

**Single aux chain (SHIT only)** — the common case:

- chain merkle root = the SHIT block hash (display order),
- tree size = 1, nonce = 0 (any value works),
- chain merkle branch = empty, chain index = 0.

**Several aux chains in one parent:** the leaves of the merged-mining
merkle tree are the aux block hashes; the root is the ordinary Bitcoin
merkle root over them, embedded in display order. The committed tree size
must equal `2^(length of your chain merkle branch)`
(`src/auxpow.cpp:110–114`), and SHIT's leaf index is not yours to choose —
it is fixed by the coinbase nonce and the chain ID
(`CAuxPow::getExpectedIndex`, `src/auxpow.cpp:126–144`):

```
rand = nonce
rand = rand * 1103515245 + 12345;  rand %= 2^h
rand += 16                        # SHIT chain ID
rand = rand * 1103515245 + 12345;  rand %= 2^h
index = rand                      # h = chain branch length
```

Set the nonce first, compute SHIT's slot from the formula, and place the
SHIT hash at that leaf. A wrong slot is rejected ("Aux POW wrong index").

### 5.2 Mine the parent header

Hash the Bitcoin parent header exactly as you do for Bitcoin. The SHIT
block is valid when the **parent header's double-SHA256 is ≤ the target
implied by the SHIT block's `nBits`** — that is the only proof-of-work
check (`src/validation.cpp:1915`, `CheckProofOfWork` on the parent hash
against the SHIT `nBits`). The parent does not need to meet Bitcoin's
difficulty for the SHIT block to count; if it also meets Bitcoin
difficulty, submit it to Bitcoin Core as usual and collect both rewards —
the two submissions are independent.

Parent-header constraint: SHIT applies a *negative* condition only — the
parent header must not itself claim SHIT's chain ID (16, or the legacy
4096) in its version (`src/auxpow.cpp:43–53`, strict chain ID is on).
A normal Bitcoin header, including BIP310 version-rolled headers, never
does.

### 5.3 Assemble the AuxPoW

When a parent header solves the SHIT target, serialize the `CAuxPow`
exactly in this order (`SERIALIZE_METHODS`, `src/auxpow.h`):

| # | Field | Format |
|---|---|---|
| 1 | parent coinbase tx | the full serialized parent coinbase transaction (witness included if it has one) |
| 2 | `hashBlock` | 32 bytes: the parent header's hash, internal byte order. (The daemon never verifies this field — `src/auxpow.h` — but production producers write the real parent hash; do the same.) |
| 3 | `vMerkleBranch` | CompactSize count + 32-byte hashes: the merkle branch proving the parent coinbase sits at index 0 of the parent block. Empty only when the parent block contains the coinbase alone. A bad branch is rejected ("Aux POW merkle root incorrect"). |
| 4 | `nIndex` | int32 LE — always 0 |
| 5 | `vChainMerkleBranch` | CompactSize count + 32-byte hashes: the branch from the SHIT block hash up to the chain merkle root committed in §5.1. Empty in the single-chain case. More than 30 entries is rejected ("Aux POW chain merkle branch too long"). |
| 6 | `nChainIndex` | int32 LE — SHIT's leaf index (0 single-chain; otherwise the §5.1 formula result) |
| 7 | parent header | the 80-byte Bitcoin parent header that met the target |

### 5.4 Assemble and submit the SHIT block

Full block serialization (`src/primitives/block.h`, and the layout both
production producers emit):

```
80-byte SHIT header ‖ AuxPoW (§5.3) ‖ CompactSize tx count ‖ transactions ‖ CompactSize NEVM data length ‖ NEVM RLP block (§4)
```

Submit the hex with `submitblock` to your `syscoind` RPC (port 8370).

**Verify acceptance — never assume it.** A `null`/empty `submitblock`
result means the daemon accepted the block *into processing*; count the
block as yours only when `getblockhash(H)` returns your block's hash.
Non-empty results are rejection reason strings (§7). If the tip moved
while you were building (your prevhash is no longer the tip), the block
is stale: throw it away and rebuild on the new tip — production code
aborts stale work rather than submitting it. If the reply is lost in
transit, check visibility before resubmitting; a resubmit of an accepted
block is a harmless duplicate.

---

## 6. Payout facts

- The SHIT coinbase pays **your own SHIT address** — the one in your
  coinbase output (§3.1). No registration, no intermediary, no pool-side
  accounting owed to anyone.
- Reward: **178.26466861 SHIT per block** at the current schedule
  position (exact schedule in §3.2; it decays 2% every 1,051,200 blocks
  and the payable figure already excludes the never-issued 10%).
- **Maturity: 100 confirmations** before a coinbase output can be spent
  (`src/consensus/consensus.h:20`). Budget your payout runs accordingly.
- Cadence: one SHIT block per qualifying parent solution. At current
  difficulty, any parent header that meets Bitcoin difficulty qualifies
  (§3.3), so SHIT accrues alongside your normal Bitcoin blocks for as
  long as you include the §5.1 commitment.

---

## 7. Failure modes — what the daemon rejects, and why

`submitblock` returns these reason strings verbatim (all
`src/validation.cpp` unless noted):

| Rejection | Cause |
|---|---|
| `no-auxpow` (:4826) | Block at height ≥ 1 submitted without an AuxPoW, or with the auxpow flag missing from the header version. Standalone-mined SHIT blocks are never accepted. |
| `bad-diffbits` (:4829) | Header `nBits` ≠ `GetNextWorkRequired` for that height (§3.3). Recompute from the last two headers. |
| `nevm-version-expected` (:4854) | Header version missing the NEVM flag (`0x20100180` required). |
| `nevm-data-not-found` (:4692) | NEVM-flagged block with empty NEVM block data (§4 step 4), or a commitment that doesn't match your `sysgeth`'s block for the height. |
| `bad-cb-amount` (:3178) | Coinbase pays more than subsidy + included fees (§3.1, §3.2). |
| `bad-cb-missing` | First transaction is not the coinbase. |
| `time-too-old` / `time-too-new` | Header time vs. median-time-past / +2 h bound. |

If the parent header's hash misses the SHIT target, the header check
fails and the daemon logs `AUX proof of work failed`
(`src/validation.cpp:1916`); the block is rejected as an invalid header.

AuxPoW structural failures surface in the daemon's log via
`CAuxPow::check` (`src/auxpow.cpp`) and the block is rejected as invalid:
"Aux POW merkle root incorrect" (coinbase branch doesn't reproduce the
parent header's merkle root), "Aux POW missing chain merkle root in
parent coinbase", "Missing auxpow header", "Multiple merged mining
headers in coinbase", "Merged mining header is not just before chain
merkle root", "Aux POW missing chain merkle tree size and nonce in
parent coinbase", "Aux POW merkle branch size does not match parent
coinbase", "Aux POW wrong index" (§5.1 slot formula), "Aux POW chain
merkle branch too long", "Aux POW coinbase has no inputs",
"Aux POW parent has our chain ID" (parent claims chain ID 16/4096),
and `AUX POW is not valid` from the header check that wraps them all.

---

## 8. Forward notice — height 2,010,345 (Nexus)

Not active today (`nNexusStartBlock = 2,010,345`,
`src/kernel/chainparams.cpp:210`), but integrators building for the long
term should know: from that height, `ContextualCheckBlockHeader`
(`src/validation.cpp:4884–4929`) additionally requires a tag inside an
unspendable (OP_RETURN) output of the **parent coinbase**: the 3-byte
header `sys` (`src/auxpow.h:32`) followed, later in the same output, by
the 36-byte chunk `reference hash (32 bytes, internal order) ‖ reference
height (uint32 LE)`, where the reference block is the ancestor at height
`(prevHeight − 5)` rounded down to a multiple of 10. Rejections then
include `missing-syscoin-tag`, `bad-auxpow-tag`,
`multiple-syscoin-tags`, `missing-auxpow-cb`, and `bad-auxpow-ref`.
Nothing in §§3–5 changes; only the parent coinbase gains one output.

---

## 9. Checklist

1. `syscoind` v5.0.5 synced (`blocks == headers`), RPC on 8370 reachable
   from your pool host only; `sysgeth` running with `--nevmpub` on 9377.
2. Per SHIT height: fetch NEVM template → build coinbase (subsidy §3.2 +
   commitment §4) → compute bits (§3.3) → assemble header (§3.4) → hash
   it.
3. Embed `fabe6d6d` + root + size + nonce in your Bitcoin coinbase
   (§5.1); place the SHIT hash at the formula's slot if you merge-mine
   several chains.
4. Mine the parent as usual. On a parent solution ≤ SHIT target:
   assemble the AuxPoW (§5.3) and the block (§5.4), `submitblock`, then
   confirm with `getblockhash`.
5. Pay yourself from the coinbase after 100 confirmations.

A complete, production-verified implementation of every step above is the
reference pool published with the mining release:
`docs/rod-mining-release/reference-pool/pool.py` (branch
`release/rod-mining-2026-10-14-clean`) — class `ShitAuxChain`
(template, coinbase, bits, submit/verify) and the subsidy/bits ports at
the top of that file.
