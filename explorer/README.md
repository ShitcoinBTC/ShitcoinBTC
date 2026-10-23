# ShitcoinBtc Block Explorer (UTXO chain)

Self-hosted explorer for the ShitcoinBtc UTXO chain, designed to run on the
launch VPS next to `syscoind`. NEVM-side exploration (Blockscout) is
**phase 2** and is not included here — see "Phase 2" below.

## Stack decision

**Chosen: [BTC RPC Explorer](https://github.com/janoside/btc-rpc-explorer)**
(Node.js app, talks directly to the daemon over JSON-RPC.)

Why not the default ElectRS + Esplora pairing:

- ElectRS hardcodes Bitcoin's genesis block hash per network. ShitcoinBtc has
  its own genesis, so ElectRS would require maintaining a patched fork
  (genesis constant), plus a Rust toolchain build on the VPS, plus the
  Esplora frontend build — three services to operate instead of one, with
  an ongoing fork-maintenance burden on every upstream ElectRS release.
- BTC RPC Explorer needs only a Bitcoin Core-compatible RPC plus
  `-txindex=1`. No chain-specific patching, no extra index database, no
  Rust. Blocks, transactions, mempool, and address balances all work out
  of the box against `syscoind`.
- Verified against this tree: `-txindex` is supported (`src/index/txindex.cpp`,
  wired in `src/init.cpp`); mainnet RPC defaults to port **8370**
  (`src/chainparamsbase.cpp`, P2P is **8369**); auth is standard Core
  cookie or `rpcuser`/`rpcpassword`.

Trade-off, stated honestly: full per-address *transaction history* in
BTC RPC Explorer is an optional feature that needs an Electrum-protocol
server (`BTCEXP_ADDRESS_API`). Without one, address pages are limited
(balance/overview only). Running an Electrum server means the ElectRS-fork
route described above — documented as a phase-2 upgrade, not needed for
launch.

Why a plain install script instead of docker-compose:

- The launch box already runs `syscoind` and `geth` as native systemd
  services. Running the explorer as a native systemd service keeps one
  service manager, one log story (`journalctl`), and no docker-networking
  subtleties for localhost RPC access.
- Easier to audit and restart from a phone than debugging containers.
- Docker remains a fine later improvement; the upstream repo ships its own
  Dockerfile if we ever want it.

## Prerequisites

- Ubuntu 22.04/24.04 LTS VPS (script assumes `apt`; **TO-VERIFY** distro at
  launch time).
- `syscoind` built, configured, and **fully synced**, started with:
  - `server=1`
  - `txindex=1` (required for full transaction lookup; rebuilding the
    txindex on an already-synced node takes a while — set it *before*
    first sync, see `syscoind.conf.snippet`)
  - pruning **disabled** (default)
- The OS user that runs `syscoind` (default in these files: `shitcoin`).
  The explorer authenticates with the daemon's cookie file
  (`~/.syscoin/.cookie`), so it must run as the same user — or switch to
  `rpcuser`/`rpcpassword` (see env file comments).
- Outbound HTTPS (NodeSource, npm, GitHub) during install.

> Naming quirk: the daemon binaries in this tree are still called
> `syscoind` / `syscoin-cli` and the datadir is `~/.syscoin` (inherited
> from Syscoin). This runbook uses the real names so the commands work.
> Renaming binaries is a separate job.

## How the explorer talks to the node

```
browser -> [nginx :443, optional] -> btc-rpc-explorer 127.0.0.1:3002
                                        |
                                        v (HTTP JSON-RPC, cookie auth)
                                   syscoind 127.0.0.1:8370
```

- The app binds to `127.0.0.1:3002` only. Nothing about the explorer is
  exposed publicly by default.
- RPC traffic never leaves the box. `rpcbind`/`rpcallowip` are locked to
  localhost in `syscoind.conf.snippet`.
- `BTCEXP_NO_RATES=true` and `BTCEXP_PRIVACY_MODE=true`: no exchange-rate
  or IP-geolocation lookups (SHIT has no price feeds; fewer external calls).

## Install

On the VPS, from this repo:

```bash
cd ~/ShitcoinBtc/explorer   # wherever the repo was cloned on the VPS
sudo NODE_USER=shitcoin ./install-explorer.sh
```

What the script does:

1. Installs Node.js 22 LTS via NodeSource (18+ required, 22+ recommended
   upstream).
2. Verifies the pinned `btc-rpc-explorer` tag exists upstream (fails loudly
   if not — no silent fallback to another version).
3. Clones to `/opt/shitcoin-explorer`, checks out the pinned tag,
   `npm install --omit=dev`.
4. Writes `/opt/shitcoin-explorer/.env` from `shitcoin-explorer.env`,
   pointing the RPC cookie at the node user's datadir, `chmod 600`.
5. Installs and starts the `btc-rpc-explorer.service` systemd unit.

Then open `http://127.0.0.1:3002/` on the box (e.g. via SSH tunnel) and
confirm the block height matches `syscoin-cli getblockcount`.

## Ports & firewall

| Port      | Service              | Bind       | Exposure            |
|-----------|----------------------|------------|---------------------|
| 8369/tcp  | `syscoind` P2P       | 0.0.0.0    | Public (peers need it) |
| 8370/tcp  | `syscoind` RPC       | 127.0.0.1  | **Localhost only — firewall it** |
| 3002/tcp  | explorer app         | 127.0.0.1  | Localhost only; public via reverse proxy |
| 8545/tcp  | geth (NEVM/Shitbridge) | 127.0.0.1 | **Localhost only — firewall it** |
| 1111/tcp  | ZMQ (bridge burn feed) | 127.0.0.1 | **Localhost only — firewall it** |

For a public explorer URL: put nginx in front with TLS (certbot), proxying
to `127.0.0.1:3002`. Upstream documents this setup in their README
("Reverse proxy with HTTPS"). Optionally set `BTCEXP_BASIC_AUTH_PASSWORD`
in the `.env` to gate the site, or `BTCEXP_SECURE_SITE=true` when serving
behind the HTTPS proxy.

## Operations

```bash
sudo systemctl status btc-rpc-explorer
sudo journalctl -u btc-rpc-explorer -f        # logs
sudo systemctl restart btc-rpc-explorer
# after editing /opt/shitcoin-explorer/.env:
sudo systemctl restart btc-rpc-explorer
```

Updating to a newer upstream release: edit `EXPLORER_VERSION` in
`install-explorer.sh`, re-run it (it re-checkouts and re-installs).

## Known limitations (honest)

- The UI brands itself "BTC RPC Explorer" / "Bitcoin" in places
  (`BTCEXP_COIN` officially supports only `BTC`). Cosmetic only; a full
  "Shitcoin" rebrand means forking the view templates — optional follow-up.
- Address pages show balance/overview; full address *history* needs an
  Electrum-protocol server (phase 2, see below).
- Homepage widgets like "next halving" assume Bitcoin's schedule and will
  look wrong — harmless, can be hidden later.
- Versions pinned here were verified 2026-09-24; re-check before running
  on the VPS (the installer fails loudly on a missing tag).

## Phase 2 (not included)

- **NEVM explorer (Blockscout):** point a Blockscout instance at the local
  geth RPC (`127.0.0.1:8545`, chain ID 20026) for SHIT-20 token / contract
  visibility. Separate service, separate guide.
- **Electrum-protocol server:** an ElectRS fork patched with ShitcoinBtc's
  genesis hash would give BTC RPC Explorer full address history
  (`BTCEXP_ADDRESS_API=electrum`) and serve Electrum wallets.

## TO-VERIFY on the VPS (launch checklist)

- [ ] Ubuntu version; NodeSource `setup_22.x` URL still current
- [ ] Pinned tag `v3.5.0` still the right choice (script verifies it exists)
- [ ] `syscoin-cli getindexinfo` shows `txindex` synced before opening the explorer publicly
- [ ] Cookie file readable by the service user (`/home/shitcoin/.syscoin/.cookie`)
- [ ] `npm install` completes (notably the `zeromq` native dependency)
- [ ] Explorer block height matches `syscoin-cli getblockcount`
- [ ] Firewall: 8370/8545/1111 not reachable externally; 3002 only via proxy
- [ ] Public URL + TLS via nginx/certbot (no public URL is invented here)
