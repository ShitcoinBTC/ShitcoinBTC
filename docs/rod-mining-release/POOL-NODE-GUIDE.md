# Mine Bitcoin on the ShitcoinBTC Pool — Node Setup Guide

**You mine real Bitcoin, on real Bitcoin mainnet.** This pool runs against a real
Bitcoin Core node synced to the real chain (tip ≈ block 971,449 on 2026-10-14).
There is no test chain, no simulated chain, and nothing to install from us to
start mining — a node is only needed if you want to run/support one (Option A/B).
To just mine, skip to **Connect your miner**.

- **Pool (stratum):** `stratum+tcp://170.101.111.196:3333`
- **Dashboard:** http://170.101.111.196:8080/ — your stats: http://170.101.111.196:8080/miner/YOUR-BTC-ADDRESS

---

## Option A — New node operator: Bitcoin Core + fast snapshot

You get a working mainnet node in about an hour of loading instead of days of syncing.

**1. Install official Bitcoin Core.** Download from https://bitcoincore.org
(our reference node runs **v31.1.0**). Verify the download before running it:
check the release `SHA256SUMS` file against your download and verify its GPG
signature with the Bitcoin Core release keys. Never run a `bitcoind` binary
someone sent you in a chat or a forum post.

**2. Write `bitcoin.conf`** (in your datadir, e.g. `~/.bitcoin/bitcoin.conf`).
These are the settings our pool expects, taken from our own reference node:

```ini
server=1
daemon=1
prune=1100
dbcache=2048
maxmempool=100
maxconnections=100
rpcuser=pick-a-username
rpcpassword=pick-a-long-random-password
rpcport=8332
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
```

Notes: no `testnet`, `regtest`, `signet`, or `chain=` line — absent means
**mainnet**, which is what you want. `prune=1100` keeps disk use small.
`dbcache=2048` suits a machine with ~8 GB RAM; use `450` on a smaller box.
Prefer hashed auth? Generate an `rpcauth=` line with Core's bundled
`share/rpcauth/rpcauth.py` instead of a plaintext password — either works.
If the pool server must reach your node from another machine, add
`rpcbind=0.0.0.0` **and** `rpcallowip=170.101.111.196` (the pool host) **and a
host firewall** that only lets that host reach port 8332 — see the warning in
Option B. Otherwise leave RPC on localhost only.

**3. Get the snapshot.** `utxo-935000.dat` is a Bitcoin Core *assumeutxo*
snapshot: the full UTXO set (every spendable coin) as of **block 935,000**,
so your node starts at block 935,000 and only syncs the rest.

> ⚠️ **Plainly: we are not distributing this snapshot file yet.** There is
> currently no download for it from us, and this step cannot be completed
> from our copy today. Once the distribution decision lands, this section
> will state exactly where an operator obtains the file (and its full
> verified sha256 — see below). Until then, either sync from scratch
> (skip to step 4 without loading a snapshot) or use a snapshot only from
> a source you independently trust, verified as described below.

- Snapshot base block: height **935,000**, hash
  `0000000000000000000147034958af1652b2b91bba607beacc5e72a56f0fb5ee`
  (this is a public mainnet block hash — check it on any block explorer).
- **Verify the file before you trust it:** run `sha256sum utxo-935000.dat`.
  The hash of the snapshot our node loaded begins **`e572ddbe`**.
  ⚠️ Honest status (2026-10-14): the original file was deleted from our node
  after it loaded, and only that recorded prefix survives — **we cannot publish
  a full 64-character hash until we re-derive one from a trusted copy, so do
  not trust any copy whose distributor cannot give you a full hash starting
  with `e572ddbe`.** You get a second, stronger check for free: Bitcoin Core
  itself refuses any snapshot that does not match the snapshot data built into
  the official release. If `loadtxoutset` rejects the file, delete it —
  the check worked. Sync from scratch instead.

**4. Start the node and load the snapshot:**

```bash
bitcoind -daemon
bitcoin-cli loadtxoutset /path/to/utxo-935000.dat
```

