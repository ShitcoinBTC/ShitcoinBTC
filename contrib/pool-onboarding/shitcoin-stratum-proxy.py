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
  shitcoin-stratum-proxy.py [--spool-dir DIR] [--submit-retries N]
                            [--submit-backoff-secs S] [--alarm-cmd CMD]
                            /path/to/proxy.conf

Submission safety (fix-push 2026-10-26, fix-round 2026-10-26):
  * A daemon-REJECTED submitblock is a FAILURE, never a found block: the
    miner is answered with a stratum error, the block counters are not
    incremented, and the block is not acked True. (Previously any
    non-exception submitblock result -- including a rejection string --
    was counted as a found block and acked True.)
  * A rejected/failed submission is retried a bounded number of times
    (SUBMIT_RETRIES, default 3 retries after the first attempt) with the
    SAME block payload -- a found block is bound to its prevhash, so a
    "fresh template" can never replace it. Between attempts the retry
    loop runs only a cheap probe (one getblockcount); it NEVER rebuilds
    miner jobs (a clean rebuild resets every connected miner's work).
    If the probe shows the tip has moved to/past the block's height the
    block can never land, so the retry aborts immediately into the
    spool + alarm path.
  * Every found block is spooled to disk (SPOOL_DIR, default ./spool)
    BEFORE the first submission attempt, replayed on startup and
    periodically thereafter, and removed only after daemon-confirmed
    acceptance. A spool WRITE failure never blocks the submission: it
    logs ERROR + raises the alarm, then submits anyway. Spool entries
    are validated on read (required keys, hex-decodable payload, size
    cap, sane height); an invalid entry is quarantined to <name>.bad
    with one alarm and can never crash or head-of-line-block replay.
  * "inconclusive" / "duplicate" submitblock results are AMBIGUOUS:
    they count as acceptance ONLY after an on-chain confirmation
    (getblockhash(height) == the block's hash). Unconfirmed entries
    stay spooled and keep replaying.
  * Freeze v2.3 (comparative report R3): an OPTIONAL second submit
    endpoint, SUBMIT_FALLBACK_RPC_URL (empty by default = exactly the
    pre-v2.3 single-daemon behaviour). When set and the primary
    sequence ends without confirmed acceptance, the SAME blockhex is
    submitted to the secondary daemon (e.g. a house/seed node) with
    the same bounded retry, and acceptance on EITHER daemon -- outright,
    or an ambiguous result confirmed on-chain against whichever daemon
    can answer -- ends the sequence as accepted. The one exception:
    a DETERMINISTIC rejection from the primary (a real BIP22 reject
    reason string, e.g. "bad-prevblk" -- see
    is_deterministic_submit_reject()) is NOT sprayed at the
    secondary: the payload itself is invalid, so a second daemon
    applying the same consensus rules would reject it too, and
    spraying it only muddies the secondary's logs. Inconclusive /
    duplicate / timeout / transport / RPC-error outcomes carry no
    such proof and DO fall through to the secondary.
  * A submission that still fails after all retries fires a loud ALARM:
    an ALARM log/stderr line, an ALARM.txt file in the spool directory,
    and the optional ALARM_CMD hook. Alarm discipline is per-entry:
    ALARM_CMD fires at most once per spool entry, re-alarming only on
    escalation intervals of >= 1 hour (alarmed_at is persisted in the
    entry). ALARM.txt is rotated to ALARM.txt.resolved when its entry
    is confirmed accepted. An entry whose height is provably buried
    (tip at/past its height, a different hash on-chain, after a bounded
    number of replay passes) is dead-lettered to <name>.dead -- kept
    on disk, but no longer submitted or alarmed.
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
import subprocess
import sys
import tempfile
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
    # Freeze v2.3 (R3): optional SECOND submit endpoint. Empty (the
    # default) = single-daemon behaviour, byte-for-byte as before.
    # Auth for the fallback: SUBMIT_FALLBACK_RPC_USER/PASS when set,
    # otherwise the primary NODE_COOKIE / NODE_RPC_* credentials.
    "SUBMIT_FALLBACK_RPC_URL": "",
    "SUBMIT_FALLBACK_RPC_USER": "",
    "SUBMIT_FALLBACK_RPC_PASS": "",
    "NODE_COOKIE": "/home/shitcoin/.syscoin/.cookie",
    "NODE_RPC_USER": "",
    "NODE_RPC_PASS": "",
    "NEVM_ZMQ": "tcp://127.0.0.1:9377",
    "SHARE_DIFF": "1",
    "VARDIFF": "1",
    "LOG_FILE": "",
    "JOB_REFRESH_SECS": "30",
    # Submission safety (see header). SPOOL_DIR="" disables spooling.
    "SPOOL_DIR": "./spool",
    "SUBMIT_RETRIES": "3",
    "SUBMIT_BACKOFF_SECS": "2",
    "ALARM_CMD": "",
    "SPOOL_REPLAY_SECS": "60",
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
    cfg["SUBMIT_RETRIES"] = int(cfg["SUBMIT_RETRIES"])
    cfg["SUBMIT_BACKOFF_SECS"] = float(cfg["SUBMIT_BACKOFF_SECS"])
    cfg["SPOOL_REPLAY_SECS"] = int(cfg["SPOOL_REPLAY_SECS"])
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
# Submission safety: acceptance test, disk spool, alarm, retrying submit.
#
# A found block is the one thing this proxy must never lose silently.
# submitblock semantics (Bitcoin/Syscoin-derived daemon): a null/empty
# result means accepted; "inconclusive" means the daemon accepted it for
# validation; any other string is a REJECTION reason. An RPC error reply
# (e.g. -8 "block hash unknown") reaches us as an exception from call().
# ---------------------------------------------------------------------------

# Results that mean the daemon accepted the block outright.
ACCEPTED_SUBMIT_RESULTS = (None, "")
# Results that are AMBIGUOUS: the daemon did not reject the block, but
# neither string proves the block is on the active chain ("duplicate"
# only proves the daemon has SEEN the block, possibly on a losing
# branch). They count as acceptance ONLY after the on-chain
# confirmation in _confirm_on_chain() (attacker B4).
AMBIGUOUS_SUBMIT_RESULTS = ("inconclusive", "duplicate",
                            "duplicate-inconclusive")

# Alarm discipline (attacker B3): ALARM_CMD fires at most once per
# spool entry; re-alarm only after this escalation interval.
ALARM_ESCALATION_SECS = 3600
# Dead-lettering (attacker B3): a spooled entry is submitted on at most
# this many replay passes; after that, if its height is provably buried
# (tip at/past the height AND a different hash on-chain there), it is
# renamed to <name>.dead and neither submitted nor alarmed again.
DEAD_LETTER_AFTER_PASSES = 3
# Spool entry sanity caps (attacker B2): a block payload larger than
# this, or a spool file larger than this, is invalid by construction.
MAX_BLOCKHEX_CHARS = 8000000
MAX_SPOOL_FILE_BYTES = MAX_BLOCKHEX_CHARS + 65536
MAX_SPOOL_HEIGHT = 100000000


def is_submit_accepted(res):
    """True only for an outright daemon-confirmed acceptance.
    Ambiguous results ("inconclusive"/"duplicate") are NOT accepted
    here -- see is_submit_ambiguous() and _confirm_on_chain()."""
    return res in ACCEPTED_SUBMIT_RESULTS


def is_submit_ambiguous(res):
    """True for results that need an on-chain confirmation (B4)."""
    return res in AMBIGUOUS_SUBMIT_RESULTS


def is_deterministic_submit_reject(res):
    """Freeze v2.3 (R3): True only for a DETERMINISTIC rejection --
    a real BIP22 reject-reason string from submitblock (any string
    result that is neither an outright acceptance nor one of the
    AMBIGUOUS results, e.g. "bad-prevblk", "duplicate-invalid",
    "high-hash"). Such a result is the daemon's reasoned verdict on
    the payload itself: the same payload is invalid under the same
    consensus rules on any daemon, so it must NOT be sprayed at a
    secondary submit endpoint. Everything else is NOT deterministic:
    accepted/ambiguous strings, exceptions (timeout, transport, or
    an RPC error reply such as -8 raised by call() -- a second
    daemon's template cache may well know the block), and any
    non-string payload."""
    return isinstance(res, str) and bool(res) \
        and not is_submit_accepted(res) and not is_submit_ambiguous(res)


# --- INTERFACE-CONTRACT v1.0 compliance (freeze v2.3, seat-10 fixes) --
# NEVM *transport* reasons (contract section 4): the daemon could not
# judge the block; these map to outcome "unavailable", never "invalid".
NEVM_TRANSPORT_REASONS = ("nevm-response-not-found",
                          "nevm-connect-not-sent",
                          "nevm-not-connected",
                          "nevm-header-not-sent")
# BIP22 / validation verdict reasons that are not "bad-*" shaped.
_BIP22_INVALID_EXACT = ("high-hash", "duplicate-invalid",
                        "missing-auxpow", "no-auxpow")


def is_bip22_invalid(res):
    """Contract section 4 mapping, outcome "invalid" (S10-D3/D5):
    True only for a submitblock result string that IS a consensus /
    validation verdict on this payload -- a "bad-*" BIP22 reason, one
    of _BIP22_INVALID_EXACT, or a NEVM *verdict* reason ("nevm-*"
    except the NEVM_TRANSPORT_REASONS, which are "unavailable").
    Such a verdict is terminal: never-retry (contract row 6) -- the
    submit sequence breaks on the FIRST such answer, so a verdict can
    never be followed by (or poisoned by) a later timeout (S10-D5).
    Any other string (synthetic harness strings, prose, stale /
    prevhash class answers such as "inconclusive-not-best-prevblk")
    is NOT a mapped invalid verdict here."""
    if not isinstance(res, str) or not res:
        return False
    if res in NEVM_TRANSPORT_REASONS:
        return False
    if res in ("bad-prevblk", "prev-blk-not-found"):
        # Contract section 4: the prevhash class maps to
        # "stale-prevhash" (retryable-then-terminal), NOT "invalid"
        # -- it is not a verdict on this payload's own validity.
        return False
    if res.startswith("bad-") or res in _BIP22_INVALID_EXACT:
        return True
    if res.startswith("nevm-"):
        return True
    return False


# Contract C7: ambiguous / transient / unconfirmed outcomes WARN
# immediately and ALARM only at ~90 s if still unresolved. Terminal
# outcomes (invalid, malformed, provably buried) alarm immediately.
ALARM_DELAY_SECS = 90.0


def _confirm_on_chain_any(backends, blockhex, height):
    """On-chain confirmation with ANY-TRUE semantics (S10-D6, mirror
    of the adapter's _block_on_chain): a True from ANY backend wins
    -- a block either daemon can verify on its active chain IS
    confirmed. False is returned only when every backend that could
    answer said False (a different hash at the height); None when no
    backend could answer (fail-closed: unconfirmed). The pre-fix
    first-answer-wins form let one daemon's False shadow the other
    daemon's True for a block the second daemon had just accepted."""
    saw_false = False
    for backend in backends:
        if backend is None:
            continue
        confirmed = _confirm_on_chain(backend, blockhex, height)
        if confirmed is True:
            return True
        if confirmed is False:
            saw_false = True
    return False if saw_false else None


def block_hash_hex(blockhex):
    """Display-order block hash (double-SHA256, reversed) of a block.

    A block hash is the double-SHA256 of the 80-byte HEADER only
    (version, prevhash, merkle root, time, bits, nonce) -- never of
    the whole serialization. Hashing the full block can never match
    the daemon's getblockhash, which silently broke on-chain
    confirmation of landed blocks (PLUS25 agent 12, H1/X-1 CRITICAL).
    Proven against a live daemon block: dsha256(header) equals
    getblockhash; dsha256(whole block) does not."""
    raw = bytes.fromhex(blockhex)
    digest = hashlib.sha256(hashlib.sha256(raw[:80]).digest()).digest()
    return digest[::-1].hex()


def _safe_tip(backend):
    """Chain tip height, or None when the backend cannot tell us."""
    try:
        return int(backend.get_height())
    except Exception:
        return None


def _safe_chain_hash(backend, height):
    """getblockhash(height) lower-cased, or None when unavailable."""
    try:
        fn = getattr(backend, "get_block_hash", None)
        if fn is None:
            return None
        val = fn(height)
        return str(val).lower() if val else None
    except Exception:
        return None


def _confirm_on_chain(backend, blockhex, height):
    """On-chain confirmation for ambiguous submit results (B4).

    Returns True when getblockhash(height) equals the block's hash,
    False when the daemon reports a DIFFERENT hash at that height,
    and None when the check cannot be made (treated as unconfirmed:
    the fail-closed choice -- the entry stays spooled)."""
    chain_hash = _safe_chain_hash(backend, height)
    if chain_hash is None:
        return None
    try:
        return chain_hash == block_hash_hex(blockhex)
    except Exception:
        return None


def _looks_like_stale_template(detail):
    s = str(detail)
    return "-8" in s or "block hash unknown" in s or "stale" in s.lower()


class BlockSpool:
    """On-disk spool for found blocks: one JSON file per block.

    store() is idempotent (same block -> same file, never duplicated),
    entries are removed only via discard(), which the submit path
    calls solely after daemon-confirmed acceptance, and every entry
    is validated on read (attacker B2): an invalid entry is
    quarantined to <name>.bad with one alarm and never crashes or
    blocks the replay of later entries. Filenames carry the FULL
    block hash (no truncation) and writes go through a unique temp
    file + fsync + os.replace + directory fsync, so a crash or a
    concurrent writer can neither tear an entry nor lose the rename.
    The spool directory must be owned by the service user with
    permissions 0700 -- it is part of the security model.
    """

    def __init__(self, spool_dir):
        self.dir = spool_dir
        os.makedirs(self.dir, exist_ok=True)

    def _path_for(self, blockhex, height):
        name = "block-%d-%s.json" % (height, block_hash_hex(blockhex))
        return os.path.join(self.dir, name)

    def _fsync_dir(self):
        try:
            fd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:
            pass

    def _atomic_write_json(self, path, entry):
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(entry, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        self._fsync_dir()

    def store(self, blockhex, height):
        """Write the block to the spool BEFORE any submission attempt."""
        path = self._path_for(blockhex, height)
        if os.path.exists(path):
            return path  # already spooled: idempotent, keep the original
        entry = {"height": height, "blockhex": blockhex,
                 "stored_at": time.time()}
        self._atomic_write_json(path, entry)
        log("spooled found block height=%d -> %s" % (height, path))
        return path

    def _read_entry_file(self, path):
        with open(path) as f:
            return json.load(f)

    def update_entry(self, path, **fields):
        """Persist bookkeeping fields (alarmed_at, replay_passes, ...)
        on an existing entry, atomically."""
        entry = self._read_entry_file(path)
        entry.pop("_path", None)
        entry.update(fields)
        self._atomic_write_json(path, entry)
        return entry

    def discard(self, blockhex, height):
        """Remove a spooled block. Call ONLY after confirmed acceptance.
        Also removes this entry's line from the ALARM.txt pending
        list (B3, per-entry since the C2-D1 fix)."""
        try:
            path = self._path_for(blockhex, height)
        except Exception:
            return
        try:
            os.remove(path)
            self._fsync_dir()
        except FileNotFoundError:
            pass  # already gone: replay stays idempotent
        self._rotate_alarm_for(height, blockhex)

    def _rotate_alarm_for(self, height, blockhex=None):
        """Remove THIS entry's line from the ALARM.txt pending list
        (C4 fix, C2-D1). When other failures are still pending their
        lines stay; only when the last pending line is removed is the
        file rotated to ALARM.txt.resolved, as before."""
        alarm_path = os.path.join(self.dir, "ALARM.txt")
        lines = _alarm_file_read(self.dir)
        if not lines:
            return
        markers = ["height=%s " % (height,)]
        if blockhex:
            try:
                markers.append(block_hash_hex(blockhex))
            except Exception:
                pass
        remaining = [ln for ln in lines
                     if not any(m in ln for m in markers)]
        if len(remaining) == len(lines):
            return  # the alarms on file are for other entries: keep them
        if remaining:
            try:
                _alarm_file_write(self.dir, remaining)
                log("spool: ALARM.txt entry removed (height=%s confirmed "
                    "accepted); %d failure(s) still pending"
                    % (height, len(remaining)))
            except OSError as e:
                log("spool: could not update ALARM.txt: %r" % e)
            return
        try:
            os.replace(alarm_path, alarm_path + ".resolved")
            self._fsync_dir()
            log("spool: ALARM.txt rotated to ALARM.txt.resolved "
                "(height=%s confirmed accepted)" % (height,))
        except OSError as e:
            log("spool: could not rotate ALARM.txt: %r" % e)

    def dead_letter(self, entry):
        """Rename a provably buried entry to <name>.dead (B3): the file
        is KEPT as evidence, but replay never submits or alarms it."""
        path = entry.get("_path")
        if not path:
            try:
                path = self._path_for(entry["blockhex"], entry["height"])
            except Exception:
                return None
        dead = path + ".dead"
        try:
            os.replace(path, dead)
            self._fsync_dir()
        except OSError as e:
            log("spool: could not dead-letter %s: %r" % (path, e))
            return None
        log("spool: dead-lettered buried entry height=%s -> %s "
            "(kept on disk; no longer submitted or alarmed)" %
            (entry.get("height"), dead))
        return dead

    def _quarantine(self, path, reason):
        """Move an invalid entry aside to <name>.bad + one alarm (B2)."""
        bad = path + ".bad"
        try:
            os.replace(path, bad)
            self._fsync_dir()
        except OSError as e:
            log("*** ALARM *** spool: could not quarantine invalid "
                "entry %s (%s): %r" % (path, reason, e))
            return
        msg = ("spool: quarantined invalid entry file=%s reason=%s "
               "renamed=%s" % (os.path.basename(path), reason,
                               os.path.basename(bad)))
        log("*** ALARM *** %s" % msg)
        print("*** ALARM *** %s" % msg, file=sys.stderr, flush=True)
        try:
            with open(os.path.join(self.dir, "ALARM.txt"), "a") as f:
                f.write("[%s] ALARM %s\n" %
                        (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception as e:
            log("ALARM: could not write alarm file: %r" % e)

    @staticmethod
    def _validate_entry(entry):
        """Return an error string, or None when the entry is sane (B2)."""
        if not isinstance(entry, dict):
            return "entry is not a JSON object"
        for key in ("height", "blockhex", "stored_at"):
            if key not in entry:
                return "missing required key %r" % key
        height = entry["height"]
        if isinstance(height, bool) or not isinstance(height, int):
            return "height is not an int"
        if height < 0 or height > MAX_SPOOL_HEIGHT:
            return "height %r out of sane range" % (height,)
        blockhex = entry["blockhex"]
        if not isinstance(blockhex, str) or not blockhex:
            return "blockhex is not a non-empty string"
        if len(blockhex) > MAX_BLOCKHEX_CHARS:
            return "blockhex exceeds size cap"
        if len(blockhex) % 2:
            return "blockhex has odd length"
        try:
            raw = bytes.fromhex(blockhex)
        except Exception:
            return "blockhex is not hex-decodable"
        if not raw:
            return "blockhex decodes to zero bytes"
        stored_at = entry["stored_at"]
        if isinstance(stored_at, bool) \
                or not isinstance(stored_at, (int, float)):
            return "stored_at is not numeric"
        return None

    def entries(self):
        """All VALID spooled entries, oldest first. Invalid entries are
        quarantined (never raised on, never returned), so one poisoned
        file can neither crash replay nor block later entries (B2)."""
        out = []
        if not self.dir or not os.path.isdir(self.dir):
            return out
        try:
            names = sorted(os.listdir(self.dir))
        except OSError as e:
            log("spool: cannot list %s: %r" % (self.dir, e))
            return out
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.dir, name)
            try:
                if os.path.getsize(path) > MAX_SPOOL_FILE_BYTES:
                    self._quarantine(path, "file exceeds size cap")
                    continue
                with open(path) as f:
                    entry = json.load(f)
            except Exception as e:
                self._quarantine(path, "unreadable entry: %r" % e)
                continue
            err = self._validate_entry(entry)
            if err:
                self._quarantine(path, err)
                continue
            entry["_path"] = path
            out.append(entry)
        out.sort(key=lambda e: (e.get("height", 0), e.get("stored_at", 0)))
        return out

    def pending_count(self):
        """Number of valid spooled entries awaiting acceptance (B15)."""
        try:
            return len(self.entries())
        except Exception:
            return 0


def _alarm_line_key(line):
    """Identity of one ALARM.txt pending line: its block hash when the
    line carries one, else its height. None for unrecognised lines
    (such lines are never dropped by upsert/rotate)."""
    idx = line.find("hash=")
    if idx >= 0:
        rest = line[idx + 5:].split()
        if rest and rest[0] != "-":
            return ("hash", rest[0])
    idx = line.find("height=")
    if idx >= 0:
        rest = line[idx + 7:].split()
        if rest:
            return ("height", rest[0])
    return None


def _alarm_file_read(spool_dir):
    try:
        with open(os.path.join(spool_dir, "ALARM.txt")) as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return []


def _alarm_file_write(spool_dir, lines):
    """Atomically replace ALARM.txt with `lines` (tmp + fsync +
    os.replace + dir fsync, the same discipline as the spool)."""
    path = os.path.join(spool_dir, "ALARM.txt")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dfd = os.open(spool_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _alarm_file_upsert(spool_dir, height, block_hash, msg):
    """C4 fix (C2-D1): ALARM.txt is a PENDING LIST, one line per failed
    block, mirroring the adapter's marker design. The old code opened
    the file with mode "w", so a second failure silently erased the
    first failure's only file record, and _rotate_alarm_for could then
    rotate the file away when the second block landed. Upsert by block
    hash (falling back to height): re-alarming the same block refreshes
    its own line; other blocks' lines are untouched."""
    key = ("hash", block_hash) if block_hash else ("height", str(height))
    lines = [ln for ln in _alarm_file_read(spool_dir)
             if _alarm_line_key(ln) != key]
    lines.append("[%s] ALARM %s" %
                 (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    _alarm_file_write(spool_dir, lines)


def fire_submission_alarm(height, detail, spool_dir="", alarm_cmd="",
                          block_hash=None):
    """Loud, unmissable signal that a found block could not be submitted.

    Three dependency-free channels: an ALARM log line + stderr line, an
    ALARM.txt file in the spool directory, and an optional operator hook
    (ALARM_CMD / --alarm-cmd, alarm text passed on stdin). Callers that
    have a spool entry go through _fire_alarm_disciplined() so the hook
    fires at most once per entry per escalation interval (B3)."""
    msg = ("SUBMISSION FAILED height=%s hash=%s detail=%r -- found block "
           "was NOT accepted by the daemon; it is retained in the spool "
           "for replay; manual attention required" %
           (height, block_hash or "-", detail))
    log("*** ALARM *** %s" % msg)
    print("*** ALARM *** %s" % msg, file=sys.stderr, flush=True)
    if spool_dir:
        try:
            os.makedirs(spool_dir, exist_ok=True)
            _alarm_file_upsert(spool_dir, height, block_hash, msg)
        except Exception as e:
            log("ALARM: could not write alarm file: %r" % e)
    if alarm_cmd:
        try:
            subprocess.run(alarm_cmd, shell=True, timeout=30,
                           input=("ALARM %s" % msg).encode())
        except Exception as e:
            log("ALARM: alarm command failed: %r" % e)
    return msg


def _fire_alarm_disciplined(spool, blockhex, height, detail, alarm_cmd):
    """Fire the submission alarm with per-entry discipline (B3).

    The entry's alarmed_at / last_alarmed_at state lives IN the spool
    entry: the full alarm (incl. ALARM_CMD) fires at most once per
    entry, and again only once ALARM_ESCALATION_SECS (>= 1 h) have
    passed. Returns True when the full alarm fired."""
    bhash = None
    try:
        bhash = block_hash_hex(blockhex)
    except Exception:
        pass
    if spool is None:
        fire_submission_alarm(height, detail, spool_dir="",
                              alarm_cmd=alarm_cmd, block_hash=bhash)
        return True
    path = None
    entry = None
    try:
        path = spool._path_for(blockhex, height)
        if os.path.exists(path):
            entry = spool._read_entry_file(path)
    except Exception:
        entry = None
    if entry is None:
        # No entry on disk to hold state (e.g. its store failed):
        # fire plainly rather than staying silent.
        fire_submission_alarm(height, detail, spool_dir=spool.dir,
                              alarm_cmd=alarm_cmd, block_hash=bhash)
        return True
    now = time.time()
    last_alarm = entry.get("last_alarmed_at") or entry.get("alarmed_at")
    if last_alarm:
        try:
            last_alarm = float(last_alarm)
        except Exception:
            last_alarm = None
    if last_alarm and (now - last_alarm) < ALARM_ESCALATION_SECS:
        log("alarm suppressed height=%s: already alarmed %.0fs ago "
            "(re-alarm only on escalation after %ds)" %
            (height, now - last_alarm, ALARM_ESCALATION_SECS))
        return False
    fire_submission_alarm(height, detail, spool_dir=spool.dir,
                          alarm_cmd=alarm_cmd, block_hash=bhash)
    updates = {"last_alarmed_at": now,
               "alarm_count": int(entry.get("alarm_count", 0) or 0) + 1}
    if not entry.get("alarmed_at"):
        updates["alarmed_at"] = now
    try:
        spool.update_entry(path, **updates)
    except Exception as e:
        log("alarm: could not persist alarm state height=%s: %r" %
            (height, e))
    return True


def _submit_sequence(backend, blockhex, height, spool, retries,
                     backoff_secs, probe_fn, sleep_fn, confirm_backends,
                     label):
    """One bounded same-payload submit sequence against ONE daemon
    (freeze v2.3 refactor of the pre-v2.3 submit_spooled_block loop).
    Returns (accepted, last, probed, deterministic, outcome) where
    outcome is the INTERFACE-CONTRACT section 2 vocabulary for the
    sequence's terminal state and deterministic is True ONLY when
    the terminal outcome is a mapped "invalid" verdict
    (is_bip22_invalid) -- per-attempt state, never sticky (S10-D5):
    the sequence BREAKS on the first invalid verdict (contract C3,
    never-retry), so no later timeout can be poisoned by it and no
    verdict is ever retried. On CONFIRMED acceptance the spool
    entry is discarded here.

    Contract C2 (S10-D2): a bare success-shaped reply (null / "")
    is outcome "unconfirmed", NOT acceptance -- acceptance requires
    the visibility check getblockhash(height) == block hash against
    confirm_backends (any-True). Only a visibility-confirmed block
    discards the spool; an unconfirmed success keeps the entry and
    ends the sequence (replay cadence governs resubmission, contract
    row 9 -- no hot loop)."""
    attempts = 1 + max(0, int(retries))
    last = None
    probed = False
    deterministic = False
    outcome = "unavailable"
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            tip = None
            if probe_fn is not None:
                try:
                    probe_res = probe_fn()
                    probed = True
                    if isinstance(probe_res, int) \
                            and not isinstance(probe_res, bool):
                        tip = probe_res
                except Exception as e:
                    log("submission retry: probe failed: %r" % e)
            if tip is None:
                tip = _safe_tip(backend)
            if tip is not None and tip >= height:
                log("submission retry aborted height=%d: tip %d has "
                    "moved to/past this block's height -- the block "
                    "can never land; going to spool + alarm" %
                    (height, tip))
                if last is None:
                    last = "aborted: tip %d past height %d" % (tip, height)
                break
            if backoff_secs:
                sleep_fn(backoff_secs * (attempt - 1))
        try:
            res = backend.submit_block(blockhex)
        except Exception as e:
            last = e
            outcome = "unavailable"
            deterministic = False
            log("submitblock attempt %d/%d height=%d FAILED: %r%s" %
                (attempt, attempts, height, e,
                 " (stale template / unknown block hash)"
                 if _looks_like_stale_template(e) else ""))
            continue
        if is_submit_accepted(res):
            # Contract C2: bare null/"" is UNCONFIRMED until visible.
            confirmed = _confirm_on_chain_any(
                confirm_backends, blockhex, height)
            if confirmed is True:
                log("submitblock height=%d -> %r (ACCEPTED, attempt "
                    "%d/%d%s; success-shaped reply CONFIRMED on-chain "
                    "via getblockhash)" %
                    (height, res, attempt, attempts,
                     ", via " + label if label else ""))
                if spool is not None:
                    spool.discard(blockhex, height)
                return True, res, probed, False, "accepted"
            last = res
            outcome = "unconfirmed"
            deterministic = False
            log("*** WARN *** submitblock height=%d -> %r "
                "(success-shaped reply NOT confirmed on-chain: "
                "getblockhash check %s -- outcome=unconfirmed; spool "
                "entry KEPT, no credit; replay/visibility governs, "
                "attempt %d/%d%s)" %
                (height, res,
                 "reports a DIFFERENT hash at this height"
                 if confirmed is False else "unavailable",
                 attempt, attempts,
                 ", via " + label if label else ""))
            break
        if is_submit_ambiguous(res):
            confirmed = _confirm_on_chain_any(
                confirm_backends, blockhex, height)
            if confirmed:
                log("submitblock height=%d -> %r (AMBIGUOUS result "
                    "CONFIRMED on-chain via getblockhash, attempt %d/%d)"
                    % (height, res, attempt, attempts))
                if spool is not None:
                    spool.discard(blockhex, height)
                return True, res, probed, False, \
                    ("duplicate" if res == "duplicate" else "accepted")
            last = res
            outcome = "inconclusive" if res != "duplicate" \
                else "unconfirmed"
            deterministic = False
            log("submitblock attempt %d/%d height=%d AMBIGUOUS %r NOT "
                "confirmed on-chain (getblockhash check: %s) -- entry "
                "kept, retrying the same payload" %
                (attempt, attempts, height, res,
                 "different hash at height" if confirmed is False
                 else "check unavailable"))
            continue
        last = res
        if is_bip22_invalid(res):
            # Contract C3: a mapped invalid verdict is NEVER retried.
            deterministic = True
            outcome = "invalid"
            log("submitblock attempt %d/%d height=%d REJECTED: %r "
                "(outcome=invalid, deterministic verdict -- "
                "never-retry; sequence ends here)" %
                (attempt, attempts, height, res))
            break
        if res in NEVM_TRANSPORT_REASONS:
            outcome = "unavailable"
        elif res in ("inconclusive-not-best-prevblk", "bad-prevblk",
                     "prev-blk-not-found"):
            # Stale-prevhash class (contract section 4 / row 4):
            # bounded same-payload retry while the height is open;
            # the fallback gate in submit_spooled_block treats an
            # exhausted stale-prevhash sequence as no-spray (the
            # seat-10 R3-B2 design point), without calling it a
            # payload verdict.
            outcome = "stale-prevhash"
        else:
            # Unmapped non-empty string: not a section-4 verdict we
            # can name; bounded same-payload retry applies (legacy
            # behaviour for synthetic/unknown strings) and the
            # outcome stays non-terminal for alarm purposes.
            outcome = "unavailable"
        deterministic = False
        log("submitblock attempt %d/%d height=%d REJECTED: %r%s "
            "(outcome=%s)" %
            (attempt, attempts, height, res,
             " (stale template?)" if _looks_like_stale_template(res)
             else "", outcome))
    return False, last, probed, deterministic, outcome


def _spool_entry_age_secs(spool, blockhex, height):
    """Age of the spool entry for this block (now - stored_at), or
    None when there is no readable entry. C7's 90 s window is measured
    from the solve (the entry's stored_at), not from the latest
    attempt -- a replayed entry that is already past the window
    alarms immediately on its next unresolved pass."""
    if spool is None:
        return None
    try:
        path = spool._path_for(blockhex, height)
        if not os.path.exists(path):
            return None
        entry = spool._read_entry_file(path)
        stored_at = entry.get("stored_at")
        if isinstance(stored_at, bool) \
                or not isinstance(stored_at, (int, float)):
            return None
        return max(0.0, time.time() - float(stored_at))
    except Exception:
        return None


def _deferred_alarm_check(spool, blockhex, height, detail, alarm_cmd,
                          backends):
    """Timer callback for the C7 window: at ~90 s after the solve,
    if the entry is STILL spooled and no backend can confirm the
    block on-chain, fire the (disciplined) submission ALARM. If the
    block has become visible meanwhile, resolve the entry instead."""
    try:
        path = spool._path_for(blockhex, height)
        if not os.path.exists(path):
            return  # resolved/discarded since the WARN: no alarm
        if _confirm_on_chain_any(backends, blockhex, height) is True:
            log("spool: deferred alarm check height=%d: block is now "
                "CONFIRMED on-chain -- discarding entry, no alarm" %
                height)
            spool.discard(blockhex, height)
            return
        _fire_alarm_disciplined(spool, blockhex, height, detail,
                                alarm_cmd)
    except Exception as e:
        log("deferred alarm check height=%s failed: %r" % (height, e))


def _warn_or_alarm(spool, blockhex, height, detail, alarm_cmd, outcome,
                   backends, alarm_delay_secs):
    """Contract C7 disposition for an unresolved submission.

    Terminal outcome ("invalid") -> ALARM immediately. Ambiguous /
    transient / unconfirmed outcomes -> WARN immediately (log +
    persisted warn state on the entry) and ALARM at ~90 s if still
    unresolved: immediately when the entry is already past the
    window (replay of an old entry), otherwise via a daemon timer
    that re-checks visibility first (the spool replay loop is the
    backstop if the process restarts). With no spool there is no
    replay set to protect, so the alarm fires immediately."""
    if outcome == "invalid" or spool is None:
        _fire_alarm_disciplined(spool, blockhex, height, detail,
                                alarm_cmd)
        return "alarmed"
    age = _spool_entry_age_secs(spool, blockhex, height)
    log("*** WARN *** SUBMISSION UNRESOLVED height=%s outcome=%s "
        "detail=%r -- found block is retained in the spool; ALARM "
        "fires at ~%ds after the solve if still unresolved "
        "(contract C7; fallback leg already ran where configured)" %
        (height, outcome, detail, int(alarm_delay_secs)))
    if age is not None and age >= alarm_delay_secs:
        _fire_alarm_disciplined(spool, blockhex, height, detail,
                                alarm_cmd)
        return "alarmed"
    try:
        path = spool._path_for(blockhex, height)
        if os.path.exists(path):
            spool.update_entry(path, warned_at=time.time(),
                               warn_outcome=outcome,
                               alarm_due_at=time.time()
                               + (alarm_delay_secs - (age or 0.0)))
    except Exception as e:
        log("warn: could not persist warn state height=%s: %r" %
            (height, e))
    timer = threading.Timer(
        max(0.0, alarm_delay_secs - (age or 0.0)),
        _deferred_alarm_check,
        args=(spool, blockhex, height, detail, alarm_cmd, backends))
    timer.daemon = True
    timer.start()
    return "warned"


def submit_spooled_block(backend, blockhex, height, spool=None, retries=3,
                         backoff_secs=2.0, refresh_fn=None, alarm_cmd="",
                         sleep_fn=time.sleep, probe_fn=None,
                         fallback_backend=None,
                         alarm_delay_secs=ALARM_DELAY_SECS):
    """Submit one block with spool-first + same-payload retry semantics.

    Returns (accepted, detail, probed):
      accepted -- True only on CONFIRMED acceptance: a success-shaped
                  or ambiguous result CONFIRMED on-chain via
                  getblockhash(height) == block hash (B4 + contract
                  C2: a bare null/"" reply alone is "unconfirmed",
                  never acceptance, and never discards the spool).
      detail   -- the daemon result on success, else the last
                  rejection string / exception.
      probed   -- True if the cheap probe ran between attempts.
    The block is written to the spool BEFORE the first attempt and is
    removed from the spool only on confirmed acceptance. A spool write
    failure logs ERROR + raises the alarm and the submission proceeds
    anyway (B1, mirroring the yiimp adapter). Retries resubmit the
    SAME blockhex: between attempts only probe_fn (a cheap
    getblockcount-class probe) runs -- miner jobs are NEVER rebuilt
    here (B3/Angle-2 churn) -- and the retry aborts immediately when
    the tip has moved to/past the block's height, since such a block
    can never land. refresh_fn is accepted for backward compatibility
    and used ONLY as the probe hook when probe_fn is not given; it is
    never a job rebuild. After the bounded retries are exhausted the
    submission alarm fires (per-entry discipline, B3) and the entry
    stays spooled.

    Freeze v2.3 (R3): when fallback_backend is given (main() builds
    it from SUBMIT_FALLBACK_RPC_URL; empty default = None = exactly
    the pre-v2.3 behaviour) and the PRIMARY sequence ends without
    confirmed acceptance, the same blockhex is submitted to the
    secondary daemon with the same bounded sequence; the first
    confirmed acceptance on either daemon wins and only then is the
    spool entry discarded. Ambiguous-result confirmation consults
    both daemons (whichever can answer). The exception is a
    DETERMINISTIC primary rejection (a real BIP22 reject reason --
    is_deterministic_submit_reject): the payload is invalid under
    the shared consensus rules, so it is NOT sprayed at the
    secondary; the sequence ends in the normal spool + alarm path."""
    if probe_fn is None and refresh_fn is not None:
        probe_fn = refresh_fn
    spool_alarm_fired = False
    if spool is not None:
        try:
            spool.store(blockhex, height)
        except Exception as e:
            # B1: a spool failure must NOT block the submission itself.
            log("ERROR: spool write failed height=%d: %r -- "
                "submitting anyway" % (height, e))
            bhash = None
            try:
                bhash = block_hash_hex(blockhex)
            except Exception:
                pass
            fire_submission_alarm(
                height, "spool write failed: %r (submitting anyway)" % (e,),
                spool_dir=spool.dir, alarm_cmd=alarm_cmd, block_hash=bhash)
            spool_alarm_fired = True
    confirm_backends = [backend]
    if fallback_backend is not None and fallback_backend is not backend:
        confirm_backends.append(fallback_backend)
    accepted, last, probed, deterministic, outcome = _submit_sequence(
        backend, blockhex, height, spool, retries, backoff_secs,
        probe_fn, sleep_fn, confirm_backends, label="")
    if accepted:
        # S10-D1 root fix: the interim v2.3 refactor lost this early
        # return -- a primary-accepted block fell through to the
        # alarm path and the caller was told accepted=False.
        return True, last, probed
    if fallback_backend is not None and fallback_backend is not backend:
        if outcome == "invalid":
            log("submitblock height=%d: primary returned a "
                "DETERMINISTIC verdict %r (outcome=invalid) -- not "
                "spraying the same payload at the secondary submit "
                "endpoint (it is invalid under the shared consensus "
                "rules; contract never-retry)" % (height, last))
        elif outcome == "stale-prevhash":
            log("submitblock height=%d: primary sequence ended "
                "stale-prevhash %r after bounded retries -- not "
                "spraying at the secondary (seat-10 R3-B2 design "
                "point); entry stays spooled for replay" %
                (height, last))
        else:
            # The block may already be on-chain in the secondary's
            # view (e.g. the primary accepted it network-wide but its
            # own reply/check path wedged): confirm before resending.
            confirmed = _confirm_on_chain_any(
                [fallback_backend, backend], blockhex, height)
            if confirmed:
                log("submitblock height=%d CONFIRMED on-chain via "
                    "the secondary daemon's getblockhash -- accepted "
                    "without a secondary submission" % height)
                if spool is not None:
                    spool.discard(blockhex, height)
                return True, last, probed
            log("submitblock height=%d: primary sequence ended "
                "without confirmed acceptance (last=%r, outcome=%s) "
                "-- submitting the SAME payload to the secondary "
                "daemon (R3)" % (height, last, outcome))
            fb_accepted, fb_last, fb_probed, _fb_det, fb_outcome = \
                _submit_sequence(
                    fallback_backend, blockhex, height, spool, retries,
                    backoff_secs, None, sleep_fn, confirm_backends,
                    label="secondary daemon")
            probed = probed or fb_probed
            if fb_accepted:
                log("submission outcome=accepted via=secondary "
                    "height=%d detail=%r -- first confirmed acceptance "
                    "wins (R3); spool entry discarded" %
                    (height, fb_last))
                return True, fb_last, probed
            last = fb_last
            outcome = fb_outcome
    if spool_alarm_fired:
        # The spool-write alarm already fired in this call; do not page
        # twice for the same find. The failure itself is still loud.
        log("*** ALARM *** SUBMISSION FAILED height=%s detail=%r -- "
            "alarm already raised for the spool write failure in this "
            "call; block was NOT spooled and NOT accepted" %
            (height, last))
    else:
        # Contract C7: WARN now / ALARM at ~90 s for ambiguous,
        # transient and unconfirmed outcomes; ALARM immediately for
        # a terminal invalid verdict. The fallback leg (above) has
        # already run before either stage.
        _warn_or_alarm(spool, blockhex, height, last, alarm_cmd,
                       outcome, confirm_backends, alarm_delay_secs)
    return False, last, probed

def _replay_one(backend, spool, entry, submit_kwargs):
    """Replay a single validated spool entry. Returns 1 when the entry
    is resolved (accepted / found on-chain), else 0. Never raises for
    entry-shaped problems -- replay_spool isolates failures per entry."""
    height = entry["height"]
    blockhex = entry["blockhex"]
    path = entry.get("_path")
    # Already on-chain? Resolves the lost-ack case without a resubmit:
    # the block IS the chain block at its height, so it is confirmed.
    # Any-True across the primary and (when configured) the fallback
    # backend's chain views (S10-D6 semantics on the replay path too).
    confirmed = _confirm_on_chain_any(
        [backend, submit_kwargs.get("fallback_backend")],
        blockhex, height)
    if confirmed:
        log("spool replay: height=%d already on chain (getblockhash "
            "match) -- confirmed; discarding spool entry" % height)
        spool.discard(blockhex, height)
        return 1
    passes = entry.get("replay_passes", 0)
    try:
        passes = int(passes or 0)
    except Exception:
        passes = 0
    tip = _safe_tip(backend)
    buried = tip is not None and tip >= height and confirmed is False
    if buried and passes >= DEAD_LETTER_AFTER_PASSES:
        spool.dead_letter(entry)
        return 0
    if path:
        try:
            spool.update_entry(path, replay_passes=passes + 1)
        except Exception as e:
            log("spool replay: could not record pass height=%s: %r" %
                (height, e))
    accepted, _detail, _probed = submit_spooled_block(
        backend, blockhex, height, spool=spool, **submit_kwargs)
    return 1 if accepted else 0


def replay_spool(backend, spool, **submit_kwargs):
    """Replay every spooled block once (bounded retries each, via
    submit_spooled_block). Returns (accepted_count, retained_count).
    Per-entry isolation (B2): one entry's failure -- poison, crash, or
    rejection -- never stops later entries from replaying. Idempotent:
    blocks already on-chain are confirmed via getblockhash and removed,
    never an error loop."""
    if spool is None:
        return 0, 0
    entries = spool.entries()
    ok = 0
    for entry in entries:
        try:
            ok += _replay_one(backend, spool, entry, submit_kwargs)
        except Exception as e:
            log("spool replay: entry height=%s failed unexpectedly: "
                "%r -- continuing with later entries" %
                (entry.get("height"), e))
    retained = len(spool.entries())
    if entries:
        log("spool replay: %d entr%s, %d accepted, %d retained" %
            (len(entries), "y" if len(entries) == 1 else "ies",
             ok, retained))
    return ok, retained


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


def build_block(job, parent_coinbase, parent_header):
    """Assemble the full AuxPoW block bytes from a winning share."""
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
    return block


def assemble_and_submit(backend, job, parent_coinbase, parent_header,
                        spool=None, submit_retries=3, backoff_secs=2.0,
                        refresh_fn=None, alarm_cmd="", probe_fn=None,
                        fallback_backend=None):
    """Assemble, spool, and submit a winning share's block.

    Returns (accepted, detail, refreshed) from submit_spooled_block:
    a daemon rejection is (False, <reason>, ...) -- it is NEVER reported
    as a found block. (This replaces the old contract, which returned the
    raw submitblock result and let the caller mistake a rejection string
    for success.)"""
    block = build_block(job, parent_coinbase, parent_header)
    return submit_spooled_block(backend, block.hex(), job.height,
                                spool=spool, retries=submit_retries,
                                backoff_secs=backoff_secs,
                                refresh_fn=refresh_fn, alarm_cmd=alarm_cmd,
                                probe_fn=probe_fn,
                                fallback_backend=fallback_backend)


class JobManager:
    """Holds the current job; rebuilds on new tip / timer / block found."""
    def __init__(self, backend, nevm, payout_spk, refresh_secs):
        self.backend = backend
        self.nevm = nevm
        self.payout_spk = payout_spk
        self.refresh_secs = refresh_secs
        self.lock = threading.Lock()
        self.retained = {}
        self._job = None
        self.job_counter = 0
        self.listeners = []  # callbacks(job, clean) on new job
        self.last_tip = None
        # RT-1 (red team, 2026-10-27): superseded jobs are RETAINED
        # (last RETAINED_JOBS, via the job property setter below) so
        # a block-winning share that arrives just after a rotation
        # -- in particular a 30 s TIMER rotation at the SAME height,
        # where the height is still open -- can still be assembled,
        # spooled and submitted.  Before this fix the old Job object
        # was discarded at rotation and such a share was dropped as
        # a generic "stale job" with no submit, no spool and no
        # alarm: a solved block lost silently.

    @property
    def job(self):
        return self._job

    @job.setter
    def job(self, new_job):
        old_job = getattr(self, "_job", None)
        if old_job is not None and old_job is not new_job:
            self.retained[old_job.job_id] = old_job
            while len(self.retained) > self.RETAINED_JOBS:
                self.retained.pop(next(iter(self.retained)))
        self._job = new_job

    def add_listener(self, fn):
        with self.lock:
            self.listeners.append(fn)

    def remove_listener(self, fn):
        """C4 fix (C1-D5): handlers must unregister on disconnect --
        previously every connection ever made stayed in the list for
        the life of the process and every broadcast iterated all of
        them (their send() swallowed the failure, so the leak was
        silent and unbounded)."""
        with self.lock:
            try:
                self.listeners.remove(fn)
            except ValueError:
                pass

    def current(self):
        with self.lock:
            return self.job

    def get_job(self, job_id):
        """Current job if job_id matches, else a retained
        superseded job, else None.  (RT-1)"""
        with self.lock:
            if self.job is not None and self.job.job_id == job_id:
                return self.job
            return self.retained.get(job_id)

    def _broadcast(self, job, clean):
        for fn in list(self.listeners):
            try:
                fn(job, clean)
            except Exception as e:
                log("listener error: %r" % e)

    RETAINED_JOBS = 4

    def _new_job_locked(self, clean):
        self.job_counter += 1
        job_id = "%08x" % self.job_counter
        job = build_job(self.backend, self.nevm, self.payout_spk, job_id)
        # RT-1: assigning self.job retains the superseded job via
        # the property setter above.
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
                    # C4 fix (C1-D2): last_tip is assigned ONLY after the
                    # rebuild succeeds. The old code assigned it first, so
                    # one transient build failure (e.g. a NEVM/ZMQ hiccup)
                    # permanently suppressed the tip branch: every later
                    # poll saw tip == last_tip and miners kept grinding
                    # the stale job until the refresh timer or the NEXT
                    # tip change. Now a failed rebuild is retried on the
                    # next poll (5 s) until it succeeds.
                    self.new_job(clean=True)
                    self.last_tip = tip
                elif job is not None and time.time() - job.created > self.refresh_secs:
                    self.new_job(clean=True)
                elif job is None:
                    self.new_job(clean=True)
                    self.last_tip = tip
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

        _job_listener = lambda job, clean: send_job(job, clean)  # noqa: E731
        self.server.job_manager.add_listener(_job_listener)
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
            # C4 fix (C1-D5): drop this connection's job listener too,
            # not just the handler registration.
            self.server.job_manager.remove_listener(_job_listener)
            self.server.unregister(self)

    def finish(self):
        try:
            super().finish()
        except Exception:
            pass


class StratumServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, job_manager, backend, share_diff, vardiff,
                 spool=None, submit_retries=3, submit_backoff_secs=2.0,
                 alarm_cmd="", fallback_backend=None):
        self.job_manager = job_manager
        self.backend = backend
        self.share_diff = share_diff
        self.vardiff = vardiff
        self.spool = spool
        self.submit_retries = submit_retries
        self.submit_backoff_secs = submit_backoff_secs
        self.alarm_cmd = alarm_cmd
        # Freeze v2.3 (R3): optional secondary submit daemon (None =
        # pre-v2.3 single-daemon behaviour).
        self.fallback_backend = fallback_backend
        self.handlers = set()
        self.handlers_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.stats = {"shares": 0, "rejects": 0, "blocks": 0,
                      "submit_failures": 0}
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
        job = self.job_manager.get_job(job_id)
        if job is None:
            with self.stats_lock:
                self.stats["rejects"] += 1
            return False, False, "stale job"
        # RT-1: a share for a RETAINED (superseded) job is validated
        # exactly like a current-job share below, but it only earns
        # the block path if it actually meets the BLOCK target: a
        # merely-valid or low share for a superseded job keeps the
        # existing "stale job" behaviour (see the branches below).
        superseded = job is not self.job_manager.current()
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

        if superseded and hv > job.target:
            # RT-1: valid share, but NOT a block, for a superseded
            # job -- preserved stale behaviour, nothing is owed.
            with self.stats_lock:
                self.stats["rejects"] += 1
            return False, False, "stale job"

        with self.stats_lock:
            self.stats["shares"] += 1
        job.shares += 1

        if hv <= job.target:
            if superseded:
                # RT-1: a block-winning share for a superseded job
                # (e.g. after a same-height timer rotation) goes
                # through the SAME spool+submit path as a current
                # job.  assemble_and_submit spools before submitting
                # and its retry probe aborts once the tip has taken
                # the height; a block for a height the tip has
                # passed therefore ends spooled + alarmed (the
                # provably-buried path), never silently dropped.
                log("BLOCK-WINNING share for superseded job %s "
                    "height=%d -- routing through spool+submit "
                    "(RT-1)" % (job_id, job.height))
            # WINNER: assemble the full AuxPoW block and submit.
            log("BLOCK by %s job=%s height=%d hash=%s" %
                (worker, job_id, job.height,
                 sha256d(header)[::-1].hex()[:16]))
            def _probe_tip():
                # Cheap liveness probe for the retry loop ONLY (one
                # getblockcount RPC). Retrying a found block resubmits
                # the SAME payload; it must NEVER rebuild miner jobs --
                # a clean rebuild resets every connected miner's work
                # for the sake of one stale block (attacker B3 churn).
                try:
                    return self.backend.get_height()
                except Exception:
                    return None

            try:
                accepted, detail, _probed = assemble_and_submit(
                    self.backend, job, coinbase, header,
                    spool=self.spool, submit_retries=self.submit_retries,
                    backoff_secs=self.submit_backoff_secs,
                    probe_fn=_probe_tip, alarm_cmd=self.alarm_cmd,
                    fallback_backend=self.fallback_backend)
            except Exception as e:
                # Assembly/tripwire failure: nothing was submitted.
                log("submit failed: %r" % e)
                # C4 fix (C2-D2): a block-winning share whose ASSEMBLY
                # fails was previously lost with only this one log line
                # -- never spooled (spooling happens inside
                # submit_spooled_block, after assembly) and never
                # alarmed. Fire the full submission alarm so the
                # failure is unmissable on every channel (log, stderr,
                # ALARM.txt, ALARM_CMD). The block itself cannot be
                # spooled here: assembly is what produces the blockhex.
                try:
                    fire_submission_alarm(
                        job.height, "assembly failed: %r" % e,
                        spool_dir=(self.spool.dir if self.spool else ""),
                        alarm_cmd=self.alarm_cmd)
                except Exception as alarm_exc:
                    log("assembly-failure alarm failed: %r" % alarm_exc)
                return False, False, "submit failed: %s" % e
            if accepted:
                # New job immediately so nobody mines stale work. Wrapped:
                # a job-build failure must never swallow the miner's reply
                # (the old code let this exception kill the handler thread
                # after a winning submit, leaving the miner unanswered).
                # On FAILURE there is deliberately NO job rebuild: the
                # block stays spooled for replay, and miners keep their
                # current job until watch_loop moves it on a tip change.
                try:
                    self.job_manager.new_job(clean=True)
                except Exception as e:
                    log("post-submit new_job failed: %r" % e)
            if not accepted:
                # FIX: a daemon rejection is a FAILURE. It is not counted
                # as a found block and the miner is NOT acked True; the
                # block stays in the spool and the alarm has fired.
                with self.stats_lock:
                    self.stats["submit_failures"] += 1
                log("BLOCK NOT ACCEPTED height=%d detail=%r -- miner "
                    "answered with an error, block retained in spool" %
                    (job.height, detail))
                return False, False, ("submit failed: daemon rejected "
                                      "block: %s" % (detail,))
            with self.stats_lock:
                self.stats["blocks"] += 1
            job.blocks_found += 1
            return True, True, "block"
        return True, False, "share"


def _parse_main_args(argv):
    """Positional config path plus optional submission-safety flags.
    Flags override the same keys in the config file."""
    overrides = {}
    positional = []
    flag_keys = {"--spool-dir": "SPOOL_DIR",
                 "--submit-retries": "SUBMIT_RETRIES",
                 "--submit-backoff-secs": "SUBMIT_BACKOFF_SECS",
                 "--alarm-cmd": "ALARM_CMD",
                 "--spool-replay-secs": "SPOOL_REPLAY_SECS",
                 "--submit-fallback-rpc-url": "SUBMIT_FALLBACK_RPC_URL"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        key = None
        val = None
        if arg in flag_keys:
            key = flag_keys[arg]
            i += 1
            if i >= len(argv):
                sys.exit("flag %s needs a value" % arg)
            val = argv[i]
        elif any(arg.startswith(f + "=") for f in flag_keys):
            f, val = arg.split("=", 1)
            key = flag_keys[f]
        elif arg in ("-h", "--help"):
            sys.exit("usage: shitcoin-stratum-proxy.py [--spool-dir DIR] "
                     "[--submit-retries N] [--submit-backoff-secs S] "
                     "[--alarm-cmd CMD] /path/to/proxy.conf")
        else:
            positional.append(arg)
        if key:
            overrides[key] = val
        i += 1
    if len(positional) != 1:
        sys.exit("usage: shitcoin-stratum-proxy.py [--spool-dir DIR] "
                 "[--submit-retries N] [--submit-backoff-secs S] "
                 "[--alarm-cmd CMD] /path/to/proxy.conf")
    return positional[0], overrides


def main():
    conf_path, overrides = _parse_main_args(sys.argv[1:])
    cfg = load_config(conf_path)
    cfg.update(overrides)
    # Re-apply the numeric conversions for any flag-supplied strings.
    cfg["SUBMIT_RETRIES"] = int(cfg["SUBMIT_RETRIES"])
    cfg["SUBMIT_BACKOFF_SECS"] = float(cfg["SUBMIT_BACKOFF_SECS"])
    cfg["SPOOL_REPLAY_SECS"] = int(cfg["SPOOL_REPLAY_SECS"])

    global _log_file
    if cfg["LOG_FILE"]:
        _log_file = open(cfg["LOG_FILE"], "a")

    backend = SyscoinRPCBackend(cfg["NODE_RPC_URL"], cfg["NODE_COOKIE"],
                                cfg["NODE_RPC_USER"], cfg["NODE_RPC_PASS"])
    # Freeze v2.3 (R3): optional secondary submit daemon. Empty URL
    # (the default) leaves fallback_backend=None and the submission
    # path is exactly the pre-v2.3 single-daemon path.
    fallback_backend = None
    if cfg.get("SUBMIT_FALLBACK_RPC_URL"):
        fb_user = cfg.get("SUBMIT_FALLBACK_RPC_USER") or cfg["NODE_RPC_USER"]
        fb_pass = cfg.get("SUBMIT_FALLBACK_RPC_PASS") or cfg["NODE_RPC_PASS"]
        fb_cookie = "" if fb_user else cfg["NODE_COOKIE"]
        fallback_backend = SyscoinRPCBackend(
            cfg["SUBMIT_FALLBACK_RPC_URL"], fb_cookie, fb_user, fb_pass)
        log("secondary submit endpoint configured (R3): %s -- found "
            "blocks are submitted there too when the primary does not "
            "reach confirmed acceptance (deterministic rejections "
            "excepted)" % cfg["SUBMIT_FALLBACK_RPC_URL"])
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

    # Disk spool for found blocks (default ./spool; SPOOL_DIR="" disables).
    spool = BlockSpool(cfg["SPOOL_DIR"]) if cfg["SPOOL_DIR"] else None
    submit_kwargs = {"retries": cfg["SUBMIT_RETRIES"],
                     "backoff_secs": cfg["SUBMIT_BACKOFF_SECS"],
                     # Cheap probe only -- replay/retry NEVER rebuilds
                     # miner jobs (attacker B3 churn).
                     "probe_fn": lambda: backend.get_height(),
                     "alarm_cmd": cfg["ALARM_CMD"],
                     "fallback_backend": fallback_backend}
    if spool is not None:
        # Replay anything a previous run found but never got accepted,
        # before opening the stratum port.
        log("spool: %d entr%s pending replay in %s" %
            (len(spool.entries()),
             "y" if len(spool.entries()) == 1 else "ies", spool.dir))
        try:
            replay_spool(backend, spool, **submit_kwargs)
        except Exception as e:
            log("spool replay on startup failed: %r" % e)

    server = StratumServer((cfg["LISTEN_HOST"], cfg["LISTEN_PORT"]),
                           StratumHandler, jm, backend,
                           cfg["SHARE_DIFF"], cfg["VARDIFF"],
                           spool=spool,
                           submit_retries=cfg["SUBMIT_RETRIES"],
                           submit_backoff_secs=cfg["SUBMIT_BACKOFF_SECS"],
                           alarm_cmd=cfg["ALARM_CMD"],
                           fallback_backend=fallback_backend)
    threading.Thread(target=jm.watch_loop, daemon=True).start()

    if spool is not None and cfg["SPOOL_REPLAY_SECS"] > 0:
        def spool_replay_loop():
            while True:
                time.sleep(cfg["SPOOL_REPLAY_SECS"])
                if spool.entries():
                    try:
                        replay_spool(backend, spool, **submit_kwargs)
                    except Exception as e:
                        log("spool replay loop error: %r" % e)
        threading.Thread(target=spool_replay_loop, daemon=True).start()

    def stats_loop():
        while True:
            time.sleep(300)
            with server.stats_lock:
                s = dict(server.stats)
            job = jm.current()
            spool_pending = 0
            if spool is not None:
                try:
                    spool_pending = spool.pending_count()
                except Exception:
                    spool_pending = -1
            log("stats: shares=%d rejects=%d blocks=%d submit_failures=%d "
                "spool_pending=%d job=%s height=%s" %
                (s["shares"], s["rejects"], s["blocks"],
                 s.get("submit_failures", 0), spool_pending,
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
