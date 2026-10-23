# Stratum Proxy

## What the proxy is for

ShitcoinBtc is merge mined. Every block must carry a valid AuxPoW anchored
in a Bitcoin parent block, and standalone block templates are rejected by
consensus. Pool software that cannot build AuxPoW itself cannot mine
ShitcoinBtc on its own.

The stratum proxy removes that work. It is a single Python file,
`shitcoin-stratum-proxy.py`, that runs next to your node:

- Point stock stratum miners (cpuminer, cgminer, and similar) at the proxy
  as if it were a normal coin. No merged mining support is needed in the
  miner or in your pool software.
- The proxy builds the aux block itself: the coinbase pays your configured
  payout address, mempool transactions are included, and the NEVM
  commitment is embedded.
- It serves standard stratum v1 jobs, validates shares, and tracks them
  per worker with simple vardiff.
- When a share meets the aux network target, the proxy assembles the full
  block (aux header, AuxPoW, transactions, and NEVM data) and submits it
  to your node.

The proxy never touches your wallet keys. It only needs your node's RPC on
localhost and your payout address.

## What changed at block 88,000

A hard fork went live on 2026-10-18 at block 88,000. The fork changed
the AuxPoW chain ID from 16 to 29.

The proxy was updated the morning of the fork so merge mining keeps
working with real parent coins under the new chain ID. The change is
height gated inside the proxy:

- Blocks below 88,000 are stamped with AuxPoW chain ID 16.
- Blocks from 88,000 on are stamped with AuxPoW chain ID 29
  (block version `0x201D0180`).

Stamping the wrong era's ID makes the daemon reject the block
(`bad-chainid`), so a proxy copy from before the fork cannot mine current
blocks.

Do not confuse the three chain numbers:

- AuxPoW chain ID: **29** (16 before block 88,000).
- NEVM chain ID: **20026**. This is a different number, used on the NEVM
  side only.
- Current node version: **v5.0.9** — the version the network runs.

The proxy fetches NEVM templates from `sysgeth`, so `sysgeth` must be
running with its NEVM publisher on:

```
--nevmpub tcp://127.0.0.1:9377
```

## Get the current file

Download the current proxy file from the downloads folder on the site:

```
https://shitcoinbtc.xyz/downloads/shitcoin-stratum-proxy.py
```

For example:

```
wget https://shitcoinbtc.xyz/downloads/shitcoin-stratum-proxy.py
```

The same folder holds the config template `proxy.conf.example` and the
service template `shitcoin-stratum-proxy.service.example`. The pool setup
script (`pool-setup.sh`) and the pool operator guide on the site install
this same file.

Current file, verified on the server on 2026-10-23:

- File: `shitcoin-stratum-proxy.py`
- Size: 34402 bytes
- Last changed on the server: 2026-10-18 05:31 UTC
- SHA256:

```
d5379a475e4b6c544fa55b97866388fda47b064687edfb7a07c8ca733c3416a3
```

## Verify your copy

Check your copy before you run it:

```
sha256sum shitcoin-stratum-proxy.py
```

The output must match the SHA256 above exactly, character for character.
If it does not match, do not run the file. Download it again from the
link above and check it again.

## Revision note

A proxy fix was first published with a SHA256 beginning `01b347d5`.
The file served now is a later revision, with a SHA256 beginning
`d5379a47`. It had at least one further revision after the published fix.

Operators must run the current file. If your copy hashes to the older
`01b347d5` value, or to anything else, replace it with the current file
from the download link above and verify the hash again.
