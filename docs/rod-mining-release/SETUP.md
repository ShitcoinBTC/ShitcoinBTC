# Setup — nodes for merge mining SHIT

You need two daemons: a **SHIT node** (`syscoind`, for `createauxblock`/`submitauxblock`)
and a **Bitcoin node** (official Bitcoin Core, your source of parent work). They can run
on the same machine. Configuration examples below are checked against our production
configuration.

## 1. SHIT node

- **Build:** `syscoind` v5.0.5, sha256 `a60885f26b2f78954e4979738e9e4b06dc9658803f79fc80ce3bcbd57472c1c3`,
  from https://github.com/ShitcoinBTC/ShitcoinBTC master `984349cc32162ce655622966c84acab10b56d196`.
  (Release binaries, when used, come from the project's GitHub releases — the wallet/node
  release v1.0.1, published 2026-10-09.)
- **Config** (`~/.syscoin/syscoin.conf`) — the settings that matter for a pool node:
  ```ini
  server=1
  daemon=1
  # RPC credentials of your choosing; keep RPC on localhost or firewall it to
  # your pool host only — never expose daemon RPC to the open internet.
  ```
- **Do NOT enable `zmqpubnevm` on a pool node.** That was the October stall: a pool node
  doing full ZMQ/NEVM state validation wedged at block 48,000. Pool nodes validate SHIT
  blocks from the P2P network in lockstep and need no local sysgeth feed to issue
  `createauxblock` work. (If your wrapper script launches sysgeth, make sure it passes
  arguments through — the other October bug was a wrapper silently dropping `"$@"`.)
- **Sync check:**
  ```
  syscoin-cli getblockchaininfo    # blocks == headers, verificationprogress ≈ 1
  syscoin-cli createauxblock <your-shit-address>   # returns "chainid": 16, height = tip+1
  ```
  If `createauxblock` errors, the node is not ready — do not fall back to building
  blocks in pool software (that is exactly the Oct 12 failure mode).

## 2. Bitcoin node — Option A: new node with the fast snapshot

Full detail in `POOL-NODE-GUIDE.md`. Summary:
1. Install **official Bitcoin Core v31.1.0** from bitcoincore.org; verify `SHA256SUMS`
   and the GPG signature before running anything.
2. `bitcoin.conf` (from our reference node): `server=1`, `daemon=1`, `prune=1100`,
   `dbcache=2048` (use `450` on a small box), `maxmempool=100`, `maxconnections=100`,
   your own `rpcuser`/`rpcpassword` (or an `rpcauth=` line), RPC bound to localhost
   unless your pool host must reach it — then `rpcallowip` for that host **plus** a
   host firewall. No `testnet`/`regtest`/`chain=` line: absent means mainnet.
3. Snapshot: `utxo-935000.dat` (UTXO set at block 935,000, base hash
   `0000000000000000000147034958af1652b2b91bba607beacc5e72a56f0fb5ee` — check it on any
   explorer). **Stated plainly: we are not distributing this file yet, and its full
   sha256 does not currently exist on our side (only the prefix `e572ddbe…` survives;
   the original was deleted after loading).** Core itself refuses a snapshot that
   doesn't match its built-in snapshot data, so a bad file fails safe. Until
   distribution is sorted, either sync from scratch or use a snapshot only from a
   source you independently trust. Load with: `bitcoin-cli loadtxoutset /path/to/utxo-935000.dat`
   (note: on Core v31 the command is `loadtxoutset` — older write-ups saying
   `loadutxosnapshot` are wrong).
4. Verify with the checker in this package: `python3 check-node.py --datadir ~/.bitcoin`
   — you want `chain is mainnet`, the **Bitcoin chain identity check PASS** (it asserts
   block 0 is the real Bitcoin genesis block; a SHIT daemon also reports "main", so the
   name alone proves nothing), `blocks == headers`, and the getblocktemplate check PASS.

## 3. Bitcoin node — Option B: you already run Bitcoin Core

Don't re-download anything. Back up `bitcoin.conf`, then: ensure `server=1` + RPC auth
+ `rpcport=8332`; if the pool runs on another host, add `rpcallowip=<pool-host>` (keep
`127.0.0.1`) and a matching firewall rule; restart Core (config applies at startup; no
sync progress is lost); run `check-node.py` — it must pass the genesis identity check
and report `chain='main'`. If it reports test/regtest/signet or fails identity, you are
pointed at the wrong chain and the pool cannot use that node.

## 4. Why the template source matters (the synthetic-template question)

The setup above has exactly one source of Bitcoin work: `getblocktemplate` from a real
Bitcoin Core on mainnet. That is deliberate and verifiable: the pool's jobs build on
the real tip (prevhash = the current block hash, checkable on any explorer), and a
found block is submitted back to that same Core with `submitblock`, which rejects
anything that doesn't extend the real chain. There is no mode in this setup that
fabricates a parent: a made-up header (zero prevhash, zero bits — the old synthetic
style) cannot be produced by it and would be rejected by Core on submission even if
one were hand-built. Old shortcuts are explicitly closed off in the release commits:
`src/mine.sh` now refuses to run outside regtest, and the auxpow getwork wrapper is
marked test-only/invalid on mainnet.
