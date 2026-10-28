#!/usr/bin/env bash
# pool-setup.sh -- one-command ShitcoinBtc pool node + merged-mining stratum proxy.
#
# Usage:
#   curl -sSL https://shitcoinbtc.xyz/pool-setup.sh | sudo bash -s -- --payout shit1qYOURADDRESS
#
# What it does (mirrors the pool setup guide, plus the stratum proxy):
#   1. Creates the `shitcoin` user.
#   2. Installs syscoind + syscoin-cli.
#   3. Installs sysgeth (NEVM helper) beside syscoind in /usr/local/bin.
#      The v5.0.9 daemon manages sysgeth itself: at startup it stops any
#      running sysgeth, then launches the sysgeth it finds beside
#      syscoind. There is no separate sysgeth service with this build.
#   4. Restores the fast-sync chain snapshot (skips the slow initial sync).
#   5. Writes syscoin.conf, installs the node as a service.
#   6. Installs shitcoin-stratum-proxy.py + config + service.
#   7. Starts the services, creates a dedicated `pool` wallet on the
#      node, and prints the acceptance test that uses it.
#      The proxy builds a synthetic parent, so mining through it earns
#      SHIT only. The native getauxblock path, anchored in a real
#      Bitcoin parent, earns BTC + SHIT — see the operator guide.
#
# After it finishes: point your stock stratum miners at <this-box>:3333.
# REQUIRED (2026-10-26, guide section 8A): this script also creates the
# found-block spool directory (mode 0700, owned by the node user) and
# writes the submission-protection keys into proxy.conf, using exactly
# the key names the shipped proxy reads: SPOOL_DIR, SUBMIT_RETRIES,
# SUBMIT_BACKOFF_SECS, SPOOL_REPLAY_SECS, ALARM_CMD. A found block must
# never be lost silently on a stale template (-8), a restart, or a
# rejection.
# No merged-mining support needed in your pool software -- the proxy does it.
#
# Installer revision: 2026-10-27 fix loop v3 (defects D1-D6, notes N1/N2,
# and red-team RT-3 fixed; see
# CHANGELOG in the fixed kit / the published release notes). Node
# binaries remain v5.0.9 -- the version the network runs.
# AuxPoW chain ID is 29 since block 88,000 (changed from 16 on 2026-10-18).
# Pre-fork node builds reject blocks from 88,000 up -- do not run them.
# The node binaries below come from the project downloads page and are
# pinned to the exact files running on the network's own nodes.
set -euo pipefail

PAYOUT=""
DATADIR_USER="shitcoin"
STRATUM_PORT="3333"

while [ $# -gt 0 ]; do
  case "$1" in
    --payout) PAYOUT="$2"; shift 2;;
    --user) DATADIR_USER="$2"; shift 2;;
    --stratum-port) STRATUM_PORT="$2"; shift 2;;
    *) echo "unknown arg: $1 (want --payout shit1...)"; exit 1;;
  esac
done

if [ -z "$PAYOUT" ]; then
  echo "ERROR: --payout shit1... is required (your block-reward address)."
  exit 1
fi
case "$PAYOUT" in shit1*) ;; *) echo "ERROR: payout must be a shit1... address"; exit 1;; esac
# RT-3 FIX (2026-10-27): the prefix check above is NOT validation.  A
# one-character typo in a shit1 address (valid charset, bad bech32
# checksum) passed it, the installer's own proxy then refused the
# address at startup ("bad bech32 checksum", before ever listening),
# crash-looped under systemd forever, and step 7 never noticed.
# Validate the FULL bech32 checksum here, failing fast -- the same
# rules the proxy enforces (HRP "shit", witness v0, 20-byte program).
# This early check uses python3 when it is already present; the
# MANDATORY re-check against the downloaded proxy's own parser runs
# in step 6 (after packages are installed), so a box without python3
# yet cannot slip a bad address through either.
if command -v python3 >/dev/null 2>&1; then
  python3 - "$PAYOUT" <<'PYEOF' || { echo "ERROR: --payout '$PAYOUT' is not a valid shit1... address (bech32 checksum / format check failed). Copy the address from your wallet again -- a single wrong character makes it invalid."; exit 1; }
