#!/usr/bin/env python3
"""shitcoin-stratum-proxy: merged-mining stratum proxy for ShitcoinBtc.

Pool operators point STOCK stratum miners (cpuminer, cgminer, etc.) at this
proxy as if it were a normal coin. The proxy handles ALL AuxPoW work
internally:

  1. Builds the aux block itself (coinbase pays the pool operator's configured
     payout address, mempool txs included, NEVM geth commitment embedded).
  2. Serves standard stratum v1 jobs derived from the merge-mining PARENT
     header (a synthetic parent chain, like the reference miner uses -- no
     bitcoind needed). To the miner the jobs look exactly like Bitcoin jobs.
  3. Validates shares, tracks them per worker (simple vardiff).
  4. On a share meeting the aux network target, assembles the full block
     (aux header + AuxPoW + txs + NEVM data) and submits it to the node.

Consensus is untouched: every submitted block carries a valid AuxPoW and the
post-NEVM commitment, byte-compatible with what the reference miner
(/home/shitcoin/mine.py on the reference node) produces.

The node-RPC layer sits behind the small NodeBackend interface, and NEVM
templates behind NevmTemplateProvider, so the proxy does not hard-wire
syscoin-RPC assumptions -- swapping the backend later does not touch the
stratum or AuxPoW code.

Usage:
  shitcoin-stratum-proxy.py /path/to/proxy.conf
"""
import base64
import binascii
import hashlib
import json
import os
import random
import socket
import socketserver
import struct
import sys
import threading
import time
import urllib.request
from fractions import Fraction

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS = {
    "LISTEN_HOST": "127.0.0.1",
    "LISTEN_PORT": "3333",
    "PAYOUT_ADDRESS": "",
    "NODE_RPC_URL": "http://127.0.0.1:8370/",
    "NODE_COOKIE": "/home/shitcoin/.syscoin/.cookie",
    "NODE_RPC_USER": "",
    "NODE_RPC_PASS": "",
    "NEVM_ZMQ": "tcp://127.0.0.1:9377",
    "SHARE_DIFF": "1",
    "VARDIFF": "1",
    "LOG_FILE": "",
    "JOB_REFRESH_SECS": "30",
}


def load_config(path):
    cfg = dict(DEFAULTS)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    if not cfg["PAYOUT_ADDRESS"]:
        sys.exit("proxy.conf: PAYOUT_ADDRESS is required (your shit1... address)")
    cfg["LISTEN_PORT"] = int(cfg["LISTEN_PORT"])
    cfg["SHARE_DIFF"] = float(cfg["SHARE_DIFF"])
    cfg["VARDIFF"] = cfg["VARDIFF"] not in ("0", "false", "no")
    cfg["JOB_REFRESH_SECS"] = int(cfg["JOB_REFRESH_SECS"])
    return cfg


_log_file = None
_log_lock = threading.Lock()


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    with _log_lock:
        print(line, flush=True)
        if _log_file:
            _log_file.write(line + "\n")
            _log_file.flush()


# ---------------------------------------------------------------------------
# Node backend abstraction.
#
# Syscoin-RPC-backed today. To get off the syscoin RPC, implement this same
# interface against the replacement node -- nothing above this layer changes.
# ---------------------------------------------------------------------------

class NodeBackend:
    def get_height(self):
        raise NotImplementedError

    def get_block_hash(self, height):
        raise NotImplementedError

    def get_block_header(self, blockhash):
        """Return dict with at least 'time' (int) and 'bits' (hex str)."""
        raise NotImplementedError

    def get_mempool(self):
        """Return list of (txid_hex, raw_hex)."""
        raise NotImplementedError

    def submit_block(self, blockhex):
        """Return the node result (None/'' means accepted)."""
        raise NotImplementedError


