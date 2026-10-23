# Running a Shitcoin Node: Operator Notes

Short, current notes for anyone running a Shitcoin (SHIT) node or mining
SHIT. If a number here disagrees with an older page, this page is the one
to trust.

## Current version

- The network runs **v5.0.9**. That is the current version, and it is
  the version operators should run.
- Get the v5.0.9 node files from the project downloads page:
  https://shitcoinbtc.xyz/downloads/ — the `syscoind` file there has a
  SHA-256 hash beginning `436dba8c`. Check your download against the
  `SHA256SUMS.txt` file on the same page before you run it.
- The current release on the GitHub releases page is v5.0.9:
  https://github.com/ShitcoinBTC/ShitcoinBTC/releases/tag/v5.0.9 —
  it publishes the same v5.0.9 files, with the same checksums, as the
  downloads page above. The older v5.0.7 release (2026-10-17) was
  built without ZMQ and cannot build merge mining templates; do not
  use it.

## Install and upgrade

First install:

1. Download `syscoind` and `syscoin-cli` from the downloads page
   (https://shitcoinbtc.xyz/downloads/) and verify both against
   `SHA256SUMS.txt` from the same page before you run anything.
2. Install both files to `/usr/local/bin`, mode 755.
3. Install `sysgeth` beside `syscoind` (to `/usr/local/bin`, mode 755).
   The v5.0.9 daemon manages `sysgeth` itself — see the sysgeth section
   below. Do not start `sysgeth` yourself and do not run it as a
   separate service. Note: the v5.0.9 daemon links `libzmq.so.5`, so
   the `libzmq5` package must be installed for it to start.
4. Start `syscoind` and let it sync. The pool setup guide on the website
   links a chain snapshot if you want the fast path.

Upgrading:

- Stop the node service, replace both binaries in `/usr/local/bin` with
  the current version, and start the service again.
- Never delete the datadir on upgrade.
- No resync or reindex is needed when upgrading across the block 88,000
  fork: a node on the current version follows the chain straight
  through it.

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

- With v5.0.9 the daemon manages sysgeth itself: at startup `syscoind`
  stops any running sysgeth, then launches the sysgeth it finds beside
  `syscoind` (`<bindir>/sysgeth` and sibling paths, for example
  `/usr/local/bin/sysgeth` next to `/usr/local/bin/syscoind`). If no
  sysgeth is found there, NEVM init stalls.
- Do not run sysgeth as a separate service with this build. If an older
  install left a standalone `sysgeth.service` behind, disable it:
  `systemctl disable --now sysgeth 2>/dev/null || true`.
The daemon passes `--syscoin` and `--nevmpub` itself, driven by
`syscoin.conf`. The production `syscoin.conf` recipe is:

```ini
zmqpubnevm=tcp://127.0.0.1:9377
gethcommandline=--syscoin.statebootstrap.disable
gethcommandline=--nodiscover
gethcommandline=--maxpeers=0
gethcommandline=--http
gethcommandline=--http.addr=127.0.0.1
gethcommandline=--http.port=8545
gethcommandline=--http.api=eth,net,web3
```

- `zmqpubnevm` is where the daemon takes the `--nevmpub` address from.
- `--syscoin.statebootstrap.disable` is required. Without it, the
  spawned sysgeth downloads the Syscoin mainnet state bootstrap
  (about 1 GB) and never becomes ready: `getblocktemplate` and
  `getauxblock` then fail with `nevm-data-not-found`.
- If you see `nevm-data-not-found`, check the `syscoin.conf` lines
  above and that the sysgeth binary sits beside `syscoind`, before
  anything else.

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
- Two ways to merge mine, and they pay differently: pool software that
  builds AuxPoW itself can use the daemon's native `getauxblock` path,
  anchored in a real Bitcoin block, and earn BTC + SHIT. The proxy
  builds a synthetic parent instead, so mining through it earns SHIT
  only. See `doc/stratum-proxy.md`.