import sys
addr = sys.argv[1].lower()
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
pos = addr.rfind("1")
assert pos > 0 and addr[:pos] == "shit", "bad HRP"
vals = [CHARSET.index(c) for c in addr[pos + 1:]]
def polymod(v):
    chk = 1
    GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    for x in v:
        top = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ x
        for i in range(5):
            if (top >> i) & 1:
                chk ^= GEN[i]
    return chk
chk = [ord(c) >> 5 for c in "shit"] + [0] + [ord(c) & 31 for c in "shit"]
assert polymod(chk + vals) == 1, "bad bech32 checksum"
def convertbits(data, frombits, tobits, pad=True):
    acc = 0; bits = 0; ret = []; maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value; bits += frombits
        while bits >= tobits:
            bits -= tobits; ret.append((acc >> bits) & maxv)
    assert not (pad and bits >= frombits) and not ((acc << (tobits - bits)) & maxv), "bad padding"
    return ret
prog = bytes(convertbits(vals[1:-6], 5, 8, False))
assert vals[0] == 0 and len(prog) == 20, "only bech32 P2WPKH (v0, 20-byte program) is supported"
PYEOF
else
  echo "    NOTE: python3 not present yet -- the mandatory payout-address check runs in step 6 against the proxy's own parser, before any service starts."
fi
if [ "$(id -u)" != "0" ]; then echo "ERROR: run as root (sudo)"; exit 1; fi

# Node binaries: current version v5.0.9, served on the project downloads
# page as single files, plus SHA256SUMS.txt. These are the exact files
# running on the network's own nodes; the pinned hashes below are theirs.
NODE_REL="https://shitcoinbtc.xyz/downloads"
SYSCOIND_SHA256="436dba8c3bd7983887cc3d14945143ff13ea8db95ffaa1e1c28de8a7337f2422"
SYSCOIN_CLI_SHA256="03a02fa8a66c190e26cb7cf1701ccb1e2b45fbf3832a6b8c2ca716774b6c43c8"
# sysgeth is a separate program from the node. This is its current release;
# the md5 below matches the sysgeth binary running on the live nodes today.
SYSGETH_URL="https://github.com/ShitcoinBTC/sysgeth/releases/download/v1.0.0/sysgeth-linux-x86_64.zip"
SYSGETH_MD5="95601fd2e3f0829521bd8206df1f7b4b"
SNAP_URL="https://shitcoinbtc.xyz/snapshots/shitcoin-chain-snapshot-latest.tar.gz"
PROXY_URL="https://shitcoinbtc.xyz/downloads/shitcoin-stratum-proxy.py"
# Stratum proxy: updated on the morning of the block 88,000 fork so it stamps
# AuxPoW chain ID 29 on new blocks. A pre-fork proxy copy stamps chain ID 16
# and its blocks are rejected. Verify your copy against this hash.
PROXY_SHA256="4ace8e29f66578cdf9ae6de2491f2eb564ae2d1efe02358bf651f2e10396c18d"

HOME_DIR="/home/$DATADIR_USER"
SYSCOIN_DIR="$HOME_DIR/.syscoin"
# No SYSGETH_DIR: with v5.0.9 the daemon-managed sysgeth keeps its data
# in $SYSCOIN_DIR/geth. The old home-level .sysgeth directory belonged to
# the superseded standalone-service setup and is not used or created.

echo "==> 1/7 user + packages"
id "$DATADIR_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$DATADIR_USER"
apt-get update -qq
apt-get install -y -qq wget unzip python3 python3-zmq openssl curl ca-certificates libzmq5 libevent-2.1-7 libsqlite3-0 libgmp10 libssl3

