# Mining Shitcoin: real Bitcoin mainnet is the parent chain

Shitcoin (SHIT) is merge-mined with Bitcoin. That sentence only works if
"Bitcoin" means the real thing: **Bitcoin Core, on Bitcoin mainnet**.
This guide is the operator-facing setup for that. It replaces any older
habit of mining against a simulated, regtest, testnet, or signet parent
chain. Work from those networks cannot produce a valid mainnet Shitcoin
block, and standalone Shitcoin blocks are rejected by consensus
(`no-auxpow` in `src/validation.cpp`): after the AuxPoW start height,
every Shitcoin block must carry a valid AuxPoW proof anchored in a real
Bitcoin block.

## Two chains, never confuse them

| | Bitcoin (parent chain) | ShitcoinBTC (this repo) |
|---|---|---|
| Software | Bitcoin Core (`bitcoind`) | Shitcoin Core (`syscoind`) |
| Production network | Bitcoin **mainnet** (`chain=main`) | Shitcoin **mainnet** (default chain) |
| Coin | BTC | SHIT |
| Job in mining | Source of block templates and AuxPoW parent blocks | The chain being merge-mined |
| Merge-mining ID | n/a | AuxPoW chain ID **16** |
| Data directory | Bitcoin datadir (e.g. `~/.bitcoin`) | Shitcoin datadir (e.g. `~/.syscoin`) |

Rules that follow from the table:

- Bitcoin testnet, regtest, and signet are development networks. They are
  never a production parent chain. A parent block from any of them is
  worthless to Shitcoin mainnet.
- Shitcoin's own testnet / regtest / signet modes exist for developing
  Shitcoin itself (unit tests, functional tests). They do not change the
  rule above, and test SHIT is not mainnet SHIT.
- The NEVM (the EVM layer inside Shitcoin Core, chain ID 20026) is part
  of Shitcoin, not of Bitcoin. A NEVM feed is **not** a prerequisite for
  Bitcoin-only pool mining.
- A Bitcoin UTXO snapshot is not a Shitcoin snapshot. See "Path A" below:
  `utxo-935000.dat` loads into **Bitcoin Core only**. Shitcoin's own
  assumeutxo tooling (`contrib/devtools/utxo_snapshot.sh`,
  `dumptxoutset` / `loadtxoutset` on `syscoin-cli`) is a separate
  mechanism for the Shitcoin chainstate and must never be mixed with the
  Bitcoin snapshot.

## Who needs what

- **Miners with SHA-256 hardware:** you do not need your own Bitcoin
  node. Point your miner at a Shitcoin-aware pool's stratum endpoint.
  The pool runs the nodes below and does the merge-mining for you.
- **Pool operators and solo merge-miners:** you run both nodes — a real
  Bitcoin Core mainnet node (this guide, Path A or B) and `syscoind` on
  Shitcoin mainnet. The pool obtains work from Bitcoin Core, embeds the
  Shitcoin AuxPoW commitment (chain ID 16) in the Bitcoin coinbase, and
  submits finished work to both chains (`createauxblock` /
  `submitauxblock` on the Shitcoin side).

## Path A: new Bitcoin node operator (official Core + verified snapshot)

For a fresh box with no Bitcoin chain data yet. The snapshot skips most
of the initial block download; Bitcoin Core still verifies the snapshot
and syncs the rest of the way to the tip itself.

1. Install an official Bitcoin Core release (binaries or your distro
   package). Do not use a fork, a "simulated" node, or a lightweight
   stand-in: the pool needs real `getblocktemplate` work from a fully
   validating mainnet node.
2. Download the project's verified Bitcoin UTXO snapshot,
   `utxo-935000.dat` (the Bitcoin UTXO set at block height 935,000).
   - Verify it before use: `sha256sum utxo-935000.dat`
   - Expected SHA-256: **TODO-VERIFY** — the full hash is being confirmed
     against the file on the production node and will be published here
     and in the release notes. Do not trust a hash copied from chat,
     social media, or an unofficial mirror; if the value in this file
     still says TODO-VERIFY, get the hash from the project directly and
     compare it before loading.
3. Configure `bitcoin.conf` for mainnet (see "Bitcoin Core configuration"
   below). In particular: no `testnet=1`, no `regtest=1`, no `signet=1`.
4. Start `bitcoind`, then load the snapshot:
   `bitcoin-cli loadtxoutset /path/to/utxo-935000.dat`
5. Wait until the node reports it is out of initial block download and
   caught up: `initialblockdownload` is `false` and
   `verificationprogress` is at (or extremely close to) 1 in
   `bitcoin-cli getblockchaininfo`. Loading a snapshot is not the end of
   syncing — the node still has to reach the current tip.
6. Run the verification steps below before connecting a pool.

## Path B: existing Bitcoin Core operator

For an operator who already runs Bitcoin Core. You keep your existing
node, chain data, and datadir — there is nothing to re-download.

1. Confirm the existing node is the real thing and on the right network:
   `bitcoin-cli getblockchaininfo` must report `"chain": "main"`. If it
   reports `test`, `signet`, or `regtest`, that node cannot be a
   production parent — install a mainnet node separately (Path A) and
   leave the test node alone.
2. Confirm it is fully synced (`initialblockdownload: false`). A pruned
   node is fine for `getblocktemplate`; a wallet on the Bitcoin node is
   **not** required for pool template work.
