#!/usr/bin/env python3
"""ShitcoinBTC public read-only chain API — /api/v1/chain/

Stdlib only. Binds 127.0.0.1:5020 (CHAIN_API_PORT env).
Data source ONLY: node1 syscoind JSON-RPC (cookie auth, 127.0.0.1:8370)
plus sysgeth HTTP (127.0.0.1:8545) for NEVM chain-id/head reporting.
Read-only: this service issues only query RPCs (getblockchaininfo,
getnetworkinfo, getmininginfo, getchaintxstats, getmempoolinfo,
gettxoutsetinfo, getblockhash, getblock, getrawtransaction,
getnevmblockchaininfo; eth_chainId/eth_blockNumber/eth_syncing).
No wallet RPCs, no POST/PUT/DELETE handlers, no DB access.

Response contract (API STANDARDS.md v1.0):
  success: flat JSON fields + top-level "timestamp" (UTC ISO-8601 Z)
  error:   {"error": str, "code": <http status>, "timestamp": ...}

Supply method (documented in README.md + /supply response):
  circulating_supply = gettxoutsetinfo.total_amount at the tip
    (sum of every unspent output; the only on-chain-measured supply)
  total_issued_supply = 8,400,000,000 (block 1, verified on-chain:
    3.78B presale + 2.94B team + 0.42B coin support + 1.26B wrapping
    reserve) + sum over blocks 2..height of the per-block coinbase
    issuance 178.26466861 SHIT, decaying 2% every 1,051,200 blocks
    (100 eras). Verified 2026-10-14: formula vs gettxoutsetinfo
    differed by 0.01024491 SHIT at height 77,895 (burned/unspendable
    outputs sit in issued but not in the UTXO set).
  max_supply = 21,000,000,000 (documented hard cap).
  The 17% vault yield reserve (3.57B) is protocol-tracked, is NOT in
  the UTXO set, and is excluded from both supply figures above.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("CHAIN_API_PORT", "5020"))
RPC_URL = os.environ.get("SYSCOIN_RPC_URL", "http://127.0.0.1:8370/")
COOKIE = os.environ.get("SYSCOIN_COOKIE", "/home/shitcoin/.syscoin/.cookie")
GETH_URL = os.environ.get("SYSGETH_URL", "http://127.0.0.1:8545/")
BASE = "/api/v1/chain"

MAX_SUPPLY = 21_000_000_000
BLOCK1_ALLOCATION = Decimal("8400000000")
ERA0_REWARD = Decimal("178.26466861")
ERA_LEN = 1_051_200
ERAS = 100

_cache = {}
_lock = threading.Lock()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _rpc(url, method, params, timeout=10):
    version = "2.0" if url == GETH_URL else "1.0"  # geth rejects 1.0
    body = json.dumps({"jsonrpc": version, "id": "chain-api",
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    if url == RPC_URL:
        with open(COOKIE) as f:
            userpass = f.read().strip()
        import base64
        req.add_header("Authorization", "Basic " +
                       base64.b64encode(userpass.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Bitcoin-style RPC reports errors with an HTTP error status and a
        # JSON body; urllib raises before the body can be read, which used
        # to turn every RPC error (incl. unknown txid, code -5) into a
        # generic 500. Parse the body so the RPC error code survives and
        # do_GET can map not-found to 404.
        try:
            out = json.loads(e.read())
        except Exception:
            raise RpcError(f"upstream HTTP {e.code}")
    if out.get("error"):
        raise RpcError(out["error"].get("message", str(out["error"])),
                       out["error"].get("code"))
    return out.get("result")


class RpcError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.rpc_code = code


def rpc(method, *params):
    return _rpc(RPC_URL, method, list(params))


def geth(method, *params):
    return _rpc(GETH_URL, method, list(params), timeout=4)


def cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def issued_supply(height):
    """Decimal issued-UTXO supply: block-1 allocation + mining issuance
    for blocks 2..height segmented by 2%-decay eras of 1,051,200 blocks."""
    total = BLOCK1_ALLOCATION
    remaining = max(0, height - 1)  # blocks 2..height inclusive
    next_h = 2
    while remaining > 0:
        era = next_h // ERA_LEN  # matches explorer blockRewardFunction
        if era >= ERAS:
            break
        era_last_h = (era + 1) * ERA_LEN - 1
        take = min(remaining, era_last_h - next_h + 1)
        reward = ERA0_REWARD * (Decimal("0.98") ** era)
        total += reward * take
        next_h += take
        remaining -= take
    return total


def dec_str(d):
    return format(d, "f")


def nevm_status():
    """Report both sources verbatim; never synthesise a 'live' claim."""
    out = {"chain_id": None, "geth_block_number": None,
           "syscoind_height": None, "syscoind_status": None,
           "syscoind_bestblockhash": None}
    try:
        info = rpc("getnevmblockchaininfo")
        out["syscoind_height"] = info.get("height")
        out["syscoind_status"] = info.get("status")
        out["syscoind_bestblockhash"] = info.get("bestblockhash")
    except Exception:
        pass
    try:
        cid = geth("eth_chainId")
        out["chain_id"] = int(cid, 16) if cid else None
    except Exception:
        pass
    try:
        bn = geth("eth_blockNumber")
        out["geth_block_number"] = int(bn, 16) if bn is not None else None
    except Exception:
        pass
    return out


def get_status():
    def build():
        info = rpc("getblockchaininfo")
        net = rpc("getnetworkinfo")
        mining = rpc("getmininginfo")
        return {
            "chain": info["chain"],
            "height": info["blocks"],
            "headers": info["headers"],
            "tip_hash": info["bestblockhash"],
            "difficulty": info["difficulty"],
            "mediantime": info["mediantime"],
            "size_on_disk_bytes": info["size_on_disk"],
            "pruned": info["pruned"],
            "initial_block_download": info["initialblockdownload"],
            "peers": net["connections"],
            "peers_in": net["connections_in"],
            "peers_out": net["connections_out"],
            "network_active": net["networkactive"],
            "node_version": net["subversion"],
            "protocol_version": net["protocolversion"],
            "network_hash_ps": mining.get("networkhashps"),
            "txindex_synced": rpc("getindexinfo").get("txindex", {}).get("synced"),
            "nevm": nevm_status(),
        }
    return cached("status", 10, build)


def get_supply():
    def build():
        info = rpc("getblockchaininfo")
        h = info["blocks"]
        utxo = cached("utxo", 120, lambda: rpc("gettxoutsetinfo"))
        issued = issued_supply(h)
        circ = Decimal(str(utxo["total_amount"]))
        return {
            "height": h,
            "max_supply": MAX_SUPPLY,
            "max_supply_unit": "SHIT",
            "circulating_supply": dec_str(circ),
            "circulating_supply_method":
                "gettxoutsetinfo.total_amount at the tip: sum of all "
                "unspent transaction outputs (on-chain measured)",
            "total_issued_supply": dec_str(issued),
            "total_issued_supply_method":
                "8400000000 block-1 allocation (verified on-chain: "
                "3780000000 presale + 2940000000 team + 420000000 coin "
                "support + 1260000000 wrapping reserve) + blocks 2..height "
                "x 178.26466861 SHIT coinbase issuance, decaying 2% every "
                "1051200 blocks (100 eras)",
            "issued_minus_circulating": dec_str(issued - circ),
            "issued_minus_circulating_note":
                "burned / unspendable outputs are issued but not in the "
                "UTXO set; observed 0.01024491 SHIT at height 77895",
            "utxo_set": {"txouts": utxo["txouts"],
                         "transactions": utxo["transactions"],
                         "bestblock": utxo["bestblock"],
                         "height": utxo["height"]},
            "excluded": "the 17% vault yield reserve (3570000000 SHIT) is "
                        "protocol-tracked, not issued into the UTXO set, "
                        "and is excluded from both figures",
            "unit": "SHIT",
        }
    return cached("supply", 10, build)


def get_stats():
    def build():
        info = rpc("getblockchaininfo")
        txstats = rpc("getchaintxstats")
        mempool = rpc("getmempoolinfo")
        mining = rpc("getmininginfo")
        utxo = cached("utxo", 120, lambda: rpc("gettxoutsetinfo"))
        return {
            "height": info["blocks"],
            "tip_hash": info["bestblockhash"],
            "difficulty": info["difficulty"],
            "chainwork": info["chainwork"],
            "size_on_disk_bytes": info["size_on_disk"],
            "total_transactions": txstats["txcount"],
            "tx_rate_per_sec": txstats["txrate"],
            "utxo_transactions": utxo["transactions"],
            "utxo_outputs": utxo["txouts"],
            "utxo_total_amount": dec_str(Decimal(str(utxo["total_amount"]))),
            "network_hash_ps": mining.get("networkhashps"),
            "mempool": {"tx_count": mempool["size"],
                        "bytes": mempool["bytes"],
                        "total_fee_shit": mempool["total_fee"]},
            "target_block_time_seconds": 30,
        }
    return cached("stats", 10, build)


HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=10")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg, "code": code, "timestamp": now_iso()})

    def _ok(self, obj):
        obj = dict(obj)
        obj["timestamp"] = now_iso()
        self._send(200, obj)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            self._route()
        except RpcError as e:
            if e.rpc_code in (-5, -8) or "no such" in str(e).lower() \
                    or "not found" in str(e).lower():
                # not found / invalid hash-or-height
                self._err(404, "not found on chain")
            else:
                self._err(503, "chain node unavailable")
        except FileNotFoundError:
            self._err(503, "chain node unavailable")
        except Exception:
            self._err(500, "internal error")

    do_HEAD = do_GET

    def _route(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == BASE:
            return self._ok({
                "service": "shitcoin-public-chain-api", "version": "v1",
                "endpoints": [
                    "GET /api/v1/chain/status",
                    "GET /api/v1/chain/block/{hashOrHeight}",
                    "GET /api/v1/chain/tx/{txid}",
                    "GET /api/v1/chain/supply",
                    "GET /api/v1/chain/stats",
                ]})
        if not path.startswith(BASE + "/"):
            return self._err(404, "unknown route")
        rest = path[len(BASE) + 1:]

        if rest == "status":
            return self._ok(get_status())
        if rest == "supply":
            return self._ok(get_supply())
        if rest == "stats":
            return self._ok(get_stats())
        if rest.startswith("block/"):
            ident = rest[6:]
            if ident.isdigit():
                h = int(ident)
                tip = rpc("getblockcount")
                if h < 0 or h > tip:
                    return self._err(404, "block height out of range")
                block_hash = rpc("getblockhash", h)
            elif HEX64.match(ident):
                block_hash = ident.lower()
            else:
                return self._err(400, "block id must be a height or 64-char hex hash")
            block = rpc("getblock", block_hash, 2)
            block = {k: v for k, v in block.items() if k != "auxpow"}
            return self._ok({"block": block})
        if rest.startswith("tx/"):
            txid = rest[3:]
            if not HEX64.match(txid):
                return self._err(400, "txid must be 64-char hex")
            tx = rpc("getrawtransaction", txid.lower(), 1)
            return self._ok({"tx": tx})
        return self._err(404, "unknown route")

    def _method_not_allowed(self):
        self._err(405, "method not allowed: read-only API")

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