echo "==> 2/7 node binaries (current version v5.0.9)"
cd /tmp
rm -rf shitcoin-dl && mkdir shitcoin-dl && cd shitcoin-dl
wget -q "$NODE_REL/syscoind" -O syscoind
wget -q "$NODE_REL/syscoin-cli" -O syscoin-cli
wget -q "$NODE_REL/SHA256SUMS.txt" -O SHA256SUMS.txt
# SHA256SUMS.txt also lists sysgeth (step 3 fetches it separately), so check
# the two files downloaded here; each is then re-checked below against the
# hash pinned in this script.
grep -E '  (syscoind|syscoin-cli)$' SHA256SUMS.txt | sha256sum -c - || { echo "ERROR: v5.0.9 checksum FAILED -- aborting (will not install a bad binary)"; exit 1; }
if [ "$(sha256sum syscoind | awk '{print $1}')" != "$SYSCOIND_SHA256" ]; then
  echo "ERROR: syscoind sha256 mismatch -- aborting"
  exit 1
fi
if [ "$(sha256sum syscoin-cli | awk '{print $1}')" != "$SYSCOIN_CLI_SHA256" ]; then
  echo "ERROR: syscoin-cli sha256 mismatch -- aborting"
  exit 1
fi
install -m 755 syscoind /usr/local/bin/
install -m 755 syscoin-cli /usr/local/bin/
syscoind --version   # must report v5.0.9

echo "==> 3/7 sysgeth (NEVM helper)"
# The v5.0.9 daemon manages sysgeth itself. At startup syscoind stops any
# running sysgeth, then launches the sysgeth it finds beside syscoind
# (/usr/local/bin/sysgeth here). The daemon passes --syscoin and
# --nevmpub itself, driven by syscoin.conf: zmqpubnevm sets the --nevmpub
# address and the gethcommandline lines in step 5 pass the rest,
# including the required --syscoin.statebootstrap.disable. Do NOT run
# sysgeth as a separate service with this build. The NEVM chain ID is
# 20026 -- a different number from the AuxPoW chain ID 29; do not mix
# them up.
cd /tmp
rm -f sysgeth.zip && wget -q "$SYSGETH_URL" -O sysgeth.zip
unzip -o -q sysgeth.zip
if [ "$(md5sum sysgeth | awk '{print $1}')" != "$SYSGETH_MD5" ]; then
  echo "ERROR: sysgeth checksum mismatch -- aborting (will not install a bad binary)"
  exit 1
fi
# N1 HARDENING (2026-10-27, defect analysis 03): sysgeth is the one
# artifact in this script pinned by MD5 only -- a collision-grade hash
# should not be the sole check on an executable. Cross-check SHA256
# against the downloads-page pin for the sysgeth binary
# (SYSGETH_SHA256, from SHA256SUMS.txt). This is deliberately a loud
# WARNING, not a second fatal gate: the zip's extracted binary and the
# downloads-page file are distributed separately, and making an
# unverified byte-identity assumption fatal could break every install
# if their builds ever differ in metadata. The MD5 above remains the
# fatal pin until the zip itself is published with a SHA256 sum, at
# which point this cross-check should become fatal -- see CHANGELOG.
SYSGETH_SHA256="45026660a36a1e8ab9aa5525a72f9ff89a18e7f0a18d905409aca5aa18a51939"
SYSGETH_ACTUAL_SHA256=$(sha256sum sysgeth | awk '{print $1}')
if [ "$SYSGETH_ACTUAL_SHA256" = "$SYSGETH_SHA256" ]; then
  echo "    sysgeth SHA256 also matches the downloads-page pin ($SYSGETH_SHA256)"
else
  echo "    WARNING: sysgeth SHA256 ($SYSGETH_ACTUAL_SHA256) does not match the downloads-page pin ($SYSGETH_SHA256); the MD5 pin DID match. Continuing on the MD5 pin -- report this mismatch (kit CHANGELOG, item N1)."
fi
install -m 755 sysgeth /usr/local/bin/sysgeth
# Upgrades from a 5.0.7-era install: stop and disable a leftover
# standalone sysgeth service so it does not compete with the
# daemon-managed sysgeth.
systemctl disable --now sysgeth 2>/dev/null || true

