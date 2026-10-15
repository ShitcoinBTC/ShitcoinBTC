#!/usr/bin/env python3
"""
SHIT Pool - merge-mining stratum server (node2).

Parent chain: Bitcoin (templates via getblocktemplate from node3 bitcoind).
Aux chains:   SHIT (via local syscoind, gated on NEVM feed), NMC (config flag).

Economics (Jodie's orders, do not change without him):
  - 0.002% BTC tax in EVERY coinbase:  team_tax = coinbasevalue * 2 // 100000
    paid to Jodie's P2SH 3FPSdy4dtNpMJxEnrhXMBEm3akEUY3Z592.
    NEVER remove this output. NEVER pay any other team address.
  - PPLNS accounting. 1% pool fee on merged-coin (SHIT/NMC) rewards at
    accounting time. No fee on BTC beyond the coinbase tax.

Payouts are DISABLED by default (PAYOUTS_ENABLED=false): the engine runs in
dry-run and only logs what it would pay. No funds move without Jodie's approval
of the payout service.
"""

import base64
import collections
import fcntl
import hashlib
import html
import json
import os
import re
import socket
import sqlite3
import struct
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- config ---

CONF_PATH = os.environ.get("POOL_CONF", "/home/shitcoin/pool/pool.conf")

DEFAULTS = {
    "BTC_RPC_HOST": "127.0.0.1",
    "BTC_RPC_PORT": "8332",
    "BTC_RPC_USER": "pick-a-username",
    "BTC_RPC_PASSWORD": "",
    "SHIT_RPC_HOST": "127.0.0.1",
    "SHIT_RPC_PORT": "8370",
    "SHIT_COOKIE": "/home/shitcoin/.syscoin/.cookie",
    "SHIT_WALLET": "pool",
    "POOL_BTC_ADDRESS": "3FPSdy4dtNpMJxEnrhXMBEm3akEUY3Z592",  # default: Jodie's P2SH (safe: funds can't be lost)
    "TAX_BTC_ADDRESS": "3FPSdy4dtNpMJxEnrhXMBEm3akEUY3Z592",    # Jodie's wallet - DO NOT CHANGE
    "STRATUM_PORT": "3333",
    "HTTP_PORT": "8080",
    "NMC_ENABLED": "false",
    "NMC_RPC_HOST": "127.0.0.1",
    "NMC_RPC_PORT": "8336",
    "NMC_RPC_USER": "pick-a-username",
    "NMC_RPC_PASSWORD": "changeme",
    "FB_ENABLED": "false",
    "FB_RPC_HOST": "127.0.0.1",
    "FB_RPC_PORT": "8337",
    "FB_RPC_USER": "pick-a-username",
    "FB_RPC_PASSWORD": "changeme",
    "FB_PAYOUT_ADDRESS": "",
    "ELA_ENABLED": "false",
    "ELA_RPC_HOST": "127.0.0.1",
    "ELA_RPC_PORT": "20336",
    "ELA_RPC_USER": "pick-a-username",
    "ELA_RPC_PASSWORD": "changeme",
    "ELA_PAYOUT_ADDRESS": "",
    "SYS_ENABLED": "false",
    "SYS_RPC_HOST": "127.0.0.1",
    "SYS_RPC_PORT": "18370",
    "SYS_RPC_USER": "pick-a-username",
    "SYS_RPC_PASSWORD": "changeme",
    "SYS_PAYOUT_ADDRESS": "",
    "UNO_ENABLED": "false",
    "UNO_RPC_HOST": "127.0.0.1",
    "UNO_RPC_PORT": "65535",
    "UNO_RPC_USER": "pick-a-username",
    "UNO_RPC_PASSWORD": "changeme",
    "UNO_PAYOUT_ADDRESS": "",
    "SHIT_AUX_ENABLED": "true",          # gated on NEVM feed at runtime
    "NEVM_ZMQ_ADDR": "tcp://127.0.0.1:9377",
    "PAYOUTS_ENABLED": "false",
    "BTC_PAYOUT_THRESHOLD_SATS": "100000",       # 0.001 BTC
    "SHIT_PAYOUT_THRESHOLD_SATS": "10000000000",  # 100 SHIT
    "DATA_DIR": "/home/shitcoin/pool",
    "POOL_TAG": "/SHIT-Pool/",
}


def load_config(path=CONF_PATH):
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


CFG = load_config()
DATA_DIR = CFG["DATA_DIR"]
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------- logging ---

LOG_PATH = os.path.join(DATA_DIR, "pool.log")
_log_lock = threading.Lock()


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), msg)
    with _log_lock:
        try:
            with open(LOG_PATH, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass
    print(line, flush=True)


# ---------------------------------------------------------------- crypto ---

def sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def merkle_root(leaves):
    """leaves: list of txid bytes in INTERNAL byte order. Returns root bytes."""
    if not leaves:
        return b"\x00" * 32
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [sha256d(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


def merkle_branch(txids):
    """Stratum merkle branch for a coinbase at index 0.

    txids: template txids (internal order bytes), coinbase excluded.
    Returns sibling hashes (internal order bytes) so that:
        root = cb_txid; for b in branch: root = sha256d(root + b)
    equals merkle_root([cb_txid] + txids).
    """
    branch = []
    layer = list(txids)
    while True:
        full_len = 1 + len(layer)  # +1 for the coinbase at index 0
        if full_len <= 1:
            break
        ext = list(layer)
        if full_len % 2 == 1:
            ext.append(ext[-1])  # bitcoin odd-layer duplication (full layer)
        branch.append(ext[0])     # sibling of the coinbase-side node
        rest = ext[1:]
        layer = [sha256d(rest[i] + rest[i + 1]) for i in range(0, len(rest), 2)]
    return branch


def compact_to_target(bits: int) -> int:
    size = bits >> 24
    word = bits & 0x007FFFFF
    return word << (8 * (size - 3))


# diff1 target (Bitcoin): 0x00000000FFFF0000...0000
DIFF1_TARGET = 0x00000000FFFF0000000000000000000000000000000000000000000000000000


def diff_to_target(diff: float) -> int:
    if diff <= 0:
        diff = 1
    return DIFF1_TARGET // int(diff)


def hash_le_int(h: bytes) -> int:
    """Block/share hash as little-endian integer for target comparison."""
    return int.from_bytes(h, "little")


# ------------------------------------------------------- address decode ---

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def _bech32_polymod(vals):
    GEN = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in vals:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= GEN[i]
    return chk


def _bech32_decode(addr: str):
    """Decode bech32/bech32m. Returns (hrp, version, program5, polymod).

    The caller validates the polymod: 1 for bech32 (witness v0),
    0x2bc830a3 for bech32m (witness v1+)."""
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1:
        raise ValueError("bad bech32")
    hrp, data = addr[:pos], addr[pos + 1:]
    vals = [_BECH32_CHARSET.index(c) for c in data]
    chk = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    return hrp, vals[0], vals[1:-6], _bech32_polymod(chk + vals)


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    out = []
    maxv = (1 << tobits) - 1
    for v in data:
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad and bits:
        out.append((acc << (tobits - bits)) & maxv)
    return bytes(out)


def btc_address_to_spk(addr: str) -> bytes:
    """Bitcoin address -> scriptPubKey.

    Mainnet only: P2PKH, P2SH, P2WPKH/P2WSH (bech32) and P2TR (bech32m).
    Testnet/regtest bech32 (tb1..., bcrt1...) is rejected explicitly:
    a testnet "miner" would otherwise accrue a mainnet-unpayable balance
    whose payout refund-loops forever (fix-program scope I)."""
    a = addr.strip()
    low = a.lower()
    if low.startswith("tb1") or low.startswith("bcrt1"):
        raise ValueError("testnet/regtest address not accepted")
    if low.startswith("bc1q") or low.startswith("bc1p"):
        hrp, ver, prog5, pm = _bech32_decode(a)
        if hrp != "bc":
            raise ValueError("not a mainnet bech32 address")
        prog = _convertbits(prog5, 5, 8, False)
        if ver == 0:
            if pm != 1:
                raise ValueError("bad bech32 checksum")
            if len(prog) == 20:
                return b"\x00\x14" + prog          # P2WPKH
            if len(prog) == 32:
                return b"\x00\x20" + prog          # P2WSH
            raise ValueError("bad witness program length")
        if ver == 1:
            if pm != 0x2BC830A3:
                raise ValueError("bad bech32m checksum")
            if len(prog) != 32:
                raise ValueError("bad taproot program length")
            return b"\x51\x20" + prog              # P2TR (OP_1 <32B>)
        raise ValueError("unsupported witness version %d" % ver)
    raw = _b58decode(a)
    if len(raw) != 25:
        raise ValueError("bad base58 address length")
    body, cksum = raw[:21], raw[21:]
    if sha256d(body)[:4] != cksum:
        raise ValueError("bad base58 checksum")
    ver, h160 = body[0], body[1:]
    if ver == 0x00:
        return b"\x76\xa9\x14" + h160 + b"\x88\xac"   # P2PKH
    if ver == 0x05:
        return b"\xa9\x14" + h160 + b"\x87"           # P2SH
    raise ValueError("unsupported address version 0x%02x" % ver)


def valid_btc_address(addr: str) -> bool:
    try:
        btc_address_to_spk(addr)
        return True
    except Exception:
        return False


def valid_shit_address(addr: str) -> bool:
    """Bech32-validate a SHIT payout address (fix-program scope E).

    The chain's user-facing HRP is "shit" (shit1q...); the Syscoin-heritage
    "sys" HRP (sys1q..., used by node1's mine.py wallets) is also accepted.
    Witness v0 only, 20- or 32-byte program, valid bech32 checksum."""
    try:
        a = (addr or "").strip()
        hrp, ver, prog5, pm = _bech32_decode(a)
        if hrp not in ("shit", "sys"):
            return False
        if ver != 0 or pm != 1:
            return False
        prog = _convertbits(prog5, 5, 8, False)
        return len(prog) in (20, 32)
    except Exception:
        return False


# Jodie's P2SH tax output scriptPubKey (OP_HASH160 <20B> OP_EQUAL).
# Address 3FPSdy4dtNpMJxEnrhXMBEm3akEUY3Z592. DO NOT CHANGE.
TEAM_BTC_SPK = bytes.fromhex("a914963ee76ef44ebb777e435b352314d35368684de887")

# ------------------------------------------------------------------- RPC ---


class RPCError(Exception):
    pass


class RPC:
    # Methods that are safe to retry: pure reads with no side effects.
    # submitblock / sendtoaddress are deliberately NOT in this set: they
    # keep single-attempt behaviour at this layer. Block submission gets
    # its own persistence + verify loop at the call site (scope B), and
    # payout sends go through the payout state machine (scope C) — a blind
    # transport retry of either could double-submit / double-pay.
    RETRYABLE = frozenset(["getblockchaininfo", "getblocktemplate",
                           "getblockcount", "getblockhash", "getblockheader",
                           "listtransactions", "gettransaction"])

    def __init__(self, host, port, user=None, password=None, cookie_path=None,
                 wallet=None, timeout=30, retries=0, retry_delay=2.0):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.cookie_path = cookie_path
        self.wallet = wallet
        self.timeout = timeout
        self.retries = int(retries)
        self.retry_delay = float(retry_delay)

    def _auth(self):
        if self.cookie_path:
            with open(self.cookie_path) as f:
                cred = f.read().strip()
            return "Basic " + base64.b64encode(cred.encode()).decode()
        if self.user:
            cred = "%s:%s" % (self.user, self.password or "")
            return "Basic " + base64.b64encode(cred.encode()).decode()
        return None

    def call(self, method, params=None):
        attempts = 1 + (self.retries if method in self.RETRYABLE else 0)
        delay = self.retry_delay
        for attempt in range(attempts):
            try:
                return self._call_once(method, params)
            except RPCError:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(delay)
                delay *= 2

    def _call_once(self, method, params=None):
        body = json.dumps({"jsonrpc": "1.0", "id": "pool",
                           "method": method,
                           "params": params or []}).encode()
        url = "http://%s:%d/" % (self.host, self.port)
        if self.wallet:
            url += "wallet/%s" % self.wallet
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        auth = self._auth()
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read())
        except Exception as e:
            raise RPCError("%s:%d %s failed: %r" % (self.host, self.port, method, e))
        if out.get("error"):
            raise RPCError("%s error: %s" % (method, out["error"]))
        return out.get("result")

# ------------------------------------------------------------------ database ---

DB_PATH = os.path.join(DATA_DIR, "pool.db")
_db_lock = threading.Lock()


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def db_init():
    with _db_lock:
        con = db()
        con.executescript("""
        CREATE TABLE IF NOT EXISTS shares(
            id INTEGER PRIMARY KEY, miner TEXT, worker TEXT,
            diff REAL, job TEXT, t INTEGER);
        CREATE INDEX IF NOT EXISTS idx_shares_t ON shares(t);
        CREATE INDEX IF NOT EXISTS idx_shares_miner ON shares(miner);
        CREATE TABLE IF NOT EXISTS blocks(
            chain TEXT, height INTEGER, hash TEXT, reward INTEGER,
            finder TEXT, t INTEGER, PRIMARY KEY(chain, height));
        CREATE TABLE IF NOT EXISTS balances(
            miner TEXT, chain TEXT, pending INTEGER, paid INTEGER,
            PRIMARY KEY(miner, chain));
        CREATE TABLE IF NOT EXISTS payouts(
            id INTEGER PRIMARY KEY, miner TEXT, chain TEXT, amount INTEGER,
            address TEXT, txid TEXT, status TEXT, t INTEGER);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS workers(
            miner TEXT, worker TEXT, last_seen INTEGER,
            PRIMARY KEY(miner, worker));
        """)
        # migration (fix-program scope C): payouts gain a dedicated error
        # column so failure text no longer overwrites the txid column.
        cols = [r[1] for r in con.execute("PRAGMA table_info(payouts)")]
        if cols and "error" not in cols:
            con.execute("ALTER TABLE payouts ADD COLUMN error TEXT")
        con.commit()
        con.close()


def kv_get(k, default=None):
    with _db_lock:
        con = db()
        row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        con.close()
        return row[0] if row else default


def kv_set(k, v):
    """Write a kv row only when the value actually changes (scope K):
    re-authorize storms must not amplify into sqlite writes."""
    with _db_lock:
        con = db()
        row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        if row is not None and row[0] == v:
            con.close()
            return False
        con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v))
        con.commit()
        con.close()
        return True


