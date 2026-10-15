# The Oct 12 error — what it was, in one page

**The error you hit:**
```
CheckProofOfWork : block does not have our old chain ID (got 0, expected 4096, full nVersion 256)
```

**Short version:** the error is real — we reproduced it exactly — but it does not come
from ASIC version rolling, and it does not need a consensus change. It happens when pool
software builds the SHIT block header itself instead of getting merge-mining work from
the node. The fix is to use the node's standard interface (see `INTEGRATION.md`).

## Why we can say that with confidence

- The strict chain-ID check sits on the **SHIT (child) header**, which the node builds.
  The child must carry chain ID **16**. The value in your error — `full nVersion 256` —
  is the bare auxpow flag (`1<<8`) with **no chain ID, no BIP9 bits, and no NEVM bit**.
  A header the node built reads `0x20100180`, and rolling one could never produce 256.
  256 is the fingerprint of a header constructed by pool software from base version 0.
- The **Bitcoin parent** header — the one ASICs actually roll — has only a *negative*
  check: it must not claim to be a SHIT header itself. A plain Bitcoin header never
  does. We rolled **65,536** parent versions through the validation code: **zero
  rejected**, confirmed by a second harness and a red-team re-run.
- The same error reproduces with **no rolling at all** if the child header is built by
  hand — and rolling the child header rejects ~96.9% of the time (the rolls that
  "pass" are coincidences landing on a valid ID). Your pool hit the child-header path;
  that is the whole story.
- The arrangement you proposed as the fix (verify the ID via the merkle branch, like
  Dogecoin) is **already how SHIT works**: Dogecoin mainnet runs the identical strict
  setup (`fStrictChainId = true`, strict positive child check, negative-only parent
  check) and merges at scale — because its pools use this same node interface.
  (Litecoin Cash, the other example, is not an AuxPoW coin at all.) A "lenient" flip
  would make SHIT *less* like Dogecoin, and the naive version of it breaks a real
  safety property (one parent could commit several competing SHIT blocks), so we are
  not doing it — and you don't need it.

## About hosting SHIT as a standalone primary coin

That cannot work on this chain, and it isn't a configuration issue: since block 1,
SHIT consensus rejects any block without valid AuxPoW (`no-auxpow`). A standalone
pool's blocks would be rejected by every node on the network. Merge mining is the
only way in — fortunately it's also the free way: the same hashes you're already
computing for Bitcoin earn SHIT on top.

## What to change on your side

One integration point: replace "build a SHIT job" with `createauxblock` /
`submitauxblock` (§2 of `INTEGRATION.md`). Your ASICs, your Bitcoin job, and your
version rolling stay exactly as they are. Steps from your current stack:
`UPGRADE-FROM-OLD-SETUP.md`.