Our node's log shows this loading 164,241,311 coins and activating at block
935,000. Your node is **usable as soon as the snapshot activates**: it syncs
the remaining blocks to the tip normally. Behind the scenes it re-validates
all history up to block 935,000 in the background — that takes **hours to
days**, and RPC answers can be slow until it finishes. That is normal.

**5. Check it:** run the checker that ships with this guide (read-only):

```bash
python3 check-node.py --datadir ~/.bitcoin
```

You want `chain is mainnet`, `bitcoin chain identity` PASS (the checker
verifies the node's block 0 is the real Bitcoin genesis block — a
ShitcoinBTC daemon also calls its own chain "main", so the chain name
alone proves nothing), `blocks == headers`, and
`getblocktemplate (pool can obtain work) PASS`.

---

## Option B — You already run Bitcoin Core: keep your node, keep your chain

Do **not** re-download anything. Back up your `bitcoin.conf` first, then make
only these changes:

1. Make sure you have server + authenticated RPC:
   `server=1`, plus `rpcuser`/`rpcpassword` (or an existing `rpcauth=` line),
   `rpcport=8332` (the mainnet default).
2. Let the pool host reach it — add exactly:
   `rpcallowip=170.101.111.196` (keep your existing `rpcallowip=127.0.0.1`).
   If RPC currently binds only to localhost, the pool cannot connect from
   another machine; binding wider (`rpcbind=0.0.0.0`) is only safe together
   with the restricted allowlist and a host firewall.

> ⚠️ **Never expose unrestricted Bitcoin RPC to the internet.** Anyone who can
> reach an unrestricted node RPC can drain its wallet and control the node.
> Restricted looks like this: RPC answers **only** the IPs in `rpcallowip`
> (localhost + the one pool host above), behind a firewall, with a strong
> password. If you are not sure yours is restricted, keep RPC on localhost.

3. Restart Bitcoin Core (config changes only take effect at startup; a restart
   loses no sync progress), then run `python3 check-node.py --datadir <your datadir>`.
   It must say `chain='main'` **and** pass the `bitcoin chain identity` check
   (block 0 must be the real Bitcoin genesis block — the ShitcoinBTC daemon
   also reports its own chain as `main`, so the name alone is not proof).
   If it says test/regtest/signet, or the identity check fails, you are on
   the wrong chain and the pool cannot use the node.

---

## Connect your miner (both options — a node is NOT required to mine)

Point any Bitcoin ASIC / stratum miner at:

| Setting | Value |
|---|---|
| Pool URL | `stratum+tcp://170.101.111.196:3333` |
| Username | **Your BTC address** — this is your account and your payout address. Optional worker name: `YOUR-BTC-ADDRESS.worker1` |
| Password | Anything (e.g. `x`). To also register a ShitcoinBTC payout address for merge mining, use `shit=shit1q...your-shit-address...` |

**Confirm it is working:** your miner should show accepted shares, and your
address page — http://170.101.111.196:8080/miner/YOUR-BTC-ADDRESS — shows your
workers and shares. New miners start at difficulty 512.

**What the pool pays (BTC):** **PPLNS** (you are paid when the pool finds a
block, proportional to your recent shares). A **0.002% pool tax** is taken from
every block's coinbase — that is the whole BTC fee. Payout runs are **hourly**
in the pool software for balances of **0.0005 BTC** or more.
⚠️ **Current status, stated plainly:** payouts are **not switched on yet**
(`PAYOUTS_ENABLED=false` on the live pool). Balances accrue in the pool ledger,
but nothing is paid out until the operator arms payouts. Check the dashboard
before you rely on a payout date.

## Separate and optional: SHIT merge mining (NOT required for BTC mining)

ShitcoinBTC (SHIT) is merge-mined with Bitcoin — in principle the same hashes
can earn SHIT on top of BTC at no extra cost. **It is not live on this pool
yet:** the pool's jobs currently carry no SHIT aux block (the NEVM feed the
pool needs is not connected to the pool host; the pool status shows SHIT aux
"waiting"). Registering a `shit=` address in your password today is harmless
but earns no SHIT until that feed is connected. When it is live: SHIT fee is
1%, payout threshold is 100 SHIT. BTC mining is completely unaffected either way.