def record_share(miner, worker, diff, job_id):
    now = int(time.time())
    with _db_lock:
        con = db()
        con.execute("INSERT INTO shares(miner,worker,diff,job,t) VALUES(?,?,?,?,?)",
                    (miner, worker, diff, job_id, now))
        con.execute("INSERT OR REPLACE INTO workers(miner,worker,last_seen) VALUES(?,?,?)",
                    (miner, worker, now))
        # prune shares older than 7 days (keeps the 1GB box light)
        con.execute("DELETE FROM shares WHERE t < ?", (now - 7 * 86400,))
        con.commit()
        con.close()


def pool_stats():
    """Aggregate stats for the dashboard."""
    now = int(time.time())
    with _db_lock:
        con = db()
        miners = con.execute(
            "SELECT COUNT(DISTINCT miner) FROM workers WHERE last_seen > ?",
            (now - 600,)).fetchone()[0]
        workers = con.execute(
            "SELECT COUNT(*) FROM workers WHERE last_seen > ?",
            (now - 600,)).fetchone()[0]
        # pool hashrate from last 10 min of shares: sum(diff)*2^32/600
        dsum = con.execute(
            "SELECT COALESCE(SUM(CASE WHEN diff > 0 THEN diff ELSE 0 END),0) FROM shares WHERE t > ?",
            (now - 600,)).fetchone()[0]
        hashrate = dsum * 4294967296 / 600 if dsum else 0
        blocks24 = con.execute(
            "SELECT chain, COUNT(*) FROM blocks WHERE t > ? GROUP BY chain",
            (now - 86400,)).fetchall()
        blocks_all = con.execute(
            "SELECT chain, COUNT(*) FROM blocks GROUP BY chain").fetchall()
        last_block = con.execute(
            "SELECT chain, height, t FROM blocks ORDER BY t DESC LIMIT 1").fetchone()
        con.close()
    return {"miners_online": miners, "workers_online": workers,
            "hashrate_hps": hashrate,
            "blocks_24h": dict(blocks24), "blocks_all": dict(blocks_all),
            "last_block": last_block}


def miner_stats(miner):
    now = int(time.time())
    with _db_lock:
        con = db()
        d24 = con.execute(
            "SELECT COALESCE(SUM(CASE WHEN diff > 0 THEN diff ELSE 0 END),0), COUNT(*) FROM shares "
            "WHERE miner=? AND t > ?", (miner, now - 86400)).fetchone()
        d1 = con.execute(
            "SELECT COALESCE(SUM(CASE WHEN diff > 0 THEN diff ELSE 0 END),0) FROM shares WHERE miner=? AND t > ?",
            (miner, now - 3600)).fetchone()[0]
        rej = con.execute(
            "SELECT COUNT(*) FROM shares WHERE miner=? AND diff < 0 AND t > ?",
            (miner, now - 86400)).fetchone()[0]
        wk = con.execute(
            "SELECT worker, last_seen FROM workers WHERE miner=?", (miner,)).fetchall()
        bal = con.execute(
            "SELECT chain, pending, paid FROM balances WHERE miner=?", (miner,)).fetchall()
        pay = con.execute(
            "SELECT t, chain, amount, txid, status FROM payouts WHERE miner=? "
            "ORDER BY t DESC LIMIT 20", (miner,)).fetchall()
        # Dashboard presentation only (benchmark R5): last-share time is
        # already collected in shares.t — a read-only aggregate, no new
        # data collection and no accounting change.
        last_share = con.execute(
            "SELECT COALESCE(MAX(t),0) FROM shares WHERE miner=?",
            (miner,)).fetchone()[0]
        # NOTE (Squad 6): was kv_get(...) here -> self-deadlock: kv_get
        # takes _db_lock, which this function already holds (threading.Lock
        # is not reentrant). Use the open cursor instead.
        _r = con.execute("SELECT v FROM kv WHERE k=?",
                         ("shitaddr:" + miner,)).fetchone()
        shit_addr = _r[0] if _r else None
        con.close()
    return {"diff_24h": d24[0], "shares_24h": d24[1], "hashrate_1h": d1 * 4294967296 / 3600,
            "rejected_24h": rej, "workers": wk, "balances": bal,
            "payouts": pay, "shit_address": shit_addr,
            "last_share": last_share}


# --------------------------------------------------------------- aux chains ---

MM_MAGIC = bytes.fromhex("fabe6d6d")  # merged-mining magic in parent coinbase


class AuxChain:
    """One merge-mined aux chain."""
    name = "AUX"
    fee = 0.01  # pool take at accounting time

    def get_template(self):
        """Return dict(hash_hex, bits, target, height) or None if unavailable."""
        raise NotImplementedError

    def submit(self, job, parent_coinbase, parent_header):
        """Submit a solved aux block. Returns True on acceptance."""
        raise NotImplementedError


# --- SHIT consensus constants + helpers, ported from node1 mine.py ---
# Sources (node1 /home/shitcoin/mine.py, read-only fetch 2026-10-14):
#   subsidy constants mine.py:44-45, get_subsidy mine.py:356-366,
#   target_to_compact mine.py:343-354, retarget_interval mine.py:93-100,
#   difficulty constants mine.py:75-90. Kept in lockstep with pow.cpp via
#   mine.py; if mine.py changes, this port must change with it.
SHIT_MINING_SUBSIDY_BASE = 19807185401
SHIT_SUBSIDY_HALVING_INTERVAL = 1051200
SHIT_GENESIS_BITS = 0x1E0FFFFF
SHIT_DIFF_FORK_HEIGHT = 29196
SHIT_DIFF_REBASE_BITS = 0x1D03FFFC
SHIT_DIFF_K = 48
SHIT_DIFF_FORK3_HEIGHT = 48460
SHIT_DIFF_FLOOR_BITS = 0x1D63FFFF
SHIT_DIFF_FORK2_HEIGHT = 30950
SHIT_DIFF_FORK2_REBASE_BITS = 0x1D63FFFF
SHIT_POW_TARGET_TIMESPAN = 21600
SHIT_NEVM_START_BLOCK = 30316
# block-1 launch allocations, satoshis (mine.py:39-42: coins * COIN)
SHIT_ALLOC_BLOCK1 = (3780000000 + 2940000000 + 420000000
                     + 1260000000) * 100000000


def shit_target_to_compact(target):
    # minimal port of arith_uint256::GetCompact (mine.py target_to_compact)
    size = (target.bit_length() + 7) // 8
    if size <= 3:
        word = target << (8 * (3 - size))
    else:
        word = target >> (8 * (size - 3))
    if word & 0x00800000:
        word >>= 8
        size += 1
    return (size << 24) | (word & 0x007FFFFF)


def shit_get_subsidy(height):
    """SHIT block subsidy in satoshis. Verbatim port of mine.py get_subsidy
    (mine.py:356-366). Used only as the logged fallback when syscoind
    getblocktemplate cannot supply coinbasevalue (scope F)."""
    if height == 1:
        return SHIT_ALLOC_BLOCK1
    n = SHIT_MINING_SUBSIDY_BASE
    reductions = height // SHIT_SUBSIDY_HALVING_INTERVAL
    if reductions >= 100:
        return 0
    for _ in range(reductions):
        n -= n // 50
    superblock = int(n * 0.1)  # matches C++ (CAmount)(nSubsidy*0.1)
    return n - superblock


def shit_retarget_interval(height):
    # mine.py retarget_interval (mine.py:93-100)
    if height >= SHIT_NEVM_START_BLOCK:
        return 720
    return 1800