class SyscoinRPCBackend(NodeBackend):
    def __init__(self, url, cookie_path="", rpc_user="", rpc_pass=""):
        self.url = url
        self.cookie_path = cookie_path
        self.rpc_user = rpc_user
        self.rpc_pass = rpc_pass

    def _auth(self):
        if self.rpc_user:
            return base64.b64encode(
                ("%s:%s" % (self.rpc_user, self.rpc_pass)).encode()).decode()
        with open(self.cookie_path) as f:
            cookie = f.read().strip()
        return base64.b64encode(cookie.encode()).decode()

    def call(self, method, *params):
        body = json.dumps({"jsonrpc": "1.0", "id": "stratum-proxy",
                           "method": method, "params": list(params)}).encode()
        req = urllib.request.Request(self.url, data=body)
        req.add_header("Authorization", "Basic " + self._auth())
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.load(r)
        if out.get("error"):
            raise RuntimeError("RPC %s failed: %s" % (method, out["error"]))
        return out["result"]

    def get_height(self):
        return self.call("getblockcount")

    def get_block_hash(self, height):
        return self.call("getblockhash", height)

    def get_block_header(self, blockhash):
        return self.call("getblockheader", blockhash)

    def get_mempool(self):
        try:
            mp = self.call("getrawmempool", True)
        except Exception as e:
            log("getrawmempool failed: %r" % e)
            return []
        txids = sorted(mp.keys(), key=lambda t: (mp[t].get("ancestorcount", 1), t))
        txs = []
        for txid in txids:
            try:
                raw = self.call("getrawtransaction", txid)
                txs.append((txid, raw))
            except Exception as e:
                log("getrawtransaction %s failed: %r" % (txid, e))
        return txs

    def submit_block(self, blockhex):
        return self.call("submitblock", blockhex)


# ---------------------------------------------------------------------------
# NEVM template provider abstraction (ZMQ to sysgeth today)
# ---------------------------------------------------------------------------

class NevmTemplateProvider:
    def get_template(self):
        """Return (header96, rlp_block)."""
        raise NotImplementedError


class ZmqNevmProvider(NevmTemplateProvider):
    def __init__(self, addr):
        import zmq
        self._zmq = zmq
        self.addr = addr
        self._ctx = None

    def get_template(self):
        if self._ctx is None:
            self._ctx = self._zmq.Context()
        last_err = None
        for attempt in range(3):
            sock = self._ctx.socket(self._zmq.REQ)
            sock.setsockopt(self._zmq.RCVTIMEO, 30000)
            sock.setsockopt(self._zmq.SNDTIMEO, 10000)
            sock.setsockopt(self._zmq.LINGER, 0)
            try:
                sock.connect(self.addr)
                sock.send_multipart([b"nevmblock", b""])
                parts = sock.recv_multipart()
            except Exception as e:
                last_err = e
                time.sleep(1 + attempt)
                continue
            finally:
                sock.close()
            if len(parts) < 4 or parts[0] != b"nevmblock":
                last_err = RuntimeError("bad nevmblock reply")
                time.sleep(1 + attempt)
                continue
            payload = parts[3]
            if len(payload) < 96:
                last_err = RuntimeError("nevmblock payload too short")
                time.sleep(1 + attempt)
                continue
            header96 = payload[0:96]
            off = 96
            ln, vsz = _read_varint(payload, off)
            off += vsz
            rlp_block = payload[off:off + ln]
            if len(rlp_block) != ln:
                last_err = RuntimeError("nevmblock RLP truncated")
                time.sleep(1 + attempt)
                continue
            return header96, rlp_block
        raise RuntimeError("get_nevm_template failed: %r" % (last_err,))


# ---------------------------------------------------------------------------
# Crypto / serialization utils (ported from the reference miner)
# ---------------------------------------------------------------------------

