# Running a Shitcoin Node: Operator Notes

Short, current notes for anyone running a Shitcoin (SHIT) node or mining
SHIT. If a number here disagrees with an older page, this page is the one
to trust.

## Current public release

- The current public release is **v5.0.7**, published on GitHub on
  2026-10-17: https://github.com/ShitcoinBTC/ShitcoinBTC/releases/tag/v5.0.7
- The `syscoind` file in that release has a SHA-256 hash beginning
  `df27b410`. Check your download against the `SHA256SUMS.txt` file in
  the same release before you run it.
- v5.0.8 and v5.0.9 exist only on internal nodes. They were never
  published. There is no public v5.0.8 or v5.0.9 release, so do not go
  looking for one, and do not run a copy that claims to be one.
  Operators run v5.0.7.

## The two chain IDs (do not mix them up)

Shitcoin has two different chain IDs, for two different jobs:

| ID | Value | What it is for |
|----|-------|----------------|
| AuxPoW chain ID | **29** | Merged mining. Stamped into parent Bitcoin coinbases. |
| NEVM chain ID | **20026** | The EVM layer. Used by Ethereum wallets and tooling (gas paid in SHIT). |

- The AuxPoW chain ID changed from 16 to 29 at **block 88,000**, on
  2026-10-18. Blocks below 88,000 use 16; blocks from 88,000 use 29.
- Syscoin still uses AuxPoW chain ID 16. Because the numbers no longer
  match, SHIT and SYS can now share one merged-mining tree under Bitcoin
  as the real parent chain. Before the change they collided and could not.
- 20026 is not a merged-mining number, and 29 is not an EVM number.
  Putting one where the other belongs will not work.

## sysgeth and the NEVM layer

The NEVM layer runs through a second program, sysgeth, next to `syscoind`.

- sysgeth must be started with:

  ```sh
  --nevmpub tcp://127.0.0.1:9377
  ```

- That flag has no default. Without it, the node cannot get NEVM block
  data from sysgeth, and NEVM blocks fail with `nevm-data-not-found`.
- If you see `nevm-data-not-found`, check the sysgeth start command
  first, before anything else.

## Merge-mining proxy

Pool operators merge-mine SHIT with the stratum proxy in
`contrib/pool-onboarding/shitcoin-stratum-proxy.py` in this repository.

- The proxy was updated on the morning of the block 88,000 fork so it
  stamps chain ID 16 below block 88,000 and chain ID 29 from block
  88,000 on, with no action needed at the gate.
- The current proxy file has a SHA-256 hash beginning `d5379a47`.
  Check yours:

  ```sh
  sha256sum shitcoin-stratum-proxy.py
  grep CHAINID_FORK_HEIGHT shitcoin-stratum-proxy.py
  ```

  The second command should print `CHAINID_FORK_HEIGHT = 88000`.
- A proxy older than that update stamps chain ID 16 at every height.
  Every block it makes at or after block 88,000 is rejected with
  `bad-chainid`. Replace it with the current file and restart it.