class ShitAuxChain(AuxChain):
    """ShitcoinBTC aux chain via local syscoind.

    NOTE: every SHIT block post-NEVM-flag must carry a valid NEVM commitment,
    which requires the paired sysgeth ZMQ feed. That feed lives on node1 and is
    NOT reachable from node2, so SHIT aux stays in 'waiting' until a NEVM feed
    is configured. The pool mines BTC-only jobs meanwhile (valid blocks).
    """
    name = "SHIT"
    fee = 0.01

    def __init__(self, rpc, nevm_zmq_addr):
        self.rpc = rpc
        self.nevm_zmq_addr = nevm_zmq_addr
        self._nevm_ok = None

    def _nevm_template(self):
        """Fetch (header96, rlp_block) from sysgeth ZMQ. None if unavailable."""
        try:
            import zmq
        except ImportError:
            return None
        try:
            ctx = zmq.Context.instance()
            sock = ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.RCVTIMEO, 5000)
            sock.setsockopt(zmq.SNDTIMEO, 5000)
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(self.nevm_zmq_addr)
            try:
                sock.send_multipart([b"nevmblock", b""])
                parts = sock.recv_multipart()
            finally:
                sock.close()
            if len(parts) < 4 or parts[0] != b"nevmblock" or len(parts[3]) < 96:
                return None
            payload = parts[3]
            header96 = payload[0:96]
            off = 96
            b = payload[off]
            if b < 0xFD:
                rlp_len, vsz = b, 1
            elif b == 0xFD:
                rlp_len, vsz = struct.unpack_from("<H", payload, off + 1)[0], 3
            elif b == 0xFE:
                rlp_len, vsz = struct.unpack_from("<I", payload, off + 1)[0], 5
            else:
                rlp_len, vsz = struct.unpack_from("<Q", payload, off + 1)[0], 9
            rlp_block = payload[off + vsz:off + vsz + rlp_len]
            if len(rlp_block) != rlp_len:
                return None
            return header96, rlp_block
        except Exception:
            return None

    def get_template(self):
        # Gate 1: NEVM feed must be reachable (post-flag consensus requirement).
        nevm = self._nevm_template()
        if nevm is None:
            return None
        header96, rlp_block = nevm
        try:
            height = self.rpc.call("getblockcount") + 1
            tip_hash = self.rpc.call("getblockhash", [height - 1])
            tip = self.rpc.call("getblockheader", [tip_hash])
        except RPCError:
            return None
        bits = self._bits_for_height(height)
        # coinbase-only aux block; coinbase pays the pool's SHIT wallet.
        shit_addr = kv_get("pool_shit_address")
        if not shit_addr:
            try:
                shit_addr = self.rpc.call("getnewaddress")
                kv_set("pool_shit_address", shit_addr)
                log("SHIT pool wallet address: %s" % shit_addr)
            except RPCError:
                return None
        cb, subsidy = self._build_coinbase(height, shit_addr, header96)
        cb_txid = sha256d(cb)  # no witness in txid pre-witness... see below
        # NOTE: coinbase carries witness (NEVM commitment output), so the
        # block-tx list uses the witness-stripped txid for the merkle root.
        cb_stripped = self._strip_witness(cb)
        root = merkle_root([sha256d(cb_stripped)])
        ntime = int(time.time())
        # Version: exactly mine.py BLOCK_VERSION_NEVM (0x20100180, which
        # already includes VERSION_AUXPOW 0x100). The old extra |0x1000 bit
        # was an unproven deviation flagged in TUNNEL-EVAL §5.5 — removed.
        header = (struct.pack("<i", 0x20100180)
                  + bytes.fromhex(tip_hash)[::-1] + root
                  + struct.pack("<III", ntime, bits, 0))
        aux_hash_hex = sha256d(header)[::-1].hex()  # display order, like mine.py
        # coinbasevalue MUST travel with the template (scope F / audit B-1):
        # submit_aux_block credits exactly this amount. Previously the key
        # was absent and every SHIT block credited 0.
        return {"hash_hex": aux_hash_hex, "bits": bits,
                "target": compact_to_target(bits), "height": height,
                "header": header, "coinbase": cb, "rlp_block": rlp_block,
                "coinbasevalue": subsidy, "tip_hash": tip_hash}

    def _bits_for_height(self, height):
        """VERBATIM port of node1 mine.py bits_for_height (mine.py:734-795),
        including the exponential per-block controller (K=48), the fork-1
        and fork-2 one-time rebases, and the fork-3 difficulty floor
        0x1d63ffff for heights >= 48,460. The previous simplified linear
        rule agreed with consensus only at dt == spacing exactly and had
        no floor (TUNNEL-EVAL §5.1). RPC access is adapted to this class's
        client; the arithmetic is unchanged."""
        rpc = lambda method, *args: self.rpc.call(method, list(args))
        if height == 1:
            return SHIT_GENESIS_BITS
        if height == SHIT_DIFF_FORK2_HEIGHT:
            return SHIT_DIFF_FORK2_REBASE_BITS
        if height > SHIT_DIFF_FORK2_HEIGHT:
            tip = rpc("getblockheader", rpc("getblockhash", height - 1))
            parent = rpc("getblockheader", rpc("getblockhash", height - 2))
            spacing = 30 if height >= SHIT_NEVM_START_BLOCK else 12
            dt = tip["time"] - parent["time"]
            dt = max(spacing // 4, min(spacing * 4, dt))
            u = dt - spacing
            t = compact_to_target(int(tip["bits"], 16))
            if u > 0:
                for _ in range(u):
                    t = t * (SHIT_DIFF_K + 1) // SHIT_DIFF_K
            elif u < 0:
                for _ in range(-u):
                    t = t * SHIT_DIFF_K // (SHIT_DIFF_K + 1)
            if t < 1:
                t = 1
            # Fork #3: difficulty floor - never easier than CPU-mineable.
            if height >= SHIT_DIFF_FORK3_HEIGHT:
                t = min(t, compact_to_target(SHIT_DIFF_FLOOR_BITS))
            limit = compact_to_target(SHIT_GENESIS_BITS)
            return shit_target_to_compact(min(t, limit))
        if height == SHIT_DIFF_FORK_HEIGHT:
            # One-time rebase: clear the stranded post-attack difficulty.
            return SHIT_DIFF_REBASE_BITS
        if height > SHIT_DIFF_FORK_HEIGHT:
            # Per-block controller (mirrors src/pow.cpp exactly):
            # target *= ((K+1)/K)^(dt-spacing), dt clamped to [spacing/4, spacing*4].
            tip = rpc("getblockheader", rpc("getblockhash", height - 1))
            parent = rpc("getblockheader", rpc("getblockhash", height - 2))
            spacing = 30 if height >= SHIT_NEVM_START_BLOCK else 12
            dt = tip["time"] - parent["time"]
            dt = max(spacing // 4, min(spacing * 4, dt))
            u = dt - spacing
            t = compact_to_target(int(tip["bits"], 16))
            if u > 0:
                for _ in range(u):
                    t = t * (SHIT_DIFF_K + 1) // SHIT_DIFF_K
            elif u < 0:
                for _ in range(-u):
                    t = t * SHIT_DIFF_K // (SHIT_DIFF_K + 1)
            if t < 1:
                t = 1
            limit = compact_to_target(SHIT_GENESIS_BITS)
            return shit_target_to_compact(min(t, limit))
        tip = rpc("getblockheader", rpc("getblockhash", height - 1))
        interval = shit_retarget_interval(height)
        if height % interval != 0:
            return int(tip["bits"], 16)
        # Retarget: replicate CalculateNextWorkRequired.
        first = rpc("getblockheader", rpc("getblockhash", height - interval))
        timespan = tip["time"] - first["time"]
        timespan = max(SHIT_POW_TARGET_TIMESPAN // 4,
                       min(SHIT_POW_TARGET_TIMESPAN * 4, timespan))
        new_target = compact_to_target(int(tip["bits"], 16))
        new_target = new_target * timespan // SHIT_POW_TARGET_TIMESPAN
        limit = compact_to_target(SHIT_GENESIS_BITS)
        return shit_target_to_compact(min(new_target, limit))

    def _shit_spk(self, addr):
        # shit1q... bech32 -> P2WPKH scriptPubKey (mine.py addr_to_scriptpubkey)
        a = addr.lower()
        pos = a.rfind("1")
        hrp, data = a[:pos], a[pos + 1:]
        if hrp not in ("shit", "sys"):
            raise ValueError("unexpected HRP %s" % hrp)
        vals = [_BECH32_CHARSET.index(c) for c in data]
        chk = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
        if _bech32_polymod(chk + vals) != 1:
            raise ValueError("bad bech32 checksum")
        prog = _convertbits(vals[1:-6], 5, 8, False)
        return b"\x00\x14" + prog

    def _build_coinbase(self, height, shit_addr, header96):
        # coinbase: [pool reward] + [witness commitment w/ NEVM header].
        # Returns (coinbase_bytes, subsidy_sats). Subsidy comes from the
        # chain first (syscoind getblocktemplate coinbasevalue, scope F).
        # NOTE: on this fork GBT currently always errors with no-auxpow
        # (TUNNEL-EVAL §2), so the fallback is the operative path: the
        # consensus subsidy schedule ported from mine.py — NOT the old
        # hardcoded 178.26 SHIT constant, which over-claims (invalid
        # blocks) once the decaying subsidy falls below it (TUNNEL-EVAL
        # §5.4). The fallback is always logged.
        subsidy = None
        try:
            tmpl = self.rpc.call("getblocktemplate", [{"rules": ["segwit"]}])
            if isinstance(tmpl, dict):
                subsidy = tmpl.get("coinbasevalue")
        except RPCError:
            subsidy = None
        if not subsidy:
            subsidy = shit_get_subsidy(height)
            log("SHIT subsidy: getblocktemplate unavailable, using "
                "schedule subsidy %d sats at height %d" % (subsidy, height))
        spk = self._shit_spk(shit_addr)
        nevm_extra = b"nevm" + header96  # 100 bytes
        wtxids = [b"\x00" * 32]
        commitment = sha256d(merkle_root(wtxids) + b"\x00" * 32)
        wit_spk = b"\x6a\x24\xaa\x21\xa9\xed" + commitment
        # push the 100-byte NEVM extra after the commitment (C++ style 4c 64)
        wit_spk += b"\x4c\x64" + nevm_extra
        s = struct.pack("<i", 1) + b"\x00\x01" + b"\x01" + b"\x00" * 32
        s += struct.pack("<I", 0xFFFFFFFF)
        scriptsig = bytes([3]) + struct.pack("<I", height)[:3] + b"\x00"
        s += bytes([len(scriptsig)]) + scriptsig + struct.pack("<I", 0xFFFFFFFF)
        s += b"\x02"
        s += struct.pack("<q", subsidy) + bytes([len(spk)]) + spk
        s += struct.pack("<q", 0) + bytes([len(wit_spk)]) + wit_spk
        s += b"\x01\x20" + b"\x00" * 32   # witness: 32-byte nonce
        s += struct.pack("<I", 0)
        return s, subsidy

    def _strip_witness(self, cb):
        # remove segwit marker/flag + witness stack -> txid preimage
        assert cb[4:6] == b"\x00\x01"
        body = cb[0:4] + cb[6:]
        # witness stack sits right before locktime: 01 20 <32B>
        assert body[-38:-36] == b"\x01\x20"
        return body[:-38] + body[-4:]

    def submit(self, job, parent_coinbase, parent_header):
        tmpl = job.aux.get(self.name)
        if not tmpl:
            return False
        parent_hash = sha256d(parent_header)
        # auxpow layout mirrors mine.py exactly:
        # tx | parentHash | vchCoinBaseMerkleBranch(0) | nIdx | vchChainBranch | nChainIdx | parentHeader
        chain_branch, chain_idx = job.aux_branch(self.name)
        auxpow = (parent_coinbase + parent_hash + b"\x00" + struct.pack("<I", 0)
                  + varint(len(chain_branch)) + b"".join(chain_branch)
                  + struct.pack("<I", chain_idx) + parent_header)
        block = (tmpl["header"] + auxpow + varint(1) + tmpl["coinbase"]
                 + varint(len(tmpl["rlp_block"])) + tmpl["rlp_block"])
        # Acceptance is verified, never assumed (scope B / TUNNEL-EVAL
        # §5.5): a non-empty submitblock result is a rejection string, and
        # even a null result only counts once the block is visible at its
        # height. Previously ANY non-exception return meant "accepted"
        # and the block was credited (at 0, via the missing coinbasevalue).
        height = tmpl.get("height", 0)
        want = tmpl.get("hash_hex")
        try:
            res = self.rpc.call("submitblock", [block.hex()])
        except RPCError as e:
            # Ambiguous: the daemon may have accepted before the reply was
            # lost. Credit only if the block is actually visible.
            if self._visible(height, want):
                log("SHIT submitblock reply lost but block %d visible — "
                    "counted as accepted" % height)
                return True
            log("SHIT submitblock failed: %s" % e)
            return False
        if res not in (None, ""):
            if self._visible(height, want):
                return True   # e.g. duplicate of our own earlier submit
            log("SHIT submitblock REJECTED: %r — not credited" % (res,))
            return False
        for _ in range(3):
            if self._visible(height, want):
                return True
            time.sleep(1)
        log("SHIT submitblock returned success but block %d not visible "
            "— not credited" % height)
        return False

    def _visible(self, height, want_hash_hex):
        if not height or not want_hash_hex:
            return False
        try:
            return self.rpc.call("getblockhash", [height]) == want_hash_hex
        except RPCError:
            return False


class NmcAuxChain(AuxChain):
    """Namecoin aux chain. Enabled via NMC_ENABLED once namecoind exists."""
    name = "NMC"
    fee = 0.01

    def __init__(self, rpc):
        self.rpc = rpc

    def get_template(self):
        try:
            t = self.rpc.call("getauxblock")
        except RPCError as e:
            log("NMC getauxblock failed: %s" % e)
            return None
        return {"hash_hex": t["hash"], "bits": int(t["bits"], 16),
                "target": compact_to_target(int(t["bits"], 16)),
                "height": t.get("height", 0), "_raw": t}

    def submit(self, job, parent_coinbase, parent_header):
        tmpl = job.aux.get(self.name)
        if not tmpl:
            return False
        parent_hash = sha256d(parent_header)
        chain_branch, chain_idx = job.aux_branch(self.name)
        auxpow = (parent_coinbase + parent_hash + b"\x00" + struct.pack("<I", 0)
                  + varint(len(chain_branch)) + b"".join(chain_branch)
                  + struct.pack("<I", chain_idx) + parent_header)
        try:
            ok = self.rpc.call("getauxblock", [tmpl["hash_hex"], auxpow.hex()])
            return bool(ok)
        except RPCError as e:
            log("NMC submit failed: %s" % e)
            return False


class FbAuxChain(AuxChain):
    """Fractal Bitcoin aux chain. createauxblock requires a payout address."""
    name = "FB"
    fee = 0.01

    def __init__(self, rpc, payout_address):
        self.rpc = rpc
        self.payout_address = payout_address

    def get_template(self):
        if not self.payout_address:
            return None
        try:
            t = self.rpc.call("createauxblock", [self.payout_address])
        except RPCError as e:
            log("FB createauxblock failed: %s" % e)
            return None
        return {"hash_hex": t["hash"], "bits": int(t["bits"], 16),
                "target": compact_to_target(int(t["bits"], 16)),
                "height": t.get("height", 0), "_raw": t}

    def submit(self, job, parent_coinbase, parent_header):
        tmpl = job.aux.get(self.name)
        if not tmpl:
            return False
        parent_hash = sha256d(parent_header)
        chain_branch, chain_idx = job.aux_branch(self.name)
        auxpow = (parent_coinbase + parent_hash + b"\x00" + struct.pack("<I", 0)
                  + varint(len(chain_branch)) + b"".join(chain_branch)
                  + struct.pack("<I", chain_idx) + parent_header)
        try:
            ok = self.rpc.call("submitauxblock", [tmpl["hash_hex"], auxpow.hex()])
            return bool(ok)
        except RPCError as e:
            log("FB submit failed: %s" % e)
            return False


class ElaAuxChain(AuxChain):
    """Elastos ELA aux chain. JSON-RPC 2.0; createauxblock needs paytoaddress."""
    name = "ELA"
    fee = 0.01

    def __init__(self, rpc, payout_address):
        self.rpc = rpc
        self.payout_address = payout_address

    def get_template(self):
        if not self.payout_address:
            return None
        try:
            t = self.rpc.call("createauxblock", {"paytoaddress": self.payout_address})
        except RPCError as e:
            log("ELA createauxblock failed: %s" % e)
            return None
        if not t:
            return None
        return {"hash_hex": t["hash"], "bits": int(t["bits"], 16),
                "target": compact_to_target(int(t["bits"], 16)),
                "height": t.get("height", 0), "_raw": t}

    def submit(self, job, parent_coinbase, parent_header):
        tmpl = job.aux.get(self.name)
        if not tmpl:
            return False
        parent_hash = sha256d(parent_header)
        chain_branch, chain_idx = job.aux_branch(self.name)
        auxpow = (parent_coinbase + parent_hash + b"\x00" + struct.pack("<I", 0)
                  + varint(len(chain_branch)) + b"".join(chain_branch)
                  + struct.pack("<I", chain_idx) + parent_header)
        try:
            ok = self.rpc.call("submitauxblock",
                               {"blockhash": tmpl["hash_hex"], "auxpow": auxpow.hex()})
            return bool(ok)
        except RPCError as e:
            log("ELA submit failed: %s" % e)
            return False


class SysAuxChain(AuxChain):
    """Syscoin aux chain (chainid 16). UTXO-side merged mining via createauxblock."""
    name = "SYS"
    fee = 0.01

    def __init__(self, rpc, payout_address):
        self.rpc = rpc
        self.payout_address = payout_address

    def get_template(self):
        if not self.payout_address:
            return None
        try:
            t = self.rpc.call("createauxblock", [self.payout_address])
        except RPCError as e:
            log("SYS createauxblock failed: %s" % e)
            return None
        return {"hash_hex": t["hash"], "bits": int(t["bits"], 16),
                "target": compact_to_target(int(t["bits"], 16)),
                "height": t.get("height", 0), "_raw": t}

    def submit(self, job, parent_coinbase, parent_header):
        tmpl = job.aux.get(self.name)
        if not tmpl:
            return False
        parent_hash = sha256d(parent_header)
        chain_branch, chain_idx = job.aux_branch(self.name)
        auxpow = (parent_coinbase + parent_hash + b"\x00" + struct.pack("<I", 0)
                  + varint(len(chain_branch)) + b"".join(chain_branch)
                  + struct.pack("<I", chain_idx) + parent_header)
        try:
            ok = self.rpc.call("submitauxblock", [tmpl["hash_hex"], auxpow.hex()])
            return bool(ok)
        except RPCError as e:
            log("SYS submit failed: %s" % e)
            return False


class UnoAuxChain(AuxChain):
    """Unobtanium aux chain. Standard Namecoin-style AuxPoW via getauxblock."""
    name = "UNO"
    fee = 0.01

    def __init__(self, rpc):
        self.rpc = rpc

    def get_template(self):
        try:
            t = self.rpc.call("getauxblock")
        except RPCError as e:
            log("UNO getauxblock failed: %s" % e)
            return None
        return {"hash_hex": t["hash"], "bits": int(t["bits"], 16),
                "target": compact_to_target(int(t["bits"], 16)),
                "height": t.get("height", 0), "_raw": t}

    def submit(self, job, parent_coinbase, parent_header):
        tmpl = job.aux.get(self.name)
        if not tmpl:
            return False
        parent_hash = sha256d(parent_header)
        chain_branch, chain_idx = job.aux_branch(self.name)
        auxpow = (parent_coinbase + parent_hash + b"\x00" + struct.pack("<I", 0)
                  + varint(len(chain_branch)) + b"".join(chain_branch)
                  + struct.pack("<I", chain_idx) + parent_header)
        try:
            ok = self.rpc.call("getauxblock", [tmpl["hash_hex"], auxpow.hex()])
            return bool(ok)
        except RPCError as e:
            log("UNO submit failed: %s" % e)
            return False

# --------------------------------------------------------------- job build ---

class Job:
    """One stratum mining job: BTC template + aux merkle root + coinbase halves."""
    _counter = 0
    _counter_lock = threading.Lock()

    def __init__(self, tmpl, aux_templates, pool_spk, team_spk, tax_sats, tag):
        with Job._counter_lock:
            Job._counter += 1
            self.job_id = "%08x" % (Job._counter & 0xFFFFFFFF)
        self.created = time.time()
        self.height = tmpl["height"]
        self.prevhash_hex = tmpl["previousblockhash"]
        self.version = tmpl["version"]
        self.bits = int(tmpl["bits"], 16)
        self.target = compact_to_target(self.bits)
        self.curtime = tmpl["curtime"]
        self.coinbasevalue = tmpl["coinbasevalue"]
        self.tax_sats = tax_sats
        self.txs = tmpl.get("transactions", [])

        # --- aux merkle tree over aux block hashes (display-hex -> bytes) ---
        self.aux = {}          # name -> template dict
        self.aux_order = []    # ordered names for the tree
        for name, at in aux_templates:
            self.aux[name] = at
            self.aux_order.append(name)
        leaves = [bytes.fromhex(self.aux[n]["hash_hex"]) for n in self.aux_order]
        if leaves:
            self.aux_root = merkle_root(leaves)
            self.aux_count = len(leaves)
        else:
            self.aux_root = None
            self.aux_count = 0

        # --- coinbase ---
        # scriptSig: [BIP34 height] [tag] [extranonce1 4B] [extranonce2 4B] [merged-mining header]
        height_push = _push_int64(self.height)
        prefix = height_push + bytes([len(tag)]) + tag
        mm_header = b""
        if self.aux_root is not None:
            mm_header = (MM_MAGIC + self.aux_root
                         + struct.pack("<I", self.aux_count) + struct.pack("<I", 0))
        scriptsig_len = len(prefix) + 8 + len(mm_header)
        assert scriptsig_len <= 100, "coinbase scriptSig too long"
        self.coinb1 = (struct.pack("<i", 2) + b"\x01" + b"\x00" * 32
                       + struct.pack("<I", 0xFFFFFFFF)
                       + bytes([scriptsig_len]) + prefix)
        # witness commitment over template txs (coinbase wtxid = 32 zero bytes)
        wtxids = [b"\x00" * 32]
        tx_datas = []
        for tx in self.txs:
            raw = bytes.fromhex(tx["data"])
            tx_datas.append(raw)
            wtxids.append(sha256d(raw))
        self.tx_datas = tx_datas
        commitment = sha256d(merkle_root(wtxids) + b"\x00" * 32)
        wit_spk = b"\x6a\x24\xaa\x21\xa9\xed" + commitment
        pool_reward = self.coinbasevalue - tax_sats
        outputs = [
            (0, b"\x6a" + bytes([len(tag)]) + tag),
            (pool_reward, pool_spk),
            (tax_sats, team_spk),   # <-- Jodie's 0.002% tax output. NEVER remove.
            (0, wit_spk),
        ]
        out_bin = b""
        for amt, spk in outputs:
            out_bin += struct.pack("<q", amt) + bytes([len(spk)]) + spk
        self.coinb2 = (mm_header + struct.pack("<I", 0xFFFFFFFF)
                       + bytes([len(outputs)]) + out_bin + struct.pack("<I", 0))
        # merkle branch for stratum (template txids, internal order)
        self.merkle_branch = [h.hex() for h in
                              merkle_branch([bytes.fromhex(t["txid"])[::-1]
                                             for t in self.txs])]
        self.prevhash_stratum = bytes.fromhex(self.prevhash_hex)[::-1].hex()
        self.version_hex = struct.pack("<i", self.version).hex()
        self.nbits_hex = bytes.fromhex(tmpl["bits"]).hex()
        self.ntime_hex = "%08x" % self.curtime  # big-endian hex per stratum spec

    def aux_branch(self, name):
        """Merkle branch proving aux chain `name` leaf in the aux tree."""
        idx = self.aux_order.index(name)
        leaves = [bytes.fromhex(self.aux[n]["hash_hex"]) for n in self.aux_order]
        branch = []
        layer, i = list(leaves), idx
        while len(layer) > 1:
            if len(layer) % 2:
                layer.append(layer[-1])
            branch.append(layer[i ^ 1])
            i //= 2
            layer = [sha256d(layer[j] + layer[j + 1]) for j in range(0, len(layer), 2)]
        return branch, idx


def _push_int64(n):
    if 1 <= n <= 16:
        return bytes([n + 0x51 - 1])
    r = bytearray()
    m = n
    while m:
        r.append(m & 0xFF)
        m >>= 8
    if r[-1] & 0x80:
        r.append(0)
    return bytes([len(r)]) + bytes(r)


def btc_subsidy_sats(height):
    """Bitcoin block subsidy (no fees) in satoshis at a height."""
    halvings = height // 210000
    if halvings >= 64:
        return 0
    return 5000000000 >> halvings


class PoolState:
    """Owns chain state: polls bitcoind, builds jobs, tracks sync status."""
    def __init__(self, btc_rpc, shit_chain, nmc_chain, fb_chain=None,
                 ela_chain=None, sys_chain=None, uno_chain=None):
        self.btc = btc_rpc
        self.shit_chain = shit_chain
        self.nmc_chain = nmc_chain
        self.fb_chain = fb_chain
        self.ela_chain = ela_chain
        self.sys_chain = sys_chain
        self.uno_chain = uno_chain
        self.lock = threading.Lock()
        self.job = None
        self.bitcoind_state = "unknown"   # unknown|syncing|ready|down
        self.bitcoind_height = 0
        self.bitcoind_progress = 0.0
        # RPC resilience (scope A, design from cells/rpc FIX-PREP):
        self.consecutive_failures = 0     # failed poll cycles in a row
        self.last_good_poll = 0.0         # time of last full poll success
        self.job_time = 0.0               # time current job was adopted
        self._last_tip = -1               # tip height at last GBT fetch
        self._last_tmpl = None            # cached GBT result for that tip
        self._last_gbt_time = 0.0
        self._last_aux_fp = None          # aux fingerprint of current job
        self.shit_aux_state = "unknown"   # unknown|waiting|ready
        self.shit_aux_reason = ""
        self.pool_spk = btc_address_to_spk(CFG["POOL_BTC_ADDRESS"])
        self.team_spk = TEAM_BTC_SPK
        assert CFG["TAX_BTC_ADDRESS"] == "3FPSdy4dtNpMJxEnrhXMBEm3akEUY3Z592", \
            "tax address changed without Jodie's order - refusing to start"
        self.tag = CFG["POOL_TAG"].encode()[:32]

    def poll(self):
        """One template poll cycle. Never raises; on any RPC failure the
        last good job keeps being served (scope A)."""
        try:
            info = self.btc.call("getblockchaininfo")
        except RPCError as e:
            # Only declare "down" after 3 consecutive failed cycles so a
            # single slow response does not flap the dashboard/state.
            with self.lock:
                self.consecutive_failures += 1
                fails = self.consecutive_failures
                if fails >= 3:
                    self.bitcoind_state = "down"
            log("bitcoind unreachable (streak=%d): %s" % (fails, e))
            return False
        with self.lock:
            self.consecutive_failures = 0
            self.last_good_poll = time.time()
            self.bitcoind_height = info.get("blocks", 0)
            self.bitcoind_progress = info.get("verificationprogress", 0)
            if info.get("initialblockdownload"):
                self.bitcoind_state = "syncing"
                return False
            if self.bitcoind_progress < 0.999:
                self.bitcoind_state = "syncing"
                return False
            self.bitcoind_state = "ready"
        # Template fetch (scope A): getblocktemplate is requested only
        # when the tip changed (cheap getblockchaininfo gate above), when
        # there is no job yet, or on a 120 s refresh — every GBT makes
        # bitcoind build a full template, and polling it every 10 s was
        # part of the load that timed the pool out (DIAGNOSIS fix #2).
        tip = info.get("blocks", 0)
        need_gbt = (self.job is None or tip != self._last_tip
                    or time.time() - self._last_gbt_time > 120)
        if need_gbt:
            try:
                tmpl = self.btc.call("getblocktemplate",
                                     [{"rules": ["segwit"]}])
            except RPCError as e:
                with self.lock:
                    self.consecutive_failures += 1
                    fails = self.consecutive_failures
                log("getblocktemplate failed (streak=%d): %s" % (fails, e))
                return False
            # Template sanity (scope G): never serve a suspect job.
            try:
                tip_hash = self.btc.call("getblockhash", [tip])
            except RPCError as e:
                log("getblockhash failed during sanity check: %s" % e)
                return False
            problem = None
            if tmpl.get("height") != tip + 1:
                problem = "height %s != tip+1 %d" % (tmpl.get("height"),
                                                     tip + 1)
            elif tmpl.get("previousblockhash") != tip_hash:
                problem = "prevhash does not match getblockhash(tip)"
            else:
                cv = tmpl.get("coinbasevalue", 0)
                subsidy = btc_subsidy_sats(tip + 1)
                if cv < subsidy or cv > subsidy + 100000000:
                    problem = ("coinbasevalue %d outside [subsidy %d, "
                               "subsidy + 1 BTC fees]" % (cv, subsidy))
            if problem:
                log("template sanity FAILED at tip %d: %s — keeping "
                    "last good job, will retry next poll" % (tip, problem))
                return False
            self._last_tmpl = tmpl
            self._last_tip = tip
            self._last_gbt_time = time.time()
        else:
            tmpl = self._last_tmpl
        # aux templates (each chain decides; failures -> chain skipped)
        aux = []
        if self.shit_chain and CFG["SHIT_AUX_ENABLED"] == "true":
            try:
                st = self.shit_chain.get_template()
            except Exception as e:
                log("SHIT aux template error: %r" % e)
                st = None
            if st:
                aux.append(("SHIT", st))
                self._set_shit_state("ready", "")
            else:
                self._set_shit_state("waiting", "NEVM feed unreachable")
        if self.nmc_chain and CFG["NMC_ENABLED"] == "true":
            try:
                nt = self.nmc_chain.get_template()
            except Exception as e:
                log("NMC aux template error: %r" % e)
                nt = None
            if nt:
                aux.append(("NMC", nt))
        if self.fb_chain and CFG["FB_ENABLED"] == "true":
            try:
                ft = self.fb_chain.get_template()
            except Exception as e:
                log("FB aux template error: %r" % e)
                ft = None
            if ft:
                aux.append(("FB", ft))
        if self.ela_chain and CFG["ELA_ENABLED"] == "true":
            try:
                et = self.ela_chain.get_template()
            except Exception as e:
                log("ELA aux template error: %r" % e)
                et = None
            if et:
                aux.append(("ELA", et))
        if self.sys_chain and CFG["SYS_ENABLED"] == "true":
            try:
                st = self.sys_chain.get_template()
            except Exception as e:
                log("SYS aux template error: %r" % e)
                st = None
            if st:
                aux.append(("SYS", st))
        if self.uno_chain and CFG["UNO_ENABLED"] == "true":
            try:
                ut = self.uno_chain.get_template()
            except Exception as e:
                log("UNO aux template error: %r" % e)
                ut = None
            if ut:
                aux.append(("UNO", ut))
        # Job adoption (scope A + F): adopt a fresh job when the BTC
        # prevhash changed OR when any aux chain's tip/template advanced.
        # Previously adoption keyed on BTC prevhash only, so a SHIT
        # template inside the live job went stale within ~30 s while BTC
        # prevhash changed only every ~10 min (TUNNEL-EVAL §5.2) — most
        # pool SHIT work targeted a settled height. Same prevhash AND
        # same aux fingerprint => keep the old job: the job_id must stay
        # stable, or every submit older than one poll goes stale ([21]).
        aux_fp = tuple((name, at.get("height"), at.get("hash_hex"))
                       for name, at in aux)
        with self.lock:
            old = self.job
            adopt = (old is None
                     or old.prevhash_hex != tmpl.get("previousblockhash")
                     or aux_fp != self._last_aux_fp)
            if not adopt:
                return False
            tax_sats = tmpl.get("coinbasevalue", 312500000) * 2 // 100000
            job = Job(tmpl, aux, self.pool_spk, self.team_spk, tax_sats,
                      self.tag)
            self.job = job
            self.job_time = time.time()
            self._last_aux_fp = aux_fp
        log("new job %s height=%d aux=%s tax=%d sats" %
            (job.job_id, job.height,
             ",".join(job.aux_order) if job.aux_order else "none",
             tax_sats))
        return True   # clean_jobs notify

    def _set_shit_state(self, state, reason):
        # NOTE (Squad 6): log() moved out from under self.lock (see above).
        changed = False
        with self.lock:
            if self.shit_aux_state != state:
                changed = True
            self.shit_aux_state = state
            self.shit_aux_reason = reason
        if changed:
            log("SHIT aux %s: %s" % (state, reason))

    def get_job(self):
        with self.lock:
            return self.job

    def status(self):
        with self.lock:
            return {
                "bitcoind": {"state": self.bitcoind_state,
                             "height": self.bitcoind_height,
                             "progress": round(self.bitcoind_progress, 4)},
                "shit_aux": {"state": self.shit_aux_state,
                             "reason": self.shit_aux_reason},
                "poll_health": {
                    "consecutive_failures": self.consecutive_failures,
                    "job_age_secs": (round(time.time() - self.job_time, 1)
                                     if self.job else None),
                    "last_good_poll_age_secs": (
                        round(time.time() - self.last_good_poll, 1)
                        if self.last_good_poll else None)},
                "job": {"id": self.job.job_id, "height": self.job.height,
                        "aux": self.job.aux_order} if self.job else None,
            }


# ------------------------------------------------------- share validation ---

def validate_share(job, extranonce1_hex, extranonce2_hex, ntime_hex, nonce_hex):
    """Rebuild the header from a stratum submit. Returns dict or raises."""
    if len(extranonce2_hex) != 8:
        raise ValueError("bad extranonce2 size")
    try:
        en2 = bytes.fromhex(extranonce2_hex)
        ntime = int(ntime_hex, 16)
        nonce = int(nonce_hex, 16)
    except ValueError:
        raise ValueError("bad hex in submit")
    coinbase = (job.coinb1 + bytes.fromhex(extranonce1_hex) + en2 + job.coinb2)
    cb_txid = sha256d(coinbase)
    root = cb_txid
    for bhex in job.merkle_branch:
        root = sha256d(root + bytes.fromhex(bhex))
    header = (bytes.fromhex(job.version_hex)
              + bytes.fromhex(job.prevhash_stratum) + root
              + struct.pack("<I", ntime) + bytes.fromhex(job.nbits_hex)
              + struct.pack("<I", nonce))
    if len(header) != 80:
        raise ValueError("bad header assembly")
    return {"coinbase": coinbase, "cb_txid": cb_txid, "header": header,
            "hash": sha256d(header), "ntime": ntime, "nonce": nonce}


# -------------------------------------------------------------- stratum ---

# ------------------------------------------- stratum abuse defenses (Squad 6) ---
# Tunables for the stratum front door. Conservative: legit ASICs - even large
# ones, even several behind one NAT IP - stay far under these; floods trip
# them in seconds. Drops are logged as "stratum-abuse ip=.. reason=.." lines
# that the fail2ban filter (staged in ../fail2ban/) watches.
STRATUM_MAX_CONNS_PER_IP = 32    # simultaneous TCP connections per source IP
STRATUM_MAX_LINE = 4096          # max bytes per stratum line; longer => drop conn
STRATUM_MAX_RECV = 1 << 20       # max buffered unread bytes per conn; more => drop
STRATUM_MAX_BAD_JSON = 20        # malformed JSON lines before the conn is dropped
STRATUM_MAX_UNAUTH_SUBMIT = 10   # mining.submit before authorize => drop
STRATUM_MAX_SUBMITS_PER_MIN = 300  # per-conn sustained submit rate (sliding 60s)
STRATUM_MAX_BAD_PER_MIN = 60     # REJECTED submits per conn per 60s => drop conn
STRATUM_MAX_HANDSHAKE_PER_MIN = 20  # authorize+subscribe per conn per 60s (scope K)
WORKER_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")  # worker charset (scope H)


class _DropConn(Exception):
    """Raised inside stratum handlers when the connection must be killed."""
    pass


class MinerConn:
    def __init__(self, addr):
        self.addr = addr
        self.extranonce1 = os.urandom(4).hex()
        self.miner = None
        self.worker = "default"
        self.diff = 512.0
        self.shares_window = []  # timestamps of recent shares
        self.authorized = False
        self.shit_address = None
        # --- abuse-defense state (Squad 6) ---
        self.submit_times = []   # sliding 60s window of ALL submits (rate limit)
        self.bad_times = []      # sliding 60s window of REJECTED submits
        self.bad_json = 0        # malformed-line counter
        self.unauth_submits = 0  # submits seen before authorize
        self.handshake_times = []  # sliding 60s window of authorize/subscribe


class StratumServer:
    def __init__(self, state, port):
        self.state = state
        self.port = port
        self.conns = []
        self.conns_lock = threading.Lock()
        self.seen = set()  # share hashes this job (dup detection)
        self.seen_lock = threading.Lock()
        # --- abuse-defense state (Squad 6) ---
        self.ip_conns = {}       # ip -> active connection count
        self.ip_lock = threading.Lock()
        self.abuse = {"conns_rejected_ip": 0, "submits_rate_limited": 0,
                      "conns_dropped_abuse": 0, "shares_accepted": 0,
                      "shares_rejected": 0}
        self.reject_times = collections.deque()  # reject timestamps (rejects/min)
        self.abuse_lock = threading.Lock()

    def _abuse_bump(self, key, n=1):
        with self.abuse_lock:
            self.abuse[key] = self.abuse.get(key, 0) + n

    def _note_reject(self, mc):
        """Account one rejected share; drop the conn on sustained bad submits."""
        now = time.time()
        with self.abuse_lock:
            self.abuse["shares_rejected"] += 1
            self.reject_times.append(now)
        mc.bad_times = [tt for tt in mc.bad_times if now - tt < 60]
        mc.bad_times.append(now)
        if len(mc.bad_times) > STRATUM_MAX_BAD_PER_MIN:
            log("stratum-abuse ip=%s reason=submit-flood bad60s=%d"
                % (mc.addr[0], len(mc.bad_times)))
            self._abuse_bump("conns_dropped_abuse")
            raise _DropConn()

    def abuse_stats(self):
        """Snapshot for /api/stats: rejected-share rate + conns per IP."""
        now = time.time()
        with self.abuse_lock:
            while self.reject_times and self.reject_times[0] < now - 300:
                self.reject_times.popleft()
            rej5 = len(self.reject_times)
            counters = dict(self.abuse)
        with self.ip_lock:
            by_ip = dict(self.ip_conns)
        return {"rejects_last_5m": rej5,
                "rejects_per_min_5m": round(rej5 / 5.0, 2),
                "conns_by_ip": by_ip,
                "total_conns": sum(by_ip.values()),
                "counters": counters}

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(100)
        log("stratum listening on 0.0.0.0:%d" % self.port)
        while True:
            c, a = srv.accept()
            t = threading.Thread(target=self.handle, args=(c, a), daemon=True)
            t.start()

    def broadcast_job(self, clean):
        job = self.state.get_job()
        if not job:
            return
        with self.seen_lock:
            self.seen.clear()
        msg = {"id": None, "method": "mining.notify", "params": [
            job.job_id, job.prevhash_stratum, job.coinb1.hex(), job.coinb2.hex(),
            job.merkle_branch, job.version_hex, job.nbits_hex, job.ntime_hex,
            clean]}
        data = (json.dumps(msg) + "\n").encode()
        with self.conns_lock:
            conns = list(self.conns)
        for mc, sock in conns:
            try:
                sock.sendall(data)
            except Exception:
                pass

    def send_job(self, sock):
        job = self.state.get_job()
        if not job:
            return False
        msg = {"id": None, "method": "mining.notify", "params": [
            job.job_id, job.prevhash_stratum, job.coinb1.hex(),
            job.coinb2.hex(), job.merkle_branch, job.version_hex,
            job.nbits_hex, job.ntime_hex, True]}
        try:
            sock.sendall((json.dumps(msg) + "\n").encode())
            return True
        except Exception:
            return False

    def handle(self, sock, addr):
        ip = addr[0]
        with self.ip_lock:
            n = self.ip_conns.get(ip, 0)
            if n >= STRATUM_MAX_CONNS_PER_IP:
                self._abuse_bump("conns_rejected_ip")
                # NOTE: conn-limit is deliberately NOT a fail2ban trigger - a
                # NAT'd farm can trip it legitimately. Just refuse politely.
                log("stratum conn-limit ip=%s (already %d conns)" % (ip, n))
                try:
                    sock.close()
                except Exception:
                    pass
                return
            self.ip_conns[ip] = n + 1
        mc = MinerConn(addr)
        with self.conns_lock:
            self.conns.append((mc, sock))
        buf = b""
        sock.settimeout(600)
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > STRATUM_MAX_RECV:
                    log("stratum-abuse ip=%s reason=recv-flood buf=%d"
                        % (ip, len(buf)))
                    self._abuse_bump("conns_dropped_abuse")
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if line:
                        self.on_msg(mc, sock, line)
        except _DropConn:
            pass
        except Exception:
            pass
        finally:
            with self.ip_lock:
                left = self.ip_conns.get(ip, 1) - 1
                if left > 0:
                    self.ip_conns[ip] = left
                else:
                    self.ip_conns.pop(ip, None)
            with self.conns_lock:
                self.conns = [(m, s) for m, s in self.conns if s is not sock]
            try:
                sock.close()
            except Exception:
                pass

    def reply(self, sock, mid, result=None, error=None):
        msg = {"id": mid, "result": result, "error": error}
        try:
            sock.sendall((json.dumps(msg) + "\n").encode())
        except Exception:
            pass

    def on_msg(self, mc, sock, line):
        ip = mc.addr[0]
        if len(line) > STRATUM_MAX_LINE:
            log("stratum-abuse ip=%s reason=oversize-line len=%d"
                % (ip, len(line)))
            self._abuse_bump("conns_dropped_abuse")
            raise _DropConn()
        try:
            m = json.loads(line)
        except Exception:
            mc.bad_json += 1
            if mc.bad_json > STRATUM_MAX_BAD_JSON:
                log("stratum-abuse ip=%s reason=bad-json n=%d"
                    % (ip, mc.bad_json))
                self._abuse_bump("conns_dropped_abuse")
                raise _DropConn()
            return
        if not isinstance(m, dict):
            # Valid JSON but not an object (array/scalar): count it as a
            # bad line and keep the connection accounting honest — the old
            # code raised on m.get() and killed the conn via handle()'s
            # generic except, uncounted (scope K / red-team #10).
            mc.bad_json += 1
            if mc.bad_json > STRATUM_MAX_BAD_JSON:
                log("stratum-abuse ip=%s reason=bad-json n=%d"
                    % (ip, mc.bad_json))
                self._abuse_bump("conns_dropped_abuse")
                raise _DropConn()
            return
        method, mid, params = m.get("method"), m.get("id"), m.get("params") or []
        if method in ("mining.subscribe", "mining.authorize"):
            # Handshake rate limit per connection (scope K / red-team #9):
            # each authorize used to cost a sqlite kv write under the
            # global lock, unlimited. Legit miners handshake once.
            now = time.time()
            mc.handshake_times = [tt for tt in mc.handshake_times
                                  if now - tt < 60]
            if len(mc.handshake_times) >= STRATUM_MAX_HANDSHAKE_PER_MIN:
                log("stratum-abuse ip=%s reason=handshake-flood n=%d"
                    % (ip, len(mc.handshake_times)))
                self._abuse_bump("conns_dropped_abuse")
                raise _DropConn()
            mc.handshake_times.append(now)
        if method == "mining.subscribe":
            self.reply(sock, mid, result=[
                [["mining.set_difficulty", "x"], ["mining.notify", "y"]],
                mc.extranonce1, 4])
            self.send_difficulty(sock, mc.diff)
            if not self.send_job(sock):
                log("miner %s subscribed, no job yet (waiting for bitcoind)" % (mc.addr,))
        elif method == "mining.authorize":
            user = str(params[0]) if params else ""
            pw = str(params[1]) if len(params) > 1 else ""
            if "." in user:
                addr, worker = user.split(".", 1)
            else:
                addr, worker = user, "default"
            # Worker charset (scope H): the worker string is stored and
            # later rendered on the dashboard; restrict it at the door.
            if not WORKER_RE.match(worker):
                self.reply(sock, mid, result=False,
                           error=[24, "invalid worker name", None])
                return
            # Validate the BTC identity FIRST. No kv write of any kind may
            # happen for a failed authorize (scope E): the old code wrote
            # shitaddr: before this check, so an invalid "miner" address
            # could poison the payout address book (red-team #1).
            if not valid_btc_address(addr):
                self.reply(sock, mid, result=False,
                           error=[24, "invalid BTC address", None])
                return
            # Optional SHIT payout address via password: "shit=shit1q...".
            # FIRST-WRITE-WINS (scope E): the BTC password is unchecked by
            # design (the address is the identity), so the only thing
            # stopping payout-address theft is that the first registered
            # SHIT address for a miner can never be overwritten by a
            # later connection. Overwrite attempts are rejected, logged,
            # and counted for the dashboard alert.
            shit_val = None
            for part in pw.split():
                if part.startswith("shit="):
                    shit_val = part[5:]
            if shit_val is not None:
                if not valid_shit_address(shit_val):
                    log("shitaddr rejected (invalid SHIT address) for "
                        "%s from %s" % (addr, ip))
                else:
                    existing = kv_get("shitaddr:" + addr)
                    if existing is None:
                        kv_set("shitaddr:" + addr, shit_val)
                        mc.shit_address = shit_val
                        log("shitaddr registered for %s" % addr)
                    elif existing == shit_val:
                        mc.shit_address = shit_val   # no write needed
                    else:
                        self._abuse_bump("shitaddr_overwrite_attempts")
                        try:
                            n = int(kv_get("shitaddr_overwrite_attempts",
                                           "0")) + 1
                        except ValueError:
                            n = 1
                        kv_set("shitaddr_overwrite_attempts", str(n))
                        log("ALERT shitaddr overwrite attempt for %s "
                            "from %s (attempt #%d) — keeping registered "
                            "address" % (addr, ip, n))
                        mc.shit_address = existing
            mc.miner, mc.worker, mc.authorized = addr, worker, True
            log("miner authorized: %s.%s" % (addr, worker))
            self.reply(sock, mid, result=True)
        elif method == "mining.submit":
            self.on_submit(mc, sock, mid, params)
        elif method == "mining.extranonce.subscribe":
            self.reply(sock, mid, result=False)
        else:
            self.reply(sock, mid, result=None, error=[20, "unknown method", None])

    def send_difficulty(self, sock, diff):
        try:
            sock.sendall((json.dumps(
                {"id": None, "method": "mining.set_difficulty",
                 "params": [diff]}) + "\n").encode())
        except Exception:
            pass

    def on_submit(self, mc, sock, mid, params):
        if not mc.authorized:
            mc.unauth_submits += 1
            if mc.unauth_submits > STRATUM_MAX_UNAUTH_SUBMIT:
                log("stratum-abuse ip=%s reason=unauth-probe n=%d"
                    % (mc.addr[0], mc.unauth_submits))
                self._abuse_bump("conns_dropped_abuse")
                raise _DropConn()
            self.reply(sock, mid, result=False, error=[24, "unauthorized", None])
            return
        if len(params) < 5:
            self._note_reject(mc)
            self.reply(sock, mid, result=False, error=[20, "bad params", None])
            return
        # --- submit rate limit (BEFORE any sqlite write or crypto): floods die
        # here cheaply. 300/min is far above any legit vardiff'd miner; a big
        # rig's ramp-up burst is mostly VALID shares, which never trip the
        # bad-submit drop below.
        now = time.time()
        mc.submit_times = [tt for tt in mc.submit_times if now - tt < 60]
        if len(mc.submit_times) >= STRATUM_MAX_SUBMITS_PER_MIN:
            self._abuse_bump("submits_rate_limited")
            self.reply(sock, mid, result=False, error=[25, "rate limited", None])
            return
        mc.submit_times.append(now)
        _user, job_id, en2, ntime_hex, nonce_hex = params[:5]
        job = self.state.get_job()
        if not job or job.job_id != str(job_id):
            self._note_reject(mc)
            record_share(mc.miner, mc.worker, -1, str(job_id))  # stale counted as reject
            self.reply(sock, mid, result=False, error=[21, "stale job", None])
            return
        # ntime sanity: within [curtime-300, curtime+7200]
        try:
            ntime = int(str(ntime_hex), 16)
        except ValueError:
            self._note_reject(mc)
            self.reply(sock, mid, result=False, error=[20, "bad ntime", None])
            return
        if not (job.curtime - 300 <= ntime <= job.curtime + 7200):
            self._note_reject(mc)
            record_share(mc.miner, mc.worker, -1, job.job_id)
            self.reply(sock, mid, result=False, error=[21, "ntime out of range", None])
            return
        try:
            v = validate_share(job, mc.extranonce1, str(en2), str(ntime_hex),
                               str(nonce_hex))
        except ValueError as e:
            self._note_reject(mc)
            record_share(mc.miner, mc.worker, -1, job.job_id)
            self.reply(sock, mid, result=False, error=[20, str(e), None])
            return
        # duplicate check (in-memory; cleared on each new job)
        dup_key = v["hash"].hex()
        with self.seen_lock:
            if dup_key in self.seen:
                self._note_reject(mc)
                self.reply(sock, mid, result=False, error=[22, "duplicate", None])
                return
            self.seen.add(dup_key)
        h = hash_le_int(v["hash"])
        # --- pool share? ---
        if h > diff_to_target(mc.diff):
            self._note_reject(mc)
            record_share(mc.miner, mc.worker, -1, job.job_id)
            self.reply(sock, mid, result=False, error=[23, "low difficulty", None])
            return
        record_share(mc.miner, mc.worker, mc.diff, job.job_id)
        self._abuse_bump("shares_accepted")
        mc.shares_window.append(time.time())
        self.reply(sock, mid, result=True, error=None)
        # --- BTC block? ---
        if h <= job.target:
            log("!!! BTC BLOCK FOUND by %s.%s job=%s" % (mc.miner, mc.worker, job.job_id))
            self.submit_btc_block(job, v)
        # --- aux blocks? ---
        for name in job.aux_order:
            at = job.aux[name]
            if h <= at["target"]:
                log("!!! %s AUX BLOCK FOUND by %s.%s" % (name, mc.miner, mc.worker))
                self.submit_aux_block(name, job, v)
        self.vardiff(mc, sock)

    def vardiff(self, mc, sock):
        now = time.time()
        mc.shares_window = [t for t in mc.shares_window if now - t < 120]
        n = len(mc.shares_window)
        if n > 40 and mc.diff < 1e12:
            mc.diff *= 2
            mc.shares_window = []
            self.send_difficulty(sock, mc.diff)
        elif n < 4 and mc.diff > 64:
            mc.diff /= 2
            self.send_difficulty(sock, mc.diff)

    def _btc_block_visible(self, height, block_hash_hex):
        try:
            return self.state.btc.call("getblockhash",
                                       [height]) == block_hash_hex
        except RPCError:
            return False

    def submit_btc_block(self, job, v):
        # coinbase with witness: insert marker/flag, append witness stack
        cb = v["coinbase"]
        cb_wit = cb[0:4] + b"\x00\x01" + cb[4:-4] + b"\x01\x20" + b"\x00" * 32 + cb[-4:]
        block = (v["header"] + varint(1 + len(job.tx_datas)) + cb_wit
                 + b"".join(job.tx_datas))
        block_hex = block.hex()
        block_hash = v["hash"][::-1].hex()
        # Persist BEFORE submitting (scope B / TEMPLATE-REPORT R1): a
        # found block is ~3.125 BTC. The old code kept it only in memory
        # and made a single submitblock attempt over an RPC link that
        # timed out 1,021 times in a day — a transient timeout lost the
        # block forever. The hex on disk survives any crash and can be
        # resubmitted by hand if every automatic attempt fails.
        try:
            fdir = os.path.join(DATA_DIR, "found_blocks")
            os.makedirs(fdir, exist_ok=True)
            fpath = os.path.join(fdir, "BTC-%d-%s.hex"
                                 % (job.height, block_hash))
            if not os.path.exists(fpath):
                with open(fpath + ".tmp", "w") as f:
                    f.write(block_hex)
                os.rename(fpath + ".tmp", fpath)
            log("found BTC block %d persisted to %s" % (job.height, fpath))
        except Exception as e:
            log("WARNING: could not persist found block %d: %r "
                "(continuing to submit)" % (job.height, e))
        # Submit with bounded retry + backoff; credit ONLY on verified
        # acceptance (scope B / audit B-3): submitblock returning a string
        # is a rejection, not a success, and a lost reply is ambiguous —
        # both are resolved by checking whether the block is visible.
        accepted = False
        delays = [0, 2, 5, 15]
        for attempt in range(len(delays)):
            if delays[attempt]:
                time.sleep(delays[attempt])
            try:
                res = self.state.btc.call("submitblock", [block_hex])
            except RPCError as e:
                log("submitblock BTC attempt %d RPCError: %s"
                    % (attempt + 1, e))
                if self._btc_block_visible(job.height, block_hash):
                    accepted = True
                    break
                continue
            log("submitblock BTC attempt %d result: %r" % (attempt + 1, res))
            if res is None:
                for _ in range(3):
                    if self._btc_block_visible(job.height, block_hash):
                        accepted = True
                        break
                    time.sleep(1)
                if accepted:
                    break
                log("submitblock reported success but block %d is not "
                    "visible yet — retrying" % job.height)
                continue
            # Rejection string. It still counts as accepted if the block
            # is visible (e.g. "duplicate" of our own earlier attempt
            # whose reply was lost).
            if self._btc_block_visible(job.height, block_hash):
                accepted = True
            else:
                log("submitblock BTC REJECTED: %r — block %d NOT "
                    "credited (hex kept on disk)" % (res, job.height))
            break
        if not accepted:
            log("ALERT: BTC block %d was NOT accepted after all attempts "
                "— NOT credited. Hex is persisted under found_blocks/ "
                "for manual resubmission." % job.height)
            return
        # Tax-exact crediting (scope D / audit B-2): miners are credited
        # the pool's actual coinbase output, coinbasevalue − tax, never
        # the gross. Crediting the gross over-credited exactly the tax
        # on every block.
        net_reward = job.coinbasevalue - job.tax_sats
        self.credit_block("BTC", job.height, block_hash, net_reward, "pool")

    def submit_aux_block(self, name, job, v):
        chain = {"SHIT": self.state.shit_chain,
                 "NMC": self.state.nmc_chain,
                 "FB": self.state.fb_chain,
                 "ELA": self.state.ela_chain,
                 "SYS": self.state.sys_chain,
                 "UNO": self.state.uno_chain}.get(name)
        if chain is None:
            return
        ok = chain.submit(job, v["coinbase"], v["header"])
        if ok:
            at = job.aux[name]
            reward = at.get("coinbasevalue", 0)
            if reward <= 0:
                # With the scope-F wiring the SHIT template always
                # carries its real subsidy; a zero here means the wiring
                # regressed — shout instead of silently crediting 0.
                log("WARNING: %s block at height %s accepted but template "
                    "carries coinbasevalue=%r — crediting 0"
                    % (name, at.get("height"), at.get("coinbasevalue")))
            self.credit_block(name, at.get("height", 0),
                              bytes.fromhex(at["hash_hex"])[::-1].hex(),
                              reward, "pool", aux_target=at.get("target"))

    def credit_block(self, chain, height, block_hash, reward_sats, finder,
                     aux_target=None):
        """PPLNS credit for a VERIFIED-ACCEPTED block.

        reward_sats: the amount the pool actually received for the block
        (for BTC: coinbasevalue − tax; for aux chains: the template's
        real coinbasevalue). Crediting is exactly-once (scope B): the
        balance distribution runs only when the blocks-row INSERT
        actually inserted — a duplicate (chain, height) can never
        credit twice (audit B-4 / resilience finding A1)."""
        fee_bps = {"BTC": 0, "SHIT": 100, "NMC": 100}.get(chain, 100)
        # PPLNS window sizing from the chain's real difficulty (scope F):
        # BTC from the current job target, aux chains from their own
        # template target. The old code used window 0 for every aux
        # chain, which silently paid ALL historic shares on every aux
        # block (red-team #6).
        job = self.state.get_job()
        if chain == "BTC" and job:
            net_diff = job.target and (DIFF1_TARGET // job.target)
            window_need = 2 * net_diff if net_diff else 0
        elif aux_target:
            window_need = 2.0 * (DIFF1_TARGET / aux_target)
        else:
            window_need = 0
        now = int(time.time())
        with _db_lock:
            con = db()
            cur = con.execute(
                "INSERT OR IGNORE INTO blocks(chain,height,hash,reward,finder,t)"
                " VALUES(?,?,?,?,?,?)",
                (chain, height, block_hash, reward_sats, finder, now))
            if cur.rowcount == 0:
                con.commit()
                con.close()
                log("credit skipped: %s block %d already credited "
                    "(exactly-once gate)" % (chain, height))
                return
            # PPLNS window: newest shares until cumulative diff >= window
            rows = con.execute(
                "SELECT miner, diff FROM shares ORDER BY t DESC, id DESC").fetchall()
            per_miner, total = {}, 0.0
            for miner, d in rows:
                if window_need and total >= window_need:
                    break
                d = max(d, 0)
                if d > 0:
                    per_miner[miner] = per_miner.get(miner, 0) + d
                total += d
            distributable = reward_sats - (reward_sats * fee_bps // 10000)
            for miner, d in per_miner.items():
                if total <= 0:
                    break
                credit = int(distributable * d / total)
                con.execute("INSERT INTO balances(miner,chain,pending,paid) VALUES(?,?,?,0)"
                            " ON CONFLICT(miner,chain) DO UPDATE SET pending=pending+?",
                            (miner, chain, credit, credit))
            con.commit()
            con.close()
        log("credited %s block %d: %d sats to %d miners (fee %.2f%%)" %
            (chain, height, distributable, len(per_miner), fee_bps / 100.0))

# ------------------------------------------------------------ payout engine ---

CHAINS = ("BTC", "SHIT", "NMC")


def payout_threshold(chain):
    if chain == "BTC":
        return int(CFG["BTC_PAYOUT_THRESHOLD_SATS"])
    if chain == "SHIT":
        return int(CFG["SHIT_PAYOUT_THRESHOLD_SATS"])
    return 10 ** 8  # 1 NMC default


def payout_address_for(miner, chain):
    if chain == "BTC":
        return miner if valid_btc_address(miner) else None
    if chain == "SHIT":
        a = kv_get("shitaddr:" + miner)
        if a and not valid_shit_address(a):
            # Defense in depth: a stored address that fails validation
            # (e.g. written before scope E landed) is never paid.
            log("payout %s SHIT: stored address fails validation — "
                "accruing, not paying" % miner)
            return None
        return a  # may be None -> accrues
    return None  # NMC: address mapping TBD


def sats_to_btc_str(sats):
    """Integer satoshis -> exact decimal BTC string for RPC amounts
    (scope J). No float ever touches a send amount."""
    sats = int(sats)
    sign = "-" if sats < 0 else ""
    sats = abs(sats)
    return "%s%d.%08d" % (sign, sats // 100000000, sats % 100000000)


def _payout_flock():
    """Single-instance payout lock (scope C): an flock held for a whole
    payout run, so a second pool process can never double-pay the same
    balances. Returns the open lock file, or None if already held."""
    try:
        f = open(os.path.join(DATA_DIR, "payout.lock"), "a+")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        try:
            f.close()
        except Exception:
            pass
        return None


def _wallet_rpc(state, chain):
    if chain == "BTC":
        return state.btc
    if chain == "SHIT" and getattr(state, "shit_chain", None) is not None:
        return state.shit_chain.rpc
    return None


def _find_wallet_send(state, chain, address, amount_sats, since_t):
    """Reconciliation lookup (scope C): search the sending wallet's own
    transaction history for a send of exactly amount_sats to address at
    or after since_t (minus a 300 s clock-skew allowance — a send for
    this row can only have happened after the row was queued). Returns
    the txid or None. The wallet — not our DB — is the ground truth for
    whether a send happened.

    A txid already recorded on another payout row is never matched
    twice: two unresolved rows for the same miner and amount cannot
    both claim the same historical send."""
    rpc = _wallet_rpc(state, chain)
    if rpc is None:
        return None
    with _db_lock:
        con = db()
        used = set(r[0] for r in con.execute(
            "SELECT txid FROM payouts WHERE txid IS NOT NULL").fetchall())
        con.close()
    try:
        txs = rpc.call("listtransactions", ["*", 200])
    except RPCError:
        return None
    if not isinstance(txs, list):
        return None
    for tx in reversed(txs):
        try:
            if tx.get("category") != "send":
                continue
            if tx.get("address") != address:
                continue
            if tx.get("txid") in used:
                continue
            sent_sats = int(round(abs(float(tx.get("amount"))) * 1e8))
            if sent_sats != int(amount_sats):
                continue
            t = tx.get("time") or tx.get("blocktime") or 0
            if t and t < since_t - 300:
                continue
            return tx.get("txid")
        except Exception:
            continue
    return None


def _payout_set_status(payout_id, status, txid=None, error=None):
    """Row-scoped payout update (scope C): always by payout ID, never by
    (miner, chain, status='queued') — the old unscoped updates marked or
    refunded every queued row for a miner in lockstep (audit M-5)."""
    with _db_lock:
        con = db()
        con.execute("UPDATE payouts SET status=?, txid=?, error=? WHERE id=?",
                    (status, txid, error, payout_id))
        con.commit()
        con.close()


def reconcile_payout_row(state, row):
    """Check one unresolved payout row against the wallet. Marks it
    'sent' (with the real txid) when the wallet proves the send.
    Returns True when resolved."""
    pid, _miner, chain, amount, address, _status, t = row
    txid = _find_wallet_send(state, chain, address, amount, t)
    if txid:
        _payout_set_status(pid, "sent", txid=txid)
        log("payout %d RECONCILED to sent via wallet history txid=%s"
            % (pid, txid))
        return True
    return False


def recover_payouts(state):
    """Startup / tick-start sweep (scope C, audit B-6): resolve payout
    rows left in queued/sending/unknown by a crash or an ambiguous send.

    - 'queued' rows were committed but the send was never attempted
      (execute_payout flips a row to 'sending' BEFORE the RPC): after a
      wallet check, they are executed — the miner is paid exactly once.
    - 'sending'/'unknown' rows may already have been broadcast: they are
      reconciled against the wallet and NEVER auto-resent and NEVER
      auto-refunded. Unresolved rows stay 'unknown' and are alerted for
      a human decision."""
    with _db_lock:
        con = db()
        rows = con.execute(
            "SELECT id, miner, chain, amount, address, status, t "
            "FROM payouts WHERE status IN ('queued','sending','unknown') "
            "ORDER BY id").fetchall()
        con.close()
    for row in rows:
        pid, miner, chain, amount, address, status, _t = row
        if status == "queued":
            if reconcile_payout_row(state, row):
                continue
            log("recovering queued payout %d: %s %d %s -> %s (send was "
                "never attempted)" % (pid, miner, amount, chain, address))
            execute_payout(state, pid, miner, chain, amount, address)
        else:
            if reconcile_payout_row(state, row):
                continue
            log("ALERT: payout %d (%s %d %s -> %s) is %s and the wallet "
                "shows no matching send — left UNKNOWN for manual "
                "reconciliation. NOT refunded, NOT resent."
                % (pid, miner, amount, chain, address, status))
            if status == "sending":
                _payout_set_status(pid, "unknown",
                                   error="unresolved after restart sweep")


def payout_tick(state):
    """Hourly: queue payouts over threshold. Executor only if enabled.

    Exactly-once shape (scope C): the whole tick runs under a
    cross-process flock; it starts by recovering unresolved rows; each
    new payout debits its balance inside the same transaction that
    creates its payout row, re-reading the balance in that transaction
    (no snapshot race, audit M-4)."""
    enabled = CFG["PAYOUTS_ENABLED"] == "true"
    now = int(time.time())
    with _db_lock:
        con = db()
        rows = con.execute(
            "SELECT miner, chain, pending FROM balances WHERE pending > 0").fetchall()
        con.close()
    if not enabled:
        for miner, chain, pending in rows:
            if pending < payout_threshold(chain):
                continue
            addr = payout_address_for(miner, chain)
            if not addr:
                log("payout %s %s: %d pending but no %s address set - accruing" %
                    (miner, chain, pending, chain))
                continue
            log("DRY-RUN payout: %s -> %s %d (%s)" % (miner, addr, pending, chain))
        return
    lockf = _payout_flock()
    if lockf is None:
        log("payout tick skipped: another payout run holds the lock")
        return
    try:
        recover_payouts(state)
        for miner, chain, _snapshot in rows:
            addr = payout_address_for(miner, chain)
            if not addr:
                log("payout %s %s: no %s address set - accruing" %
                    (miner, chain, chain))
                continue
            # Atomic per-miner debit: re-read the balance inside the
            # same transaction that queues the payout row.
            payout_id = None
            amount = 0
            with _db_lock:
                con = db()
                try:
                    r = con.execute(
                        "SELECT pending FROM balances WHERE miner=? AND chain=?",
                        (miner, chain)).fetchone()
                    if not r or r[0] < payout_threshold(chain):
                        con.close()
                        continue
                    amount = r[0]
                    cur = con.execute(
                        "INSERT INTO payouts(miner,chain,amount,address,txid,status,t)"
                        " VALUES(?,?,?,?,?, 'queued', ?)",
                        (miner, chain, amount, addr, None, now))
                    payout_id = cur.lastrowid
                    cur = con.execute(
                        "UPDATE balances SET pending=pending-?, paid=paid+? "
                        "WHERE miner=? AND chain=? AND pending=?",
                        (amount, amount, miner, chain, amount))
                    if cur.rowcount != 1:
                        con.rollback()
                        con.close()
                        log("payout for %s %s aborted: balance changed "
                            "under the debit" % (miner, chain))
                        payout_id = None
                        continue
                    con.commit()
                finally:
                    try:
                        con.close()
                    except Exception:
                        pass
            if payout_id is None:
                continue
            log("queued payout #%d: %s %d %s -> %s"
                % (payout_id, miner, amount, chain, addr))
            execute_payout(state, payout_id, miner, chain, amount, addr)
    finally:
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
            lockf.close()
        except Exception:
            pass


def execute_payout(state, payout_id, miner, chain, amount, addr):
    """Broadcast one queued payout (scope C state machine).

    queued -> sending -> sent | unknown. The row is flipped to 'sending'
    BEFORE the send RPC, so a crash mid-send leaves a truthful marker.
    ANY send error is ambiguous (the daemon may have broadcast before
    the reply was lost): the row goes to 'unknown', the amount is NOT
    refunded and the send is NOT retried — reconciliation against the
    wallet (recover_payouts) decides what happened. The old code
    refunded on every RPCError, which double-paid whenever the daemon
    had in fact sent (audit B-5)."""
    with _db_lock:
        con = db()
        cur = con.execute(
            "UPDATE payouts SET status='sending' WHERE id=? AND status='queued'",
            (payout_id,))
        flipped = cur.rowcount
        con.commit()
        con.close()
        if flipped != 1:
            log("execute_payout #%d skipped: row is not in 'queued' state"
                % payout_id)
            return
    amt_str = sats_to_btc_str(amount)
    try:
        if chain == "BTC":
            # node3 bitcoind needs a loaded wallet for sendtoaddress
            txid = state.btc.call("sendtoaddress", [addr, amt_str])
        elif chain == "SHIT":
            txid = state.shit_chain.rpc.call(
                "sendtoaddress", [addr, amt_str])
        else:
            raise RPCError("no payout path for " + chain)
    except Exception as e:
        _payout_set_status(payout_id, "unknown",
                           error=str(e)[:200])
        log("PAYOUT UNKNOWN #%d %s %d %s -> %s: %r — NOT refunded, NOT "
            "retried; reconciliation against the wallet will decide"
            % (payout_id, miner, amount, chain, addr, e))
        return
    _payout_set_status(payout_id, "sent", txid=txid)
    log("PAYOUT SENT #%d %s %d %s -> %s txid=%s"
        % (payout_id, miner, amount, chain, addr, txid))


# ---------------------------------------------------------------- dashboard ---

TEMPLATE_DIR = os.path.join(DATA_DIR, "templates")


def fmt_hashrate(hps):
    for unit in ("H/s", "KH/s", "MH/s", "GH/s", "TH/s", "PH/s", "EH/s"):
        if hps < 1000 or unit == "EH/s":
            return "%.2f %s" % (hps, unit)
        hps /= 1000
    return "%.2f EH/s" % hps


def render_index(state):
    # The templates carry the truthful copy themselves (scope H); this
    # renderer only fills values and the state-driven status line. The
    # old runtime string replacements (fee corrections, FPPS->PPLNS,
    # "mockup"->"Live.") are gone: if a template regresses, it now
    # shows, instead of being silently patched over.
    tpl = open(os.path.join(TEMPLATE_DIR, "index.html")).read()
    st = pool_stats()
    s = state.status()
    payouts_on = CFG["PAYOUTS_ENABLED"] == "true"
    # stat strip: hashrate / miners / blocks 24h / payout state.
    # Payouts are shown DISABLED/OFF unless actually enabled — the old
    # tile rendered bitcoind's state here and read "READY" while
    # payouts were disabled (CONFIG-DASHBOARD F3).
    vals = [fmt_hashrate(st["hashrate_hps"]),
            str(st["miners_online"]),
            str(sum(st["blocks_24h"].values())),
            "HOURLY" if payouts_on else "OFF"]
    i = 0

    def sub(m):
        nonlocal i
        v = vals[i] if i < len(vals) else "--"
        i += 1
        return '<div class="v">%s' % v
    tpl = re.sub(r'<div class="v">--', sub, tpl, count=4)
    # State-driven status line (scope H): merge mining is LIVE only
    # when the aux feed is connected AND SHIT templates are flowing in
    # the current job; payouts state is stated plainly either way.
    job = s.get("job") or {}
    mm_live = (s["shit_aux"]["state"] == "ready"
               and "SHIT" in (job.get("aux") or []))
    if mm_live:
        mm_line = "SHIT merge mining: LIVE"
    else:
        mm_line = ("SHIT merge mining: not active (%s)"
                   % html.escape(s["shit_aux"]["reason"] or
                                 s["shit_aux"]["state"]))
    pay_line = ("Payouts: ENABLED (hourly)"
                if payouts_on else
                "Payouts: DISABLED — balances accrue, nothing is sent yet")
    banner = ('<div style="background:#151920;border:1px solid #232a34;'
              'border-radius:10px;padding:14px 18px;margin:20px 0;'
              'color:#e8ecf1">%s &nbsp;·&nbsp; %s</div>'
              % (html.escape(mm_line), html.escape(pay_line)))
    if s["bitcoind"]["state"] != "ready":
        banner += ('<div style="background:#3a2a12;border:1px solid #f7931a;border-radius:10px;'
                   'padding:14px 18px;margin:0 0 20px;color:#f5c87a">Waiting for Bitcoin node '
                   'sync (%s, %.1f%%). Stratum is up; jobs start automatically.</div>'
                   % (html.escape(s["bitcoind"]["state"]),
                      s["bitcoind"]["progress"] * 100))
    tpl = tpl.replace("<!--POOL_STATUS-->", banner, 1)
    return tpl


def render_miner(state, miner):
    # Templates carry truthful copy (scope H); every DB-derived string
    # rendered here is html.escape()d (red-team #7: worker names were a
    # stored-XSS vector).
    tpl = open(os.path.join(TEMPLATE_DIR, "dashboard.html")).read()
    ms = miner_stats(miner)
    short = miner[:12] + "…" + miner[-6:] if len(miner) > 20 else miner
    tpl = tpl.replace("bc1q…example — look up any address you mine with, no login",
                      "%s — no login, your address is your account"
                      % html.escape(short))
    bal = {c: (p, d) for c, p, d in ms["balances"]}
    btc_p, btc_d = bal.get("BTC", (0, 0))
    shit_p, shit_d = bal.get("SHIT", (0, 0))
    vals = [fmt_hashrate(ms["hashrate_1h"]),
            "%d <small>/ %d</small>" % (sum(1 for _, ls in ms["workers"]
                                           if time.time() - ls < 600),
                                       len(ms["workers"])),
            str(ms["shares_24h"]),
            "%.1f <small>%%</small>" % (100 * ms["rejected_24h"] / ms["shares_24h"]
                                       if ms["shares_24h"] else 0),
            "%.8f BTC" % (btc_p / 1e8),
            "%.8f BTC" % (btc_d / 1e8),
            "%.2f SHIT" % (shit_p / 1e8),
            "%.2f SHIT" % (shit_d / 1e8)]
    i = 0

    def sub(m):
        nonlocal i
        v = vals[i] if i < len(vals) else "--"
        i += 1
        return '<div class="v">%s' % v
    tpl = re.sub(r'<div class="v">--', sub, tpl, count=8)
    # SHIT payout address (the one remaining bare placeholder): escaped.
    tpl = tpl.replace('<div class="v">--</div>',
                      '<div class="v">%s</div>'
                      % html.escape(ms["shit_address"] or "not set"), 1)
    # workers table — worker names are attacker-controlled DB strings.
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>--</td><td>--</td><td>%s</td>"
        "<td class=\"%s\">%s</td></tr>" % (
            html.escape(str(w)), fmt_hashrate(0),
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(ls)) if ls else "--",
            "ok" if time.time() - ls < 600 else "warn",
            "online" if time.time() - ls < 600 else "idle")
        for w, ls in ms["workers"][:20])
    tpl = re.sub(r"<tr><td>rig01</td>.*?</tr>\s*<tr><td>rig02</td>.*?</tr>",
                 rows or "<tr><td colspan=6>No workers yet</td></tr>",
                 tpl, flags=re.S)
    # Last share + this miner's own payout history (benchmark R5, partial:
    # both render rows the pool already collects — shares.t and the
    # payouts rows miner_stats already fetches. A per-miner blocks-found
    # count is NOT shown: blocks.finder is the constant "pool" in the
    # deployed crediting code, so no per-miner block data exists to show
    # without new data collection.) All values are html.escape()d.
    if ms.get("last_share"):
        last_share = time.strftime("%Y-%m-%d %H:%M UTC",
                                   time.gmtime(ms["last_share"]))
    else:
        last_share = "no shares yet"
    tpl = tpl.replace("<!--LAST_SHARE-->", html.escape(last_share), 1)
    if ms["payouts"]:
        prows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td>"
            "<td class=\"a\">%s</td></tr>" % (
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)),
                html.escape(str(ch)),
                ("%.8f" % (amt / 1e8)) if ch == "BTC"
                else ("%.2f" % (amt / 1e8)),
                html.escape(str(tx or status or "--")))
            for t, ch, amt, tx, status in ms["payouts"])
    else:
        prows = ('<tr><td colspan="4">No payouts for this address yet.'
                 "</td></tr>")
    tpl = tpl.replace("<!--MINER_PAYOUTS-->", prows, 1)
    return tpl