3. Add the RPC settings in "Bitcoin Core configuration" below so the
   pool can reach the node. Reuse your existing synced chain — never
   delete or re-download it for this.
4. Run the verification steps below.

## Bitcoin Core configuration

Minimum `bitcoin.conf` for a node that serves a pool:

```ini
# Production parent chain: Bitcoin mainnet. Never testnet/regtest/signet.
chain=main
server=1
rpcport=8332
# Authenticate the pool's RPC access. Prefer rpcauth= (hashed) or a
# strong rpcpassword= generated locally. Never share it publicly.
rpcuser=<pool-rpc-user>
rpcpassword=<strong-random-password>
# Restrict RPC to the pool host only. Same box:
rpcallowip=127.0.0.1
# Separate pool box: replace with that host's IP, and nothing broader.
# Never expose Bitcoin RPC (port 8332) to the public internet.
```

Notes:

- If the pool runs on a different machine, set `rpcbind` / `rpcallowip`
  to that machine only, and firewall port 8332 to it. Unrestricted
  public RPC is a theft and abuse risk, not a convenience.
- The pool polls `getblockchaininfo` and
  `getblocktemplate {"rules":["segwit"]}`. If those calls fail or time
  out, miners get no work — a slow or still-syncing Bitcoin node is a
  pool outage, not a degraded mode. Give the node enough RAM/CPU and do
  not run it on a box that is already saturated.
- On the Shitcoin side, run `syscoind` on its mainnet default (no
  `-testnet`, `-regtest`, or `-signet` flags) with its own datadir,
  untouched by anything in this guide.

## Verification: prove it is real mainnet before mining

Run the bundled read-only check (it changes nothing):

```sh
contrib/mining/check-bitcoin-mainnet.sh bitcoin-cli
# Separate datadir: contrib/mining/check-bitcoin-mainnet.sh bitcoin-cli -datadir=/path/to/.bitcoin
```

It must print PASS for all of: `chain` is `main`, the node is out of
initial block download, `verificationprogress` shows it synced, and
`getblocktemplate` returns work (so the pool can obtain a job). Any
FAIL means do not mine against that node.

By hand, the same proof is:

1. `bitcoin-cli getblockchaininfo` → `"chain": "main"`,
   `"initialblockdownload": false`.
2. Compare `blocks` and `bestblockhash` with a public Bitcoin block
   explorer at the same moment. They should match the public mainnet
   tip (allowing a block or two of timing difference). A height in the
   millions on a private chain, or a hash no explorer knows, means you
   are not on real mainnet.
3. `bitcoin-cli getblocktemplate '{"rules":["segwit"]}'` returns a
   template whose `previousblockhash` is the same tip hash.

## Migrating from a synthetic setup to real Bitcoin

If an earlier setup mined against a regtest/testnet/simulated parent, or
used the old `src/mine.sh` `generatetoaddress` loop (regtest-only — it
cannot produce a valid mainnet block, and the script now refuses to run
outside regtest), migrate like this. The Shitcoin side survives intact.

**Back up first.** Copy (do not move) your current configuration before
changing anything:

```sh
cp ~/.bitcoin/bitcoin.conf ~/.bitcoin/bitcoin.conf.bak   # if present
cp ~/.syscoin/syscoin.conf ~/.syscoin/syscoin.conf.bak   # if present
cp <pool-config> <pool-config>.bak
```

**What changes:**

- The Bitcoin node: switch to a real Bitcoin Core **mainnet** node
  (Path A for a new install, Path B if you already run one). If your
  existing Bitcoin datadir belongs to testnet/regtest, use a **new,
  separate datadir** for mainnet — a test-network datadir cannot be
  converted into a mainnet one.
- The pool configuration: point its Bitcoin RPC host, port, and
  credentials at the mainnet node (default mainnet RPC port 8332).
- Remove/never carry over: `regtest=1`, `testnet=1`, `signet=1`, any
  simulated/fake parent endpoint, and any `generatetoaddress` mining
  loop.

**What stays identical:**

- Your Shitcoin datadir (`~/.syscoin`), wallet, and SHIT addresses.
  **The Shitcoin chain does not need to be re-downloaded** — switching
  the Bitcoin parent does not touch Shitcoin chain data.
- Your Shitcoin configuration, except any test-network flags you were
  using for the old setup (production runs the default main chain).
- Pool payout addresses and miner stratum credentials, unless you were
  using test-network addresses (test-network addresses must be replaced
  with mainnet ones — a testnet BTC address cannot receive mainnet BTC).

**Steps:**

1. Stop the old mining loop / pool.
2. Back up the configs (above).
3. Stand up the real mainnet Bitcoin node (Path A or B) and let it
   finish syncing.
4. Update the pool's Bitcoin RPC settings to the mainnet node.
5. Start `syscoind` (mainnet default) and the pool.
6. Run the verification section above. Do not declare the migration
   done until the check script passes and the template's
   `previousblockhash` matches the public mainnet tip.
7. Keep the `.bak` configs until the first real merge-mined block has
   been accepted by both chains; then archive them.

## Test setups still exist — for testing

Regtest, testnet, and signet (for both Bitcoin and Shitcoin) remain the
right tools for developing and testing code, and the test suites in
`src/test/` and `test/functional/` use them heavily. They are how the
software gets tested. They are never how production SHIT gets mined.