def sha256d(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def _read_varint(buf, off):
    b = buf[off]
    if b < 0xFD:
        return b, 1
    if b == 0xFD:
        return struct.unpack_from("<H", buf, off + 1)[0], 3
    if b == 0xFE:
        return struct.unpack_from("<I", buf, off + 1)[0], 5
    return struct.unpack_from("<Q", buf, off + 1)[0], 9


def _varint(n):
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def merkle_root(leaves):
    if not leaves:
        return b"\x00" * 32
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [sha256d(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


def compact_to_target(bits):
    size = bits >> 24
    word = bits & 0x007FFFFF
    return word << (8 * (size - 3))


def target_to_compact(target):
    size = (target.bit_length() + 7) // 8
    if size <= 3:
        word = target << (8 * (3 - size))
    else:
        word = target >> (8 * (size - 3))
    if word & 0x00800000:
        word >>= 8
        size += 1
    return (size << 24) | (word & 0x007FFFFF)


# --- bech32 decode for shit1... addresses ---
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(vals):
    chk = 1
    GEN = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    for v in vals:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= GEN[i]
    return chk


def _convertbits(data, frombits, tobits, pad):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for v in data:
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def addr_to_scriptpubkey(addr):
    a = addr.lower()
    pos = a.rfind("1")
    assert pos > 0, "bad address"
    hrp, data = a[:pos], a[pos + 1:]
    assert hrp == "shit", "unexpected HRP %s" % hrp
    vals = [CHARSET.index(c) for c in data]
    chk = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    assert _polymod(chk + vals) == 1, "bad bech32 checksum"
    ver = vals[0]
    prog = bytes(_convertbits(vals[1:-6], 5, 8, False))
    assert ver == 0 and len(prog) == 20, "only P2WPKH supported"
    return b"\x00\x14" + prog


def _push_int64(n):
    """CScript::push_int64 -- returns PUSH_OPCODE + number bytes (not bare bytes)."""
    if n == -1 or (1 <= n <= 16):
        return bytes([n + 0x51 - 1])  # OP_1 == 0x51
    neg = n < 0
    n = abs(n)
    r = bytearray()
    while n:
        r.append(n & 0xFF)
        n >>= 8
    if r[-1] & 0x80:
        r.append(0x80 if neg else 0x00)
    elif neg:
        r[-1] |= 0x80
    assert len(r) < 76
    return bytes([len(r)]) + bytes(r)


def _push_data_any(data):
    n = len(data)
    if n < 76:
        return bytes([n]) + data
    if n <= 0xFF:
        return b"\x4c" + bytes([n]) + data
    if n <= 0xFFFF:
        return b"\x4d" + struct.pack("<H", n) + data
    return b"\x4e" + struct.pack("<I", n) + data


COIN = 100_000_000
MINING_SUBSIDY_BASE = 19807185401
SUBSIDY_HALVING_INTERVAL = 1051200

BLOCK_VERSION = 0x20100100
BLOCK_VERSION_NEVM = BLOCK_VERSION | 0x80  # 0x20100180 (AuxPoW chain ID 16)
# Block 88,000 hard fork (v5.0.7): AuxPoW chain ID 16 -> 29.
CHAINID_FORK_HEIGHT = 88000
BLOCK_VERSION_NEVM_FORK = 0x201D0180  # chain ID 29 | VERSION_AUXPOW | VERSION_NEVM
VERSION_AUXPOW = 0x100
NEVM_START_BLOCK = 30316
NEVM_MAGIC = b"nevm"
GENESIS_BITS = 0x1E0FFFFF
SHIT_DIFF_FORK_HEIGHT = 29196
SHIT_DIFF_REBASE_BITS = 0x1d03fffc
SHIT_DIFF_K = 48
SHIT_DIFF_FORK3_HEIGHT = 48460
SHIT_DIFF_FLOOR_BITS = 0x1d63ffff
SHIT_DIFF_FORK2_HEIGHT = 30950
SHIT_DIFF_FORK2_REBASE_BITS = 0x1d63ffff
POW_TARGET_TIMESPAN = 21600

# diff-1 target (Bitcoin definition), used for share difficulty
DIFF1_TARGET = 0x00000000FFFF0000000000000000000000000000000000000000000000000000


def get_subsidy(height):
    n = MINING_SUBSIDY_BASE
    reductions = height // SUBSIDY_HALVING_INTERVAL
    if reductions >= 100:
        return 0
    for _ in range(reductions):
        n -= n // 50
    superblock = int(n * 0.1)
    return n - superblock


def bits_for_height(backend, height):
    """Mirror of the reference miner's bits_for_height (matches src/pow.cpp)."""
    if height == SHIT_DIFF_FORK2_HEIGHT:
        return SHIT_DIFF_FORK2_REBASE_BITS
    if height > SHIT_DIFF_FORK2_HEIGHT:
        tip = backend.get_block_header(backend.get_block_hash(height - 1))
        parent = backend.get_block_header(backend.get_block_hash(height - 2))
        spacing = 30 if height >= NEVM_START_BLOCK else 12
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
        if height >= SHIT_DIFF_FORK3_HEIGHT:
            t = min(t, compact_to_target(SHIT_DIFF_FLOOR_BITS))
        limit = compact_to_target(GENESIS_BITS)
        return target_to_compact(min(t, limit))
    if height == SHIT_DIFF_FORK_HEIGHT:
        return SHIT_DIFF_REBASE_BITS
    if height > SHIT_DIFF_FORK_HEIGHT:
        tip = backend.get_block_header(backend.get_block_hash(height - 1))
        parent = backend.get_block_header(backend.get_block_hash(height - 2))
        spacing = 30 if height >= NEVM_START_BLOCK else 12
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
        limit = compact_to_target(GENESIS_BITS)
        return target_to_compact(min(t, limit))
    tip = backend.get_block_header(backend.get_block_hash(height - 1))
    interval = 720 if height >= NEVM_START_BLOCK else 1800
    if height % interval != 0:
        return int(tip["bits"], 16)
    first = backend.get_block_header(backend.get_block_hash(height - interval))
    timespan = tip["time"] - first["time"]
    timespan = max(POW_TARGET_TIMESPAN // 4, min(POW_TARGET_TIMESPAN * 4, timespan))
    new_target = compact_to_target(int(tip["bits"], 16))
    new_target = new_target * timespan // POW_TARGET_TIMESPAN
    limit = compact_to_target(GENESIS_BITS)
    return target_to_compact(min(new_target, limit))


def ser_coinbase(height, outputs, with_witness, nevm_extra=None):
    s = struct.pack("<i", 1)
    if with_witness:
        s += b"\x00\x01"
    s += b"\x01"
    s += b"\x00" * 32
    s += struct.pack("<I", 0xFFFFFFFF)
    scriptsig = _push_int64(height) + b"\x00"  # BIP34 height + OP_0
    assert len(scriptsig) <= 100
    s += bytes([len(scriptsig)]) + scriptsig
    s += struct.pack("<I", 0xFFFFFFFF)
    s += bytes([len(outputs)])
    for amount, spk in outputs:
        s += struct.pack("<q", amount)
        s += bytes([len(spk)]) + spk
    if with_witness:
        s += b"\x01"
        s += b"\x20" + b"\x00" * 32
    s += struct.pack("<I", 0)
    return s


def witness_commitment_output(extra_txs, nevm_extra):
    wtxids = [b"\x00" * 32] + [sha256d(bytes.fromhex(raw)) for _, raw in extra_txs]
    witness_root = merkle_root(wtxids)
    commitment = sha256d(witness_root + b"\x00" * 32)
    spk = b"\x6a\x24\xaa\x21\xa9\xed" + commitment
    spk += _push_data_any(nevm_extra)
    return spk

# ---------------------------------------------------------------------------
# Block building: aux block + synthetic parent template.
#
# The aux block is built exactly like the reference miner builds it. The
# parent is synthetic (no bitcoind needed): its coinbase commits to the aux
# block hash with the standard merge-mining tag, and miners grind the parent
# header via stratum. Consensus validates the PARENT header's PoW against the
# aux target -- the same rule the reference miner relies on.
# ---------------------------------------------------------------------------

# Parent coinbase scriptSig layout for stratum:
#   fabe6d6d || aux_hash(32, display order) || 0x01000000 || 0x00000000
#     || extranonce1(4) || extranonce2(8)   = 56 bytes total
AUX_TAG = bytes.fromhex("fabe6d6d")
EXTRANONCE1_SIZE = 4
EXTRANONCE2_SIZE = 8
PARENT_SCRIPTSIG_LEN = 4 + 32 + 4 + 4 + EXTRANONCE1_SIZE + EXTRANONCE2_SIZE  # 56


class Job:
    """One mining job: a fully-built aux block plus its parent template."""
    def __init__(self, job_id, height, bits, target, ntime,
                 aux_header80, txs, nevm_rlp,
                 coinb1, coinb2, aux_hash_display):
        self.job_id = job_id
        self.height = height
        self.bits = bits
        self.target = target
        self.ntime = ntime
        self.aux_header80 = aux_header80
        self.txs = txs
        self.nevm_rlp = nevm_rlp
        self.coinb1 = coinb1      # bytes: parent coinbase prefix
        self.coinb2 = coinb2      # bytes: parent coinbase suffix
        self.aux_hash_display = aux_hash_display
        self.created = time.time()
        self.shares = 0
        self.blocks_found = 0


def build_job(backend, nevm, payout_spk, job_id):
    """Build a fresh aux block + parent stratum template. Returns Job."""
    height = backend.get_height() + 1
    prev_hash = backend.get_block_hash(height - 1)
    bits = bits_for_height(backend, height)
    target = compact_to_target(bits)
    ntime = int(time.time())

    # NEVM template (mandatory post-30316: carries the geth commitment)
    nevm_header96, nevm_rlp = nevm.get_template()
    nevm_extra = NEVM_MAGIC + nevm_header96
    assert len(nevm_extra) == 100

    mempool = backend.get_mempool()
    outputs = [(get_subsidy(height), payout_spk)]
    wit_spk = witness_commitment_output(mempool, nevm_extra)
    outputs.append((0, wit_spk))
    coinbase = ser_coinbase(height, outputs, with_witness=True)
    coinbase_txid = sha256d(ser_coinbase(height, outputs, with_witness=False))
    txs = [coinbase] + [bytes.fromhex(raw) for _, raw in mempool]
    leaf_ids = [coinbase_txid] + [bytes.fromhex(txid)[::-1] for txid, _ in mempool]
    root = merkle_root(leaf_ids)

    # Height-gated AuxPoW chain ID: 16 before block 88,000, 29 from 88,000 on.
    # Stamping the wrong era's ID makes the daemon reject the block (bad-chainid).
    block_version = BLOCK_VERSION_NEVM_FORK if height >= CHAINID_FORK_HEIGHT \
        else BLOCK_VERSION_NEVM
    aux_header80 = (struct.pack("<i", block_version)
                    + bytes.fromhex(prev_hash)[::-1]
                    + root
                    + struct.pack("<III", ntime, bits, 0))
    aux_hash_display = sha256d(aux_header80)[::-1].hex()

    # Parent coinbase template. Miners fill extranonce1+extranonce2 after the
    # fixed 44-byte aux commitment.
    fixed = AUX_TAG + bytes.fromhex(aux_hash_display) + struct.pack("<I", 1) \
        + struct.pack("<I", 0)
    assert len(fixed) == 44
    coinb1 = (struct.pack("<i", 1) + b"\x01" + b"\x00" * 32
              + struct.pack("<I", 0xFFFFFFFF)
              + bytes([PARENT_SCRIPTSIG_LEN]) + fixed)
    coinb2 = (struct.pack("<I", 0xFFFFFFFF) + b"\x01"
              + struct.pack("<q", 0) + b"\x01\x6a"
              + struct.pack("<I", 0))

    log("job %s: height=%d bits=%08x target~2^%d mempool=%d" %
        (job_id, height, bits, target.bit_length(), len(mempool)))
    return Job(job_id, height, bits, target, ntime,
               aux_header80, txs, nevm_rlp, coinb1, coinb2, aux_hash_display)


def assemble_and_submit(backend, job, parent_coinbase, parent_header):
    """Assemble the full AuxPoW block from a winning share and submit it."""
    parent_hash = sha256d(parent_header)  # internal byte order
    # auxpow: tx | parentHash | vMerkleBranch(empty) | nIndex |
    #         vChainMerkleBranch(empty) | nChainIndex | parentHeader
    auxpow = (parent_coinbase + parent_hash + b"\x00" + struct.pack("<I", 0)
              + b"\x00" + struct.pack("<I", 0) + parent_header)
    block = (job.aux_header80 + auxpow + _varint(len(job.txs))
             + b"".join(job.txs) + _varint(len(job.nevm_rlp)) + job.nevm_rlp)
    # Tripwire (mirrors the reference miner): never submit without AuxPoW.
    version = struct.unpack("<i", block[0:4])[0]
    if not (version & VERSION_AUXPOW) or len(block) < 80 + 200:
        raise RuntimeError("refusing to submit: auxpow missing")
    res = backend.submit_block(block.hex())
    return res


class JobManager:
    """Holds the current job; rebuilds on new tip / timer / block found."""
    def __init__(self, backend, nevm, payout_spk, refresh_secs):
        self.backend = backend
        self.nevm = nevm
        self.payout_spk = payout_spk
        self.refresh_secs = refresh_secs
        self.lock = threading.Lock()
        self.job = None
        self.job_counter = 0
        self.listeners = []  # callbacks(job, clean) on new job
        self.last_tip = None

    def add_listener(self, fn):
        with self.lock:
            self.listeners.append(fn)

    def current(self):
        with self.lock:
            return self.job

    def _broadcast(self, job, clean):
        for fn in list(self.listeners):
            try:
                fn(job, clean)
            except Exception as e:
                log("listener error: %r" % e)

    def _new_job_locked(self, clean):
        self.job_counter += 1
        job_id = "%08x" % self.job_counter
        job = build_job(self.backend, self.nevm, self.payout_spk, job_id)
        self.job = job
        listeners = list(self.listeners)
        for fn in listeners:
            try:
                fn(job, clean)
            except Exception as e:
                log("listener error: %r" % e)
        return job

    def new_job(self, clean=True):
        with self.lock:
            return self._new_job_locked(clean)

    def watch_loop(self):
        """Poll the tip; rebuild the job when the chain moves or it ages."""
        while True:
            try:
                tip = self.backend.get_height()
                with self.lock:
                    job = self.job
                if self.last_tip is None:
                    self.last_tip = tip
                if tip != self.last_tip:
                    log("new tip %d (was %s): rebuilding job" % (tip, self.last_tip))
                    self.last_tip = tip
                    self.new_job(clean=True)
                elif job is not None and time.time() - job.created > self.refresh_secs:
                    self.new_job(clean=True)
                elif job is None:
                    self.last_tip = tip
                    self.new_job(clean=True)
            except Exception as e:
                log("watch_loop error: %r" % e)
            time.sleep(5)

# ---------------------------------------------------------------------------
# Stratum v1 server.
#
# Standard protocol: mining.subscribe / mining.authorize / mining.notify /
# mining.submit / mining.set_difficulty. Jobs look like plain Bitcoin jobs;
# the miner never sees anything merge-mining-specific.
# ---------------------------------------------------------------------------

def _send(sock_file, obj):
    data = (json.dumps(obj) + "\n").encode()
    return data


class StratumHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.server.register(self)
        extranonce1 = os.urandom(EXTRANONCE1_SIZE)
        worker = "unknown"
        difficulty = self.server.share_diff
        # Exact integer math (no float precision loss on the 256-bit target).
        share_target = int(Fraction(DIFF1_TARGET, 1) /
                           Fraction(difficulty).limit_denominator(10**9))
        last_diff_adjust = time.time()
        shares_since_adjust = 0
        authorized = False

        def send(obj):
            try:
                self.wfile.write(_send(None, obj))
                self.wfile.flush()
            except Exception:
                pass

        def send_job(job, clean):
            send({"id": None, "method": "mining.notify", "params": [
                job.job_id,
                "00" * 32,                       # prevhash (synthetic parent)
                job.coinb1.hex(),
                job.coinb2.hex(),
                [],                              # merkle_branch (single tx)
                "00000001",                      # version
                "%08x" % job.bits,               # nbits (aux network bits)
                "%08x" % job.ntime,              # ntime
                clean,
            ]})

        def push_difficulty(d):
            send({"id": None, "method": "mining.set_difficulty",
                  "params": [d]})

        self.server.job_manager.add_listener(
            lambda job, clean: send_job(job, clean))
        job = self.server.job_manager.current()
        if job is None:
            job = self.server.job_manager.new_job(clean=True)

        try:
            for raw in self.rfile:
                try:
                    msg = json.loads(raw.decode().strip())
                except Exception:
                    continue
                mid = msg.get("id")
                method = msg.get("method")
                params = msg.get("params", [])

                if method == "mining.subscribe":
                    send({"id": mid, "error": None, "result": [
                        [["mining.notify", "ae6812eb4cd098f"]],
                        extranonce1.hex(),
                        EXTRANONCE2_SIZE,
                    ]})
                    push_difficulty(difficulty)
                    send_job(job, True)
                elif method == "mining.authorize":
                    worker = str(params[0]) if params else "unknown"
                    authorized = True
                    log("authorize %s from %s" % (worker, self.client_address[0]))
                    send({"id": mid, "error": None, "result": True})
                elif method == "mining.configure":
                    send({"id": mid, "error": None, "result": {}})
                elif method == "mining.submit":
                    ok, is_block, info = self.server.check_share(
                        self, worker, extranonce1, params, share_target)
                    if ok and is_block:
                        send({"id": mid, "error": None, "result": True})
                    elif ok:
                        send({"id": mid, "error": None, "result": True})
                    else:
                        send({"id": mid, "error": [21, info, None],
                              "result": None})
                    if ok:
                        shares_since_adjust += 1
                        if (self.server.vardiff and shares_since_adjust >= 30
                                and time.time() - last_diff_adjust > 60):
                            elapsed = time.time() - last_diff_adjust
                            # aim: ~1 share per 15s
                            new_diff = difficulty * (elapsed / (15 * shares_since_adjust))
                            new_diff = max(0.001, min(1000.0, new_diff))
                            if abs(new_diff - difficulty) / difficulty > 0.2:
                                difficulty = new_diff
                                share_target = int(Fraction(DIFF1_TARGET, 1) /
                                                   Fraction(difficulty).limit_denominator(10**9))
                                push_difficulty(difficulty)
                                log("vardiff %s -> %.4g" % (worker, difficulty))
                            shares_since_adjust = 0
                            last_diff_adjust = time.time()
                else:
                    send({"id": mid, "error": [20, "unknown method", None],
                          "result": None})
                job = self.server.job_manager.current() or job
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.server.unregister(self)

    def finish(self):
        try:
            super().finish()
        except Exception:
            pass


class StratumServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, job_manager, backend, share_diff, vardiff):
        self.job_manager = job_manager
        self.backend = backend
        self.share_diff = share_diff
        self.vardiff = vardiff
        self.handlers = set()
        self.handlers_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.stats = {"shares": 0, "rejects": 0, "blocks": 0}
        super().__init__(addr, handler)

    def register(self, h):
        with self.handlers_lock:
            self.handlers.add(h)

    def unregister(self, h):
        with self.handlers_lock:
            self.handlers.discard(h)

    def check_share(self, handler, worker, extranonce1, params, share_target):
        """Validate a mining.submit. Returns (ok, is_block, info)."""
        try:
            _, job_id, extranonce2_hex, ntime_hex, nonce_hex = params[:5]
        except (ValueError, TypeError):
            return False, False, "bad params"
        job = self.job_manager.current()
        if job is None or job.job_id != job_id:
            with self.stats_lock:
                self.stats["rejects"] += 1
            return False, False, "stale job"
        try:
            extranonce2 = bytes.fromhex(extranonce2_hex)
            if len(extranonce2) != EXTRANONCE2_SIZE:
                return False, False, "bad extranonce2 size"
            ntime = int(ntime_hex, 16)
            nonce = int(nonce_hex, 16)
        except (ValueError, binascii.Error):
            return False, False, "bad hex"
        if not (job.ntime <= ntime <= job.ntime + 600):
            return False, False, "ntime out of range"

        coinbase = job.coinb1 + extranonce1 + extranonce2 + job.coinb2
        txid = sha256d(coinbase)  # single-tx parent: merkle root = txid
        # NOTE: nbits here MUST be job.bits -- byte-identical to the header
        # the miner hashed (it came from our mining.notify). Consensus does
        # not validate the parent nBits (only the hash vs the aux target),
        # but share validation requires the exact bytes.
        header = (struct.pack("<i", 1) + b"\x00" * 32 + txid
                  + struct.pack("<I", ntime) + struct.pack("<I", job.bits)
                  + struct.pack("<I", nonce))
        hv = int.from_bytes(sha256d(header), "little")
        if hv > share_target:
            with self.stats_lock:
                self.stats["rejects"] += 1
            return False, False, "low difficulty share"

        with self.stats_lock:
            self.stats["shares"] += 1
        job.shares += 1

        if hv <= job.target:
            # WINNER: assemble the full AuxPoW block and submit.
            log("BLOCK by %s job=%s height=%d hash=%s" %
                (worker, job_id, job.height,
                 sha256d(header)[::-1].hex()[:16]))
            try:
                res = assemble_and_submit(self.backend, job, coinbase, header)
            except Exception as e:
                log("submit failed: %r" % e)
                return False, False, "submit failed: %s" % e
            with self.stats_lock:
                self.stats["blocks"] += 1
            job.blocks_found += 1
            accepted = res in (None, "", "inconclusive")
            log("submitblock height=%d -> %r (%s)" %
                (job.height, res, "ACCEPTED" if accepted else "REJECTED?"))
            # New job immediately so nobody mines stale work.
            self.job_manager.new_job(clean=True)
            return True, True, "block"
        return True, False, "share"


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: shitcoin-stratum-proxy.py /path/to/proxy.conf")
    cfg = load_config(sys.argv[1])

    global _log_file
    if cfg["LOG_FILE"]:
        _log_file = open(cfg["LOG_FILE"], "a")

    backend = SyscoinRPCBackend(cfg["NODE_RPC_URL"], cfg["NODE_COOKIE"],
                                cfg["NODE_RPC_USER"], cfg["NODE_RPC_PASS"])
    nevm = ZmqNevmProvider(cfg["NEVM_ZMQ"])
    payout_spk = addr_to_scriptpubkey(cfg["PAYOUT_ADDRESS"])
    log("payout address OK: %s" % cfg["PAYOUT_ADDRESS"])

    # Smoke-test the backends before opening the stratum port.
    h = backend.get_height()
    log("node height: %d" % h)
    h96, rlp = nevm.get_template()
    log("nevm template OK: header96=%d bytes rlp=%d bytes" % (len(h96), len(rlp)))

    jm = JobManager(backend, nevm, payout_spk, cfg["JOB_REFRESH_SECS"])
    jm.new_job(clean=True)

    server = StratumServer((cfg["LISTEN_HOST"], cfg["LISTEN_PORT"]),
                           StratumHandler, jm, backend,
                           cfg["SHARE_DIFF"], cfg["VARDIFF"])
    threading.Thread(target=jm.watch_loop, daemon=True).start()

    def stats_loop():
        while True:
            time.sleep(300)
            with server.stats_lock:
                s = dict(server.stats)
            job = jm.current()
            log("stats: shares=%d rejects=%d blocks=%d job=%s height=%s" %
                (s["shares"], s["rejects"], s["blocks"],
                 job.job_id if job else "-", job.height if job else "-"))
    threading.Thread(target=stats_loop, daemon=True).start()

    log("stratum proxy listening on %s:%d (share diff %g)" %
        (cfg["LISTEN_HOST"], cfg["LISTEN_PORT"], cfg["SHARE_DIFF"]))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