def render_payouts(state):
    # Templates carry truthful copy (scope H); DB-derived strings are
    # html.escape()d; the payout-enabled state is stated on the page.
    tpl = open(os.path.join(TEMPLATE_DIR, "payouts.html")).read()
    if CFG["PAYOUTS_ENABLED"] == "true":
        state_line = ("Payouts are ENABLED and run hourly over the "
                      "thresholds below.")
    else:
        state_line = ("Payouts are currently DISABLED. Balances accrue "
                      "and are shown on your miner page; nothing is sent "
                      "until payouts are switched on.")
    tpl = tpl.replace("<!--PAYOUT_STATE-->",
                      '<div class="card"><p>%s</p></div>'
                      % html.escape(state_line), 1)
    with _db_lock:
        con = db()
        rows = con.execute(
            "SELECT t, chain, address, amount, txid, status FROM payouts "
            "ORDER BY t DESC LIMIT 30").fetchall()
        con.close()
    if rows:
        trs = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td class=\"a\">%s</td></tr>" % (
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)),
                html.escape(str(ch)),
                html.escape((a[:10] + "…" + a[-4:])
                            if a and len(a) > 16 else (a or "--")),
                ("%.8f" % (amt / 1e8)) if ch == "BTC" else ("%.2f" % (amt / 1e8)),
                html.escape(str(tx or status or "--")))
            for t, ch, a, amt, tx, status in rows)
        tpl = re.sub(r"<tr><td>--</td><td>BTC</td>.*?</tr>\s*<tr><td>--</td><td>SHIT</td>.*?</tr>",
                     trs, tpl, flags=re.S)
    else:
        # Empty state (benchmark R4a): a sentence, never the template's
        # dash placeholder rows — dashes read as redacted real payouts.
        # Wording is state-driven, like the banner above: while payouts
        # are disabled the empty state says so.
        if CFG["PAYOUTS_ENABLED"] == "true":
            empty = "No payouts yet."
        else:
            empty = ("No payouts yet — payouts are currently DISABLED. "
                     "Balances accrue and are shown on your miner page; "
                     "nothing is sent until payouts are switched on.")
        tpl = re.sub(r"<tr><td>--</td><td>BTC</td>.*?</tr>\s*<tr><td>--</td><td>SHIT</td>.*?</tr>",
                     '<tr><td colspan="5">%s</td></tr>' % html.escape(empty),
                     tpl, flags=re.S)
    return tpl