echo "==> 4/7 chain snapshot (fast sync)"
mkdir -p "$SYSCOIN_DIR"
cd "$SYSCOIN_DIR"
if [ ! -f snapshot-restored.ok ]; then
  # D1 FIX (2026-10-27): download the snapshot under its PUBLISHED name.
  # The published .sha256 file names shitcoin-chain-snapshot-latest.tar.gz;
  # the old script saved the download as snapshot.tar.gz, so
  # `sha256sum -c` looked for a file that did not exist under that name
  # and EVERY install died here ("No such file or directory").
  SNAP_FILE="shitcoin-chain-snapshot-latest.tar.gz"
  wget -q "$SNAP_URL" -O "$SNAP_FILE"
  wget -q "$SNAP_URL.sha256" -O "$SNAP_FILE.sha256"
  sha256sum -c "$SNAP_FILE.sha256" || { echo "ERROR: snapshot checksum FAILED"; exit 1; }
  # Dual-format snapshot extract: handles both the dated-folder layout
  # (older snapshots) and a flat layout (newer ones may not nest).
  # D2 FIX (2026-10-27): do NOT probe the format with
  # `tar tzf ... | head -1 | grep -q ...`. Under this script's
  # `set -o pipefail`, head/grep exit on the first line, tar dies with
  # SIGPIPE (exit 141), the pipeline reports failure, the prefixed-dir
  # branch is never taken, and the install aborts with
  # "blocks/chainstate missing" on a perfectly good snapshot.
  # Instead: write the full listing to a file first (no early-exit
  # consumer on the pipe), then read its first line from the file.
  tar tzf "$SNAP_FILE" > snapshot-list.txt
  FIRST_ENTRY=""
  IFS= read -r FIRST_ENTRY < snapshot-list.txt || true
  case "$FIRST_ENTRY" in
    shitcoin-chain-snapshot-*) tar xzf "$SNAP_FILE" --strip-components=1 ;;
    *) tar xzf "$SNAP_FILE" ;;
  esac
  rm -f snapshot-list.txt
  [ -d "$SYSCOIN_DIR/blocks" ] && [ -d "$SYSCOIN_DIR/chainstate" ] || { echo "ERROR: snapshot extracted but blocks/chainstate missing -- aborting"; exit 1; }
  rm -f "$SNAP_FILE" "$SNAP_FILE.sha256" snapshot.tar.gz snapshot.tar.gz.sha256
  touch snapshot-restored.ok
else
  echo "    snapshot already restored, skipping download"
fi
chown -R "$DATADIR_USER:$DATADIR_USER" "$SYSCOIN_DIR"

echo "==> 5/7 node config + service"
RPCUSER="shitcoinrpc"
RPCPASS=""
if [ ! -f "$SYSCOIN_DIR/syscoin.conf" ]; then
  RPCPASS=$(openssl rand -hex 24)
  cat > "$SYSCOIN_DIR/syscoin.conf" <<EOF
server=1
daemon=1
txindex=1
port=8369
rpcport=8370
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcuser=$RPCUSER
rpcpassword=$RPCPASS
zmqpubnevm=tcp://127.0.0.1:9377
gethcommandline=--syscoin.statebootstrap.disable
gethcommandline=--nodiscover
gethcommandline=--maxpeers=0
gethcommandline=--http
gethcommandline=--http.addr=127.0.0.1
gethcommandline=--http.port=8545
gethcommandline=--http.api=eth,net,web3
maxconnections=40
dbcache=512
addnode=77.42.74.252
addnode=170.101.111.196
addnode=216.106.186.238
EOF
  chmod 600 "$SYSCOIN_DIR/syscoin.conf"
  chown "$DATADIR_USER:$DATADIR_USER" "$SYSCOIN_DIR/syscoin.conf"
  # N2 FIX (2026-10-27, defect analysis 03): do NOT echo the RPC
  # password. The old line printed it to stdout, so the documented
  # curl|bash run leaked the node's RPC credential into terminal
  # scrollback, CI logs and support transcripts. It is already saved
  # in syscoin.conf (mode 600) -- printing it adds exposure only.
  echo "    RPC credentials saved in $SYSCOIN_DIR/syscoin.conf (mode 600; not printed -- see N2 in the kit CHANGELOG)"
