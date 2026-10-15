# Handover summary — ShitcoinBTC merge-mining release (one page)

**For Rod — from Jodie.**

**What happened with your Oct 12 email:** it reached the team late (our miss, since
fixed), and instead of answering from opinion we put ten verification agents on it and
tested the whole mining path end to end against the live network before replying.

**The verdict on the chain-ID error:** the error is real, we reproduced it exactly —
but it isn't ASIC version rolling, and it doesn't need a consensus change. The strict
chain-ID check sits on the SHIT block header, which the **node** builds. Your
`nVersion 256` shows your pool software built that header itself. When the work comes
from the node instead (`createauxblock` — which hands you only a hash, no header),
ASICs roll freely on the Bitcoin parent: we tested **65,536 rolled parent versions,
zero rejected**. Dogecoin runs the identical strict setup and merge-mines at scale
the same way. The "lenient" change would actually make SHIT *less* like Dogecoin,
so we're not doing it — and you don't need it.

**Standalone hosting won't work** — SHIT has required merge mining since block 1;
blocks without AuxPoW are rejected by every node. The good news: merge mining costs
your ASICs nothing. Same hashes, SHIT on top.

**What we verified on our side before sending this (Oct 14–15):**
- Our pool's jobs come from a real Bitcoin Core (v31.1.0) `getblocktemplate` on
  mainnet — matched against the public chain at multiple heights, field for field.
  No synthetic template anywhere in the released setup; the old shortcuts in the
  repo are now guarded off (regtest-only) in the release commits.
- Share submission, duplicate handling, block submission, and payout accounting
  tested end to end; where a leg can only be proven with a real ASIC or a found
  block, the package says so instead of claiming it.
- One gap **we** found on **our** pool and are disclosing: our pool doesn't negotiate
  BIP310 version rolling for miners pointing ASICs at us yet. Doesn't affect your
  pool — your software controls your parent job — but you should hear it from us.

**In this package:** exact versions + commit hashes (`README.md`), the root-cause
note, the `createauxblock`/`submitauxblock` integration guide, node setup for both
daemons (including a read-only checker that proves a node is on real Bitcoin
mainnet), and upgrade steps from the October stack you already have running.

**The one change on your side:** stop building SHIT headers/jobs in the pool;
call the node's auxpow interface instead. Everything else — your ASICs, your
Bitcoin job, your rolling, your coinbase — stays exactly as it is.
