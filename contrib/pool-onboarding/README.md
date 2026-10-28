# Pool setup kit (installer, stratum proxy, yiimp adapter)

This directory is the complete pool operator kit. The same files are
served on the project site (https://shitcoinbtc.xyz/pool-setup.html and
https://shitcoinbtc.xyz/downloads/), and the stratum proxy and the
yiimp adapter bundle are also attached to the v5.0.9 GitHub release.

| File | What it is |
|---|---|
| `pool-setup.sh` | One-command pool node installer: node binaries (v5.0.9), chain snapshot, syscoind service, stratum proxy + service, acceptance checks. |
| `pool-setup.html` | Source of the pool setup guide served at https://shitcoinbtc.xyz/pool-setup.html. |
| `shitcoin-stratum-proxy.py` | The stratum proxy: merge-mines SHIT alongside Bitcoin through the local Shitcoin daemon, no merged-mining support needed in the pool software. |
| `shit-yiimp-adapter-bundle.zip` | Adapter bundle for yiimp-based pools that submit through their own software instead of the proxy. |
| `proxy.conf.example` | Example proxy configuration. |
| `shitcoin-stratum-proxy.service.example` | Example systemd unit for the proxy. |

## New pool: one command

```sh
curl -sSL https://shitcoinbtc.xyz/pool-setup.sh | sudo bash -s -- --payout shit1qYOURADDRESS
```

The installer verifies every download against pinned SHA256 checksums
and refuses to finish if any component it installed is not live: the
daemon must answer RPC, the pool wallet must load, and the proxy must
be active and listening before it prints DONE.

## Stratum proxy: required updates

**Chain ID fork (block 88,000, 2026-10-18).** The AuxPoW chain ID
changed from 16 to 29 at block 88,000. The current proxy is
height-gated: it stamps chain ID 16 for blocks below 88,000 and chain
ID 29 for block 88,000 and above, automatically. Copies older than the
fork update stamp 16 at every height and every block they produced at
or after block 88,000 was rejected with `bad-chainid`.

**Submission protection (current version).** A found block must never
be lost silently. The current proxy:

- spools every found block to disk (SPOOL_DIR) before submitting, and
  replays spooled blocks on startup and on an interval;
- retries a rejected submission with the identical payload, re-checking
  the tip between attempts, and stops once the tip has taken the
  block's height;
- refreshes its job on every tip change (built-in 5-second tip poll);
- raises a loud alarm (log, stderr, ALARM.txt in the spool dir) for
  any found block that cannot land.

The installer writes the submission-protection keys into proxy.conf
(SPOOL_DIR, SUBMIT_RETRIES, SUBMIT_BACKOFF_SECS, SPOOL_REPLAY_SECS,
ALARM_CMD) and creates the spool directory mode 0700 owned by the
service user.

## Updating an existing proxy install

```sh
curl -sSL https://raw.githubusercontent.com/ShitcoinBTC/ShitcoinBTC/master/contrib/pool-onboarding/shitcoin-stratum-proxy.py \
  -o /usr/local/bin/shitcoin-stratum-proxy.py
chmod +x /usr/local/bin/shitcoin-stratum-proxy.py
sudo systemctl restart shitcoin-stratum-proxy
```

(If your install runs the proxy from /opt/shitcoin-stratum-proxy/, as
the installer sets up, replace the file there instead.)

Configuration lives in /etc/shitcoin-stratum-proxy/proxy.conf. See the
pool setup guide at https://shitcoinbtc.xyz/pool-setup.html for full
pool setup instructions.

## Verifying you have the right version

```sh
sha256sum shitcoin-stratum-proxy.py
grep CHAINID_FORK_HEIGHT shitcoin-stratum-proxy.py
```

The current proxy has SHA256
`4ace8e29f66578cdf9ae6de2491f2eb564ae2d1efe02358bf651f2e10396c18d`
and the grep prints `CHAINID_FORK_HEIGHT = 88000`. If the checksum
differs, replace the file with the version from this directory, the
downloads page, or the v5.0.9 release before mining.