# ------------------------------------------------------- miner lookup ---
# Benchmark R1: /miner and /miner/ are linked from every page's nav and
# from the homepage "Start Mining" button, but the deployed handler had
# no route for them — they returned a 404 with an empty body and the
# core miner journey (connect -> enter address -> see stats) dead-ended.
# The landing below is presentation only: classification reuses the
# deployed validators (valid_btc_address / valid_shit_address) and the
# stratum authorize split of "address.worker" — no new address rules.

LOOKUP_ERR_INVALID = (
    "That does not look like a Bitcoin address. Check it and try again "
    "— use the Bitcoin address you mine with (mainnet addresses start "
    "with 1, 3, bc1q or bc1p).")
LOOKUP_ERR_SHIT = (
    "That is a SHIT address. A SHIT address is a payout address, not a "
    "mining address — look up the Bitcoin address you mine with "
    "instead. Your SHIT balance is shown on that address's page.")
LOOKUP_ERR_TESTNET = (
    "That looks like a testnet address. This pool mines mainnet "
    "Bitcoin — look up the mainnet Bitcoin address you mine with.")


def lookup_kind(raw):
    """Classify a lookup string the way the pool itself treats it.

    Returns (kind, base): kind is "btc", "shit", "testnet" or "invalid";
    base is the address with any ".worker" suffix stripped (stratum
    authorize splits on the first dot the same way, and miner stats are
    keyed by the address, not the worker)."""
    a = (raw or "").strip()
    base = a.split(".", 1)[0] if "." in a else a
    if valid_btc_address(base):
        return "btc", base
    if valid_shit_address(base):
        return "shit", base
    if base.lower().startswith(("tb1", "bcrt1")):
        return "testnet", base
    return "invalid", base


