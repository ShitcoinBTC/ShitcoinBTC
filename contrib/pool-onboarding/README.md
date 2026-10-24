# Shitcoin Stratum Proxy (merge mining)

`shitcoin-stratum-proxy.py` is the stratum proxy pool operators run to
merge-mine SHIT alongside Bitcoin. It builds AuxPoW block templates,
stamps them with the correct AuxPoW chain ID for the current height,
and submits finished work to the Shitcoin daemon.

## Required update: block 88,000 chain ID fork

**This version has been required for merge-mining SHIT since block
88,000.**

At block 88,000 the Shitcoin AuxPoW chain ID changed from 16 to 29.
This proxy is height-gated: it stamps chain ID 16 for blocks below
88,000 and chain ID 29 for block 88,000 and above, automatically, with
no operator action at the gate.

Copies of this proxy older than this version stamp the old chain ID
(16) at every height. Every block they have produced at or after block
88,000 was rejected by the network with `bad-chainid`. Block 88,000
passed on 2026-10-18, so if you are still running an older copy,
replace it now: your pool has not produced valid SHIT blocks since
the gate.

## Install

Install to the single canonical path:

```sh
curl -sSL https://raw.githubusercontent.com/ShitcoinBTC/ShitcoinBTC/master/contrib/pool-onboarding/shitcoin-stratum-proxy.py \
  -o /usr/local/bin/shitcoin-stratum-proxy.py
chmod +x /usr/local/bin/shitcoin-stratum-proxy.py
```

If you already run the proxy from `/usr/local/bin/shitcoin-stratum-proxy.py`,
just overwrite that file with this version and restart the service:

```sh
sudo systemctl restart shitcoin-stratum-proxy
```

Configuration lives in `/etc/shitcoin-stratum-proxy/proxy.conf`. See
the pool setup guide at https://shitcoinbtc.xyz/pool-setup.html for
full pool setup instructions.

## Verifying you have the right version

```sh
grep CHAINID_FORK_HEIGHT /usr/local/bin/shitcoin-stratum-proxy.py
```

The patched version prints `CHAINID_FORK_HEIGHT = 88000`. If the grep
finds nothing, you are running the old version and must update.