else
  echo "    syscoin.conf exists, keeping it"
  # D5 FIX, existing-config path: recover the credentials the daemon is
  # actually configured with, so step 6 can give the proxy the SAME
  # credentials (see below). Values are never printed.
  RPCUSER=$(grep -E '^rpcuser=' "$SYSCOIN_DIR/syscoin.conf" | tail -n 1 | cut -d= -f2- | tr -d ' \r' || true)
  RPCPASS=$(grep -E '^rpcpassword=' "$SYSCOIN_DIR/syscoin.conf" | tail -n 1 | cut -d= -f2- | tr -d ' \r' || true)
  [ -n "$RPCUSER" ] || RPCUSER="shitcoinrpc"
fi
# D5 FIX (2026-10-27): a daemon configured with rpcuser/rpcpassword
# NEVER writes a .cookie file. The old installer nevertheless pointed
# the proxy at the cookie and left NODE_RPC_USER/NODE_RPC_PASS empty,
# so the proxy crash-looped forever on the missing cookie -- the two
# halves of the install could never connect, on any host. Refuse to
# continue without credentials to hand the proxy.
if [ -z "$RPCPASS" ]; then
  echo "ERROR: no rpcpassword found in $SYSCOIN_DIR/syscoin.conf -- the stratum proxy would have no way to authenticate to the daemon. Add rpcuser/rpcpassword to syscoin.conf and re-run."; exit 1
fi
cat > /etc/systemd/system/syscoind.service <<EOF
[Unit]
Description=ShitcoinBtc node
After=network.target
[Service]
# D3 FIX (2026-10-27): syscoin.conf sets daemon=1 (the daemon forks
# into the background). A unit with no Type= defaults to Type=simple,
# under which systemd treats the forking parent's exit as the service
# stopping: it tears down the cgroup (killing the real daemon) and
# Restart=always repeats the cycle forever (measured NRestarts=6 in
# ~3.5 min; step 7 then never reaches the daemon and the old script
# still exited 0 printing DONE). Type=forking is the unit type that
# matches a self-daemonizing daemon.
Type=forking
PIDFile=$SYSCOIN_DIR/syscoind.pid
User=$DATADIR_USER
ExecStart=/usr/local/bin/syscoind -datadir=$SYSCOIN_DIR -conf=$SYSCOIN_DIR/syscoin.conf
Restart=always
RestartSec=30
TimeoutStartSec=120
LimitNOFILE=65535
[Install]
WantedBy=multi-user.target
EOF

echo "==> 6/7 stratum proxy (merged mining handled for you)"
# D4 FIX (2026-10-27): create every directory the service user must
# traverse or write with install -d -o <service-user>, INCLUDING the
# config directory /etc/shitcoin-stratum-proxy itself. The old script
# used a plain root mkdir -p for it and only chowned the proxy.conf
# FILE inside: on any host whose root umask is 027 or stricter the
# directory lands root:root 770, the shitcoin service user cannot
# traverse it, and the proxy crash-loops on
# PermissionError: '/etc/shitcoin-stratum-proxy/proxy.conf'.
# (On a stock umask-022 host the directory lands 755 and the defect
# stays latent -- which is how it shipped.)
install -d -o "$DATADIR_USER" -g "$DATADIR_USER" -m 0755 /etc/shitcoin-stratum-proxy
install -d -o "$DATADIR_USER" -g "$DATADIR_USER" -m 0755 /opt/shitcoin-stratum-proxy /var/log/shitcoin-stratum-proxy /var/lib/shitcoin-stratum-proxy
# Spool dir: mode 0700, owned by the service user -- anyone who can write
# it can forge or delete found-block entries and alarms (guide section 8A).
install -d -o "$DATADIR_USER" -g "$DATADIR_USER" -m 0700 /var/lib/shitcoin-stratum-proxy/spool
wget -q "$PROXY_URL" -O /opt/shitcoin-stratum-proxy/shitcoin-stratum-proxy.py
if [ "$(sha256sum /opt/shitcoin-stratum-proxy/shitcoin-stratum-proxy.py | awk '{print $1}')" != "$PROXY_SHA256" ]; then
  echo "ERROR: stratum proxy checksum mismatch -- aborting (an old pre-fork proxy stamps chain ID 16 and its blocks are rejected)"
  exit 1