def lookup_error_for(kind):
    return {"shit": LOOKUP_ERR_SHIT,
            "testnet": LOOKUP_ERR_TESTNET}.get(kind, LOOKUP_ERR_INVALID)


def render_miner_lookup(state, error=None, address=""):
    # The submitted address is echoed back into the form only, always
    # html.escape()d (it is attacker-controlled input by definition).
    tpl = open(os.path.join(TEMPLATE_DIR, "miner_lookup.html")).read()
    if error:
        block = ('<div style="background:#3a1d1d;border:1px solid #ff6b6b;'
                 'border-radius:10px;padding:14px 18px;margin:0 0 20px;'
                 'color:#ffb3b3" role="alert">%s</div>'
                 % html.escape(error))
    else:
        block = ""
    tpl = tpl.replace("<!--LOOKUP_ERROR-->", block, 1)
    tpl = tpl.replace("__LOOKUP_VALUE__",
                      html.escape(address, quote=True), 1)
    return tpl


class DashHandler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html"):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        # location is built only from a validated BTC address (base58 /
        # bech32 alphabets — no header injection is possible).
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            if path == "/" or path == "/index.html":
                self._send(render_index(self.state))
            elif path == "/payouts":
                self._send(render_payouts(self.state))
            elif path == "/miner" or path == "/miner/":
                # Lookup landing (benchmark R1). The form submits here
                # with ?address=...: a valid BTC address navigates to
                # its miner page; anything else re-renders the landing
                # with a plain-language inline error — never a blank
                # 404 and never a 500.
                addr = (qs.get("address") or [""])[0]
                if not addr.strip():
                    self._send(render_miner_lookup(self.state))
                else:
                    kind, base = lookup_kind(addr)
                    if kind == "btc":
                        self._redirect("/miner/" + base)
                    else:
                        self._send(render_miner_lookup(
                            self.state, error=lookup_error_for(kind),
                            address=addr))
            elif path.startswith("/miner/"):
                miner = urllib.parse.unquote(
                    path[len("/miner/"):].split("/")[0])
                kind, base = lookup_kind(miner)
                if kind == "btc":
                    self._send(render_miner(self.state, base))
                else:
                    self._send(render_miner_lookup(
                        self.state, error=lookup_error_for(kind),
                        address=miner))
            elif path == "/api/stats":
                st = pool_stats()
                s = self.state.status()
                strat = getattr(self.state, "stratum", None)
                ab = strat.abuse_stats() if strat is not None else {}
                # conns_by_ip (miner source IPs) is abuse telemetry for
                # operators, not public data — drop it from the public
                # API (scope H / red-team #8).
                ab = dict(ab)
                ab.pop("conns_by_ip", None)
                self._send(json.dumps({"pool": st, "chains": s,
                                       "abuse": ab,
                                       "tax": "0.002%",
                                       "payouts_enabled":
                                           CFG["PAYOUTS_ENABLED"] == "true"}),
                           "application/json")
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            log("dashboard error: %r" % e)
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass


