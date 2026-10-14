#!/usr/bin/env bash
# check-bitcoin-mainnet.sh — read-only status check for a Bitcoin Core
# node that will act as the merge-mining parent for Shitcoin.
#
# Proves, without changing anything on the node:
#   1. the node is on Bitcoin MAINNET (chain == "main", nothing else)
#   2. it is out of initial block download and synced
#   3. it can serve block-template work (getblocktemplate), so a pool
#      can obtain real work from it
#
# Usage:
#   contrib/mining/check-bitcoin-mainnet.sh [bitcoin-cli args...]
# Examples:
#   contrib/mining/check-bitcoin-mainnet.sh bitcoin-cli
#   contrib/mining/check-bitcoin-mainnet.sh bitcoin-cli -datadir=/home/user/.bitcoin
#   contrib/mining/check-bitcoin-mainnet.sh bitcoin-cli -rpcuser=u -rpcpassword=p
#
# Exit code 0 = all checks PASS. 1 = a check failed (do not mine
# against this node). 2 = could not talk to the node at all.

set -u

if [ "$#" -lt 1 ]; then
    echo "usage: $0 <bitcoin-cli command...> (e.g. $0 bitcoin-cli -datadir=/path)" >&2
    exit 2
fi

CLI=("$@")
failures=0

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; failures=$((failures + 1)); }

info="$("${CLI[@]}" getblockchaininfo 2>/dev/null)" || {
    echo "FAIL: cannot reach bitcoind via: ${CLI[*]} getblockchaininfo" >&2
    echo "Is Bitcoin Core running, on mainnet config, with RPC reachable from this host?" >&2
    exit 2
}

# Extract a top-level string/number/bool field from the JSON without
# requiring jq: matches the first "field": value occurrence, which for
# getblockchaininfo's flat fields (chain, blocks, ibd, progress) is
# the top-level one.
field() {
    printf '%s\n' "$info" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\([^, }]*\).*/\1/p" | head -n1 | tr -d '", '
}

chain="$(field chain)"
blocks="$(field blocks)"
ibd="$(field initialblockdownload)"
progress="$(field verificationprogress)"

echo "node reports: chain=${chain:-?} blocks=${blocks:-?} initialblockdownload=${ibd:-?} verificationprogress=${progress:-?}"

if [ "$chain" = "main" ]; then
    pass "chain is main (Bitcoin mainnet)"
else
    fail "chain is '${chain:-unknown}', not 'main' — testnet/regtest/signet/synthetic nodes cannot be a production parent"
fi

if [ "$ibd" = "false" ]; then
    pass "node is out of initial block download"
else
    fail "node is still in initial block download (initialblockdownload=${ibd:-unknown})"
fi

# Compare verificationprogress numerically without bc(1).
if [ -n "$progress" ] && awk -v p="$progress" 'BEGIN { exit !(p >= 0.999) }'; then
    pass "verificationprogress ${progress} (>= 0.999, synced)"
else
    fail "verificationprogress is ${progress:-unknown} (< 0.999, not synced yet)"
fi

case "$blocks" in
    ''|*[!0-9]*) fail "could not read a numeric block height (blocks=${blocks:-unknown})" ;;
    *) [ "$blocks" -gt 0 ] && pass "block height ${blocks} (> 0)" || fail "block height is 0" ;;
esac

tmpl="$("${CLI[@]}" getblocktemplate '{"rules":["segwit"]}' 2>/dev/null)" || tmpl=""
if printf '%s' "$tmpl" | grep -q '"previousblockhash"'; then
    pass "getblocktemplate returned work (pool can obtain a job from this node)"
else
    fail "getblocktemplate did not return a template — a pool cannot obtain work from this node"
fi

echo
if [ "$failures" -eq 0 ]; then
    echo "RESULT: PASS — this is a synced Bitcoin Core mainnet node that can serve pool work."
    echo "Reminder: compare its bestblockhash against a public Bitcoin explorer before mining."
    exit 0
else
    echo "RESULT: FAIL (${failures} check(s)) — do NOT mine against this node."
    exit 1
fi