fi
chmod 755 /opt/shitcoin-stratum-proxy/shitcoin-stratum-proxy.py
python3 -m py_compile /opt/shitcoin-stratum-proxy/shitcoin-stratum-proxy.py
# RT-3 FIX (mandatory re-check): validate --payout with the downloaded
# proxy's OWN address parser -- literally the code that will refuse
# the address at startup if it is wrong.  Runs before any config is
# written or service started, so a bad address can never produce a
# "successful" install whose proxy crash-loops on it.
python3 - "$PAYOUT" <<'PYEOF' || { echo "ERROR: --payout '$PAYOUT' was rejected by the stratum proxy's own address parser (bech32 checksum / format). Fix the address and re-run -- nothing has been configured or started with it."; exit 1; }
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    "shitcoin_stratum_proxy", "/opt/shitcoin-stratum-proxy/shitcoin-stratum-proxy.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.addr_to_scriptpubkey(sys.argv[1])
print("    payout address verified by the proxy's own parser")
PYEOF
cat > /etc/shitcoin-stratum-proxy/proxy.conf <<EOF
LISTEN_HOST=127.0.0.1
LISTEN_PORT=$STRATUM_PORT
PAYOUT_ADDRESS=$PAYOUT
NODE_RPC_URL=http://127.0.0.1:8370/
NODE_COOKIE=$SYSCOIN_DIR/.cookie
# D5 FIX (2026-10-27): hand the proxy the daemon's REAL credentials.
# With rpcuser/rpcpassword set in syscoin.conf the daemon never writes
# a .cookie, so the old empty defaults here left the proxy with no
# working auth at all (crash-loop on the missing cookie file). The
# shipped proxy checks NODE_RPC_USER first and only falls back to the
# cookie when it is empty (SyscoinRPCBackend._auth), so setting these
# two keys is the whole fix. Values come from step 5 (generated there,
# or parsed back out of an existing syscoin.conf); never printed here
# beyond what step 5 already prints for the operator's own record.
NODE_RPC_USER=$RPCUSER
NODE_RPC_PASS=$RPCPASS
NEVM_ZMQ=tcp://127.0.0.1:9377
SHARE_DIFF=0.5
VARDIFF=1
JOB_REFRESH_SECS=30
LOG_FILE=/var/log/shitcoin-stratum-proxy/proxy.log
# REQUIRED section 8A submission protection -- exactly the keys the
# shipped proxy reads (final build). The spool is written before every
# submission and replayed on startup AND every SPOOL_REPLAY_SECS; the
# alarm (log + stderr + ALARM.txt in the spool dir) is always on;
# ALARM_CMD is an optional extra hook, empty = no hook.
SPOOL_DIR=/var/lib/shitcoin-stratum-proxy/spool
SUBMIT_RETRIES=3
SUBMIT_BACKOFF_SECS=2
SPOOL_REPLAY_SECS=60
ALARM_CMD=
EOF
chmod 600 /etc/shitcoin-stratum-proxy/proxy.conf
chown "$DATADIR_USER:$DATADIR_USER" /etc/shitcoin-stratum-proxy/proxy.conf
# D4 FIX (continued): re-assert directory ownership/mode AFTER writing
# the file in it, so no later step can leave the directory root-owned.
chown "$DATADIR_USER:$DATADIR_USER" /etc/shitcoin-stratum-proxy
chmod 0755 /etc/shitcoin-stratum-proxy
chown -R "$DATADIR_USER:$DATADIR_USER" /var/log/shitcoin-stratum-proxy /opt/shitcoin-stratum-proxy /var/lib/shitcoin-stratum-proxy
chmod 0700 /var/lib/shitcoin-stratum-proxy/spool
cat > /etc/systemd/system/shitcoin-stratum-proxy.service <<EOF
[Unit]
Description=ShitcoinBtc merged-mining stratum proxy
After=network.target syscoind.service
Wants=syscoind.service
[Service]
Type=simple
User=$DATADIR_USER
Group=$DATADIR_USER
ExecStart=/usr/bin/python3 /opt/shitcoin-stratum-proxy/shitcoin-stratum-proxy.py /etc/shitcoin-stratum-proxy/proxy.conf
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