# ------------------------------------------------------------------- main ---

def poller_loop(state, stratum):
    while True:
        try:
            clean = state.poll()
            if clean:
                stratum.broadcast_job(clean)
        except Exception:
            log("poller exception:\n%s" % traceback.format_exc())
        # Back off while bitcoind is struggling instead of adding a fresh
        # timeout-bound request every 10 s (scope A): 10 s healthy, then
        # 20/40/60 s (capped) as the consecutive-failure streak grows.
        fails = getattr(state, "consecutive_failures", 0)
        if fails:
            time.sleep(min(60, 10 * (2 ** min(fails, 3))))
        else:
            time.sleep(10)


def payout_loop(state):
    while True:
        try:
            payout_tick(state)
        except Exception:
            log("payout exception:\n%s" % traceback.format_exc())
        time.sleep(3600)


def selftest():
    """Offline self-test: job build, share validation, aux math. No daemons."""
    print("== pool selftest ==")
    # merkle branch for 0..7 template txs
    for n in range(8):
        txids = [sha256d(b"tx%d" % i) for i in range(n)]
        br = merkle_branch(txids)
        cb = sha256d(b"coinbase")
        root = cb
        for b in br:
            root = sha256d(root + b)
        assert root == merkle_root([cb] + txids), \
            "merkle branch mismatch with %d txs" % n
    print("merkle_branch OK (0..7 txs)")
    # address decode
    spk = btc_address_to_spk("3FPSdy4dtNpMJxEnrhXMBEm3akEUY3Z592")
    assert spk == TEAM_BTC_SPK, "tax spk mismatch: %s" % spk.hex()
    assert valid_btc_address("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
    assert not valid_btc_address("notanaddress")
    # scope I: testnet bech32 rejected; taproot accepted (harness
    # verifies the P2TR script byte-for-byte with an independent encoder)
    assert not valid_btc_address("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kg3g4ty")
    print("address decode OK (tax spk pinned)")
    # scope E/J/F helpers
    assert valid_shit_address("shit1qkh0zzuxyxnn2lzhezvd3vrgrqw38fa2ej8nmun")
    assert not valid_shit_address("NOT-A-REAL-ADDRESS!!!")
    assert not valid_shit_address("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
    assert sats_to_btc_str(100000) == "0.00100000"
    assert sats_to_btc_str(312493750) == "3.12493750"
    assert sats_to_btc_str(17826466861) == "178.26466861"
    # subsidy schedule: matches the on-chain coinbase measured at
    # SHIT height 77,810 (178.26466861 SHIT, TUNNEL-EVAL §5.4)
    assert shit_get_subsidy(77810) == 17826466861
    assert btc_subsidy_sats(900001) == 312500000
    print("shit address / sats format / subsidy schedule OK")
    # synthetic job
    tmpl = {"height": 900000, "previousblockhash": "00" * 31 + "01",
            "version": 0x20000000, "bits": "17034219",
            "curtime": int(time.time()), "coinbasevalue": 312500000,
            "transactions": []}
    aux = [("SHIT", {"hash_hex": "ab" * 32, "bits": 0x1d00ffff,
                     "target": 2 ** 224, "height": 69000})]
    pool_spk = TEAM_BTC_SPK
    tax = 312500000 * 2 // 100000
    assert tax == 6250, "tax math wrong: %d" % tax
    job = Job(tmpl, aux, pool_spk, TEAM_BTC_SPK, tax, b"/SHIT-Pool/")
    # tax output present?
    assert (struct.pack("<q", tax) + bytes([len(TEAM_BTC_SPK)]) + TEAM_BTC_SPK) in job.coinb2, \
        "TAX OUTPUT MISSING FROM COINBASE"
    assert MM_MAGIC in job.coinb2, "merged-mining header missing"
    print("job build OK (tax=%d sats output present)" % tax)
    # aux branch
    branch, idx = job.aux_branch("SHIT")
    assert idx == 0 and branch == [], "single-aux branch wrong"
    # simulate a miner share at diff 1 (will always pass pool diff)
    en1, en2 = "11223344", "55667788"
    coinbase = job.coinb1 + bytes.fromhex(en1) + bytes.fromhex(en2) + job.coinb2
    v = validate_share(job, en1, en2, job.ntime_hex, "%08x" % 0)
    assert v["coinbase"] == coinbase
    assert sha256d(coinbase) == v["cb_txid"]  # merkle tree uses stripped txid
    assert len(v["header"]) == 80
    print("validate_share OK")
    print("ALL SELFTESTS PASSED")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    db_init()
    log("=== SHIT Pool starting ===")
    log("tax: 0.002%% of every BTC coinbase -> %s (DO NOT REMOVE)"
        % CFG["TAX_BTC_ADDRESS"])
    # timeout=30 + bounded retries (scope A): node3 bitcoind under
    # assumeutxo background validation routinely answers in 5-35 s
    # (measured 2026-10-14); the old 10 s single-attempt timeout failed
    # most polls. Retries apply only to RPC.RETRYABLE read methods —
    # submitblock/sendtoaddress are never retried at this layer.
    btc = RPC(CFG["BTC_RPC_HOST"], CFG["BTC_RPC_PORT"],
              CFG["BTC_RPC_USER"], CFG["BTC_RPC_PASSWORD"], timeout=30,
              retries=2, retry_delay=2.0)
    shit = RPC(CFG["SHIT_RPC_HOST"], CFG["SHIT_RPC_PORT"],
               cookie_path=CFG["SHIT_COOKIE"], wallet=CFG["SHIT_WALLET"],
               timeout=30)
    shit_chain = ShitAuxChain(shit, CFG["NEVM_ZMQ_ADDR"])
    nmc_chain = None
    if CFG["NMC_ENABLED"] == "true":
        nmc_chain = NmcAuxChain(RPC(CFG["NMC_RPC_HOST"], int(CFG["NMC_RPC_PORT"]),
                                   CFG["NMC_RPC_USER"], CFG["NMC_RPC_PASSWORD"], timeout=10))
        log("NMC enabled -> %s:%s" % (CFG["NMC_RPC_HOST"], CFG["NMC_RPC_PORT"]))
    else:
        log("NMC disabled (NMC_ENABLED=false)")
    fb_chain = None
    if CFG["FB_ENABLED"] == "true":
        if not CFG["FB_PAYOUT_ADDRESS"]:
            log("FB enabled but FB_PAYOUT_ADDRESS empty - chain skipped")
        else:
            fb_chain = FbAuxChain(RPC(CFG["FB_RPC_HOST"], int(CFG["FB_RPC_PORT"]),
                                      CFG["FB_RPC_USER"], CFG["FB_RPC_PASSWORD"], timeout=10),
                                  CFG["FB_PAYOUT_ADDRESS"])
            log("FB enabled -> %s:%s" % (CFG["FB_RPC_HOST"], CFG["FB_RPC_PORT"]))
    else:
        log("FB disabled (FB_ENABLED=false)")
    ela_chain = None
    if CFG["ELA_ENABLED"] == "true":
        if not CFG["ELA_PAYOUT_ADDRESS"]:
            log("ELA enabled but ELA_PAYOUT_ADDRESS empty - chain skipped")
        else:
            ela_chain = ElaAuxChain(RPC(CFG["ELA_RPC_HOST"], int(CFG["ELA_RPC_PORT"]),
                                       CFG["ELA_RPC_USER"], CFG["ELA_RPC_PASSWORD"], timeout=10),
                                    CFG["ELA_PAYOUT_ADDRESS"])
            log("ELA enabled -> %s:%s" % (CFG["ELA_RPC_HOST"], CFG["ELA_RPC_PORT"]))
    else:
        log("ELA disabled (ELA_ENABLED=false)")
    sys_chain = None
    if CFG["SYS_ENABLED"] == "true":
        if not CFG["SYS_PAYOUT_ADDRESS"]:
            log("SYS enabled but SYS_PAYOUT_ADDRESS empty - chain skipped")
        else:
            sys_chain = SysAuxChain(RPC(CFG["SYS_RPC_HOST"], int(CFG["SYS_RPC_PORT"]),
                                       CFG["SYS_RPC_USER"], CFG["SYS_RPC_PASSWORD"], timeout=10),
                                    CFG["SYS_PAYOUT_ADDRESS"])
            log("SYS enabled -> %s:%s" % (CFG["SYS_RPC_HOST"], CFG["SYS_RPC_PORT"]))
    else:
        log("SYS disabled (SYS_ENABLED=false)")
    uno_chain = None
    if CFG["UNO_ENABLED"] == "true":
        uno_chain = UnoAuxChain(RPC(CFG["UNO_RPC_HOST"], int(CFG["UNO_RPC_PORT"]),
                                    CFG["UNO_RPC_USER"], CFG["UNO_RPC_PASSWORD"], timeout=10))
        log("UNO enabled -> %s:%s" % (CFG["UNO_RPC_HOST"], CFG["UNO_RPC_PORT"]))
    else:
        log("UNO disabled (UNO_ENABLED=false)")
    state = PoolState(btc, shit_chain, nmc_chain, fb_chain, ela_chain, sys_chain,
                      uno_chain)
    stratum = StratumServer(state, int(CFG["STRATUM_PORT"]))
    state.stratum = stratum  # abuse stats for /api/stats (Squad 6)
    threading.Thread(target=poller_loop, args=(state, stratum), daemon=True).start()
    threading.Thread(target=payout_loop, args=(state,), daemon=True).start()
    DashHandler.state = state
    httpd = ThreadingHTTPServer(("0.0.0.0", int(CFG["HTTP_PORT"])), DashHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("dashboard on 0.0.0.0:%s" % CFG["HTTP_PORT"])
    try:
        stratum.start()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