echo "==> starting services"
systemctl daemon-reload
systemctl enable --now syscoind shitcoin-stratum-proxy

echo "==> 7/7 dedicated acceptance wallet"
# Parameterless getauxblock is a wallet RPC: with no wallet loaded it
# fails, so a fresh install would fail its own acceptance test. Create a
# dedicated wallet named "pool" and name it with -rpcwallet= in the
# acceptance test below. (The wallet-free createauxblock <payout> form
# in the guide is an equal acceptance path and needs none of this.)
WALLET_NAME="pool"
_cli() { runuser -u "$DATADIR_USER" -- syscoin-cli -datadir="$SYSCOIN_DIR" "$@"; }
# D3 FIX (consequence): the old loop silently gave up after 120 s and
# the script went on to print DONE and exit 0 even when the daemon had
# never answered once -- a silent broken success. A daemon that does
# not answer is a FAILED install: say so and exit non-zero.
DAEMON_UP=0
for _ in $(seq 1 60); do if _cli getblockcount >/dev/null 2>&1; then DAEMON_UP=1; break; fi; sleep 2; done
if [ "$DAEMON_UP" != "1" ]; then
  echo "ERROR: syscoind did not answer RPC within 120 s -- the install is NOT complete. Check: systemctl status syscoind; journalctl -u syscoind -n 50" >&2
  exit 1
fi
if ! _cli -rpcwallet="$WALLET_NAME" getwalletinfo >/dev/null 2>&1; then
  if ! _cli createwallet "$WALLET_NAME" >/dev/null 2>&1 && ! _cli loadwallet "$WALLET_NAME" >/dev/null 2>&1; then
    echo "ERROR: wallet '$WALLET_NAME' could not be created or loaded although the daemon is up -- the install is NOT complete (the acceptance test needs this wallet). Check: journalctl -u syscoind -n 50" >&2
    exit 1
  fi
fi

# RT-3 FIX (step-7 audit): D6 hardened step 7 for the daemon and the
# wallet, but the step never checked the PROXY -- the component
# miners actually connect to -- so an install whose proxy could
# never start still printed DONE and exited 0 (the proxy validates
# the payout in main() BEFORE listening; a bad address crash-loops
# it forever).  Close the class, not just the instance: every
# component this installer installs gets a step-7 check below.
#   daemon RPC        -- checked above (DAEMON_UP, exit 1)
#   pool wallet       -- checked above (exit 1)
#   stratum proxy     -- service active AND stratum port listening
#   proxy.conf auth   -- NODE_RPC_USER/PASS present and non-empty
#   spool dir         -- exists, mode 0700, owned by the service user
#   sysgeth           -- daemon-managed (no unit of its own): warn
#                        if no process / NEVM RPC yet; the operator
#                        acceptance test below is what proves it,
#                        because templates require sync + mnsync.
echo "==> 7/7 (continued) stratum proxy liveness"
PROXY_UP=0
for _ in $(seq 1 30); do
  if systemctl is-active --quiet shitcoin-stratum-proxy && (exec 3<>"/dev/tcp/127.0.0.1/$STRATUM_PORT") 2>/dev/null; then exec 3>&- 3<&-; PROXY_UP=1; break; fi
  sleep 2
done
if [ "$PROXY_UP" != "1" ]; then
  echo "ERROR: the stratum proxy is not active and listening on 127.0.0.1:$STRATUM_PORT after 60 s -- the install is NOT complete (miners cannot connect). Check: systemctl status shitcoin-stratum-proxy; journalctl -u shitcoin-stratum-proxy -n 50" >&2
  exit 1
fi
grep -q '^NODE_RPC_USER=.' /etc/shitcoin-stratum-proxy/proxy.conf && grep -q '^NODE_RPC_PASS=.' /etc/shitcoin-stratum-proxy/proxy.conf || { echo "ERROR: proxy.conf is missing its NODE_RPC_USER/NODE_RPC_PASS credentials -- the install is NOT complete." >&2; exit 1; }
[ "$(stat -c %a /var/lib/shitcoin-stratum-proxy/spool)" = "700" ] && [ "$(stat -c %U /var/lib/shitcoin-stratum-proxy/spool)" = "$DATADIR_USER" ] || { echo "ERROR: spool dir /var/lib/shitcoin-stratum-proxy/spool is not mode 0700 owned by $DATADIR_USER -- the install is NOT complete." >&2; exit 1; }
if ! pgrep -x sysgeth >/dev/null 2>&1; then
  echo "    WARNING: no sysgeth process seen yet. syscoind manages sysgeth itself and may still be starting it; if it never appears, NEVM templates (and therefore proxy jobs) cannot be built -- check 'pgrep -a sysgeth' and the daemon log before mining."
fi

echo ""
echo "DONE. Your pool node is starting."
echo "  version: v5.0.9 (current version; AuxPoW chain ID 29 since block 88,000)"
echo "  node:    sudo -u $DATADIR_USER syscoin-cli -datadir=$SYSCOIN_DIR getblockcount"
echo "  stratum: 127.0.0.1:$STRATUM_PORT  (point your miners here)"
echo "  payout:  $PAYOUT"
echo "  proxy:   journalctl -u shitcoin-stratum-proxy -f"
echo ""
echo "  ACCEPTANCE TEST -- setup is not done until this passes. Either form"
echo "  passing is acceptance; both must return a template containing"
echo "  \"chainid\": 29 once the node is synced AND mnsync has completed:"
echo "    sudo -u $DATADIR_USER syscoin-cli -datadir=$SYSCOIN_DIR -rpcwallet=$WALLET_NAME getauxblock"
echo "    sudo -u $DATADIR_USER syscoin-cli -datadir=$SYSCOIN_DIR createauxblock $PAYOUT   # wallet-free, equal path"
echo "  If getauxblock refuses with a masternode-sync error, that is the"
echo "  normal 2-3 minute mnsync after every start -- check"
echo "  'syscoin-cli -datadir=$SYSCOIN_DIR mnsync status', wait, re-run."
echo ""
echo "  REQUIRED (guide section 8A): submission protection is written into"
echo "  proxy.conf with the proxy's real keys (SPOOL_DIR, SUBMIT_RETRIES=3,"
echo "  SUBMIT_BACKOFF_SECS=2, SPOOL_REPLAY_SECS=60, ALARM_CMD=) and the"
echo "  spool dir was created at /var/lib/shitcoin-stratum-proxy/spool"
echo "  (0700, owned by $DATADIR_USER). Test the alarm once before pointing"
echo "  real hash at this box. A silent lost block is a setup gap."
echo ""
echo "  RETRY SEMANTICS (exact): a found block is retried as the IDENTICAL"
echo "  payload only. Between attempts the proxy re-checks the tip; once"
echo "  the tip has taken the block's height, retry stops. A found"
echo "  solution is NEVER transplanted onto a fresh template -- that is"
echo "  not possible and this kit does not claim to do it. What cannot"
echo "  land stays in the spool and raises the alarm, loudly."
echo ""
echo "  TIP-CHANGE REFRESH (this proxy path): the proxy refreshes itself"
echo "  with its built-in 5-second tip poll and rebuilds its job on every"
echo "  tip change -- there is NO blocknotify line for the proxy and none"
echo "  is written. Verify it after the next block arrives: the proxy log"
echo "  must show 'new tip <height> ... rebuilding job'. (blocknotify"
echo "  wiring IS required on the yiimp-adapter and own-software paths;"
echo "  see guide section 8A -- one blocknotify= line only, chained with"
echo "  ';' if two consumers need a poke.)"
echo ""
echo "Note on the snapshot: it was built 2026-10-18 at height 88,386, so the"
echo "node still has roughly 22,000 blocks to catch up (as of 2026-10-26) --"
echo "that takes real time, it is not a fault. Once it has caught up, connect"
echo "any stock stratum miner (cpuminer, cgminer, ...) -- no merged-mining"
echo "support needed. The proxy builds every block's AuxPoW for you."
