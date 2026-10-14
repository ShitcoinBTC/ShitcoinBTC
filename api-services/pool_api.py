#!/usr/bin/env python3
"""
Public read-only Mining Pool API — node2 BTC stratum pool (shitcoin-pool.service).

SEPARATE from: (a) the exchange API, (b) the chain API, (c) node1's
shitcoin-mine-pool.service (the older SHIT *web-miner* pool, /api/mine/* on
node1 localhost:5004). This service describes ONLY the node2 pool:
stratum :3333, dashboard :8080, records in /home/shitcoin/pool/pool.db.

Data sources (the pool's OWN records only — no dashboard HTML scraping):
  * pool.db opened SQLite mode=ro, aggregate queries only.
  * the pool process's own JSON at http://127.0.0.1:8080/api/stats, used ONLY
    for live process state that is not persisted in pool.db (BTC tip height,
    current job, bitcoind feed state, SHIT aux state, payouts_enabled flag).

PRIVACY (absolute): this service NEVER selects or returns miner payout
addresses, per-miner balances, per-miner earnings, worker names, block
finders, payout addresses, or connection IPs. Aggregate counts only.
The pre-existing dashboard endpoints /miner/<address> and /payouts on :8080
do expose per-address data; this API deliberately does not replicate them.

TRUTH NOTES:
  * Hashrate uses the pool's own definition (pool.py pool_stats()):
    sum(diff of accepted shares in the last 600 s) * 2^32 / 600.
    With 0 accepted shares the correct hashrate is 0.
  * "Online" uses the pool's own definition: workers.last_seen > now-600.
  * Shares are pruned by pool.py after 7 days, so hashrate history depth
    is at most 7 days and currently contains only test-era rows.
  * pool.db stores rejected shares as rows with diff = -1 and NO reason
    column, so reject *reasons* cannot be reported from the records.
  * Feed health: /api/stats' bitcoind.state flaps ready/down during node3
    RPC-timeout windows (root cause owned by Boss B's rpc cell). Instead of
    republishing a binary flap, /status reports the last sampled state
    PLUS last_ready_at (tracked by this service) so consumers can see a
    degraded-but-recovering feed honestly.

Config (env): POOL_DB, POOL_STATS_URL, POOL_API_HOST, POOL_API_PORT,
POOL_API_STATE (path of the small feed-health state JSON this service owns).
Stdlib only. Read-only: the only file written is POOL_API_STATE.
"""
import json
import os
import sqlite3
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB_PATH = os.environ.get("POOL_DB", "/home/shitcoin/pool/pool.db")
STATS_URL = os.environ.get("POOL_STATS_URL", "http://127.0.0.1:8080/api/stats")
HOST = os.environ.get("POOL_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("POOL_API_PORT", "8091"))
STATE_PATH = os.environ.get("POOL_API_STATE", "/tmp/pool-api-state.json")

ONLINE_WINDOW_S = 600          # pool.py pool_stats(): last_seen > now-600
HASHRATE_WINDOW_S = 600        # pool.py pool_stats(): last 10 min of shares
SHARE_RETENTION_S = 7 * 86400  # pool.py record_share(): prune t < now-7d

# Verified 2026-10-14 against live pool.conf + pool.py (see ../AUDIT.md):
FEES = {
    "btc_tax_percent": 0.002,        # coinbase tax in every block (pool.py: tax = coinbasevalue*2//100000)
    "btc_pool_fee_percent": 0.0,     # no BTC fee beyond the coinbase tax
    "shit_pool_fee_percent": 1.0,    # pool.py credit_block(): fee SHIT = 0.01
    "payout_method": "PPLNS",
    "payout_thresholds": {"BTC": 0.001, "SHIT": 100},  # conf: 100000 sats / 10000000000 sats
    "payout_threshold_sats": {"BTC": 100000, "SHIT": 10000000000},
    "payout_schedule": "hourly",     # pool.py payout_loop(): sleep(3600)
    "payout_interval_seconds": 3600,
}

_stats_cache = {"t": 0, "data": None}


def db_ro():
    con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def pool_stats_live():
    """Pool process's own JSON (cached 5 s). Returns dict or None."""
    now = time.time()
    if now - _stats_cache["t"] < 5 and _stats_cache["data"] is not None:
        return _stats_cache["data"]
    try:
        with urllib.request.urlopen(STATS_URL, timeout=3) as r:
            data = json.loads(r.read().decode())
        _stats_cache.update(t=now, data=data)
        return data
    except Exception:
        return _stats_cache["data"]


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(s):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(s, f)
    except Exception:
        pass


def feed_health(live):
    """Honest feed view: current sampled state + when it was last ready."""
    st = _load_state()
    now = int(time.time())
    if live is None:
        return {"state": "unknown", "sampled_at": now,
                "last_ready_at": st.get("last_ready_at"),
                "note": "pool /api/stats unreachable at sample time; "
                        "last_ready_at is this service's last ready sample"}
    chains = live.get("chains", {})
    bc = chains.get("bitcoind", {})
    state = bc.get("state")
    if state == "ready":
        st["last_ready_at"] = now
        st["last_tip_height"] = bc.get("height")
        _save_state(st)
    return {"state": state, "btc_tip_height": bc.get("height"),
            "btc_tip_progress": bc.get("progress"),
            "sampled_at": now,
            "last_ready_at": st.get("last_ready_at"),
            "note": "state is the pool's own sampled bitcoind state; it can "
                    "flap during node3 RPC-timeout windows. last_ready_at "
                    "shows the most recent ready sample so a timeout window "
                    "is not mistaken for a dead feed."}


def ep_status():
    live = pool_stats_live()
    job = (live or {}).get("chains", {}).get("job", {})
    aux = (live or {}).get("chains", {}).get("shit_aux", {})
    return {
        "pool": "shitcoin-pool (node2 BTC stratum pool)",
        "stratum": {"host": "170.101.111.196", "port": 3333},
        "feed": feed_health(live),
        "job": {"id": job.get("id"), "height": job.get("height"),
                "aux_chains": job.get("aux", [])},
        "shit_aux": {"state": aux.get("state"), "reason": aux.get("reason"),
                     "active": aux.get("state") == "ready"},
        "payouts_enabled": bool((live or {}).get("payouts_enabled", False)),
        "payout_method": FEES["payout_method"],
    }


def _hashrate(con, window_s, now):
    d = con.execute(
        "SELECT COALESCE(SUM(CASE WHEN diff>0 THEN diff ELSE 0 END),0) "
        "FROM shares WHERE t > ?", (now - window_s,)).fetchone()[0]
    return d * 4294967296 / window_s if d else 0.0


def ep_hashrate():
    now = int(time.time())
    con = db_ro()
    try:
        cur = _hashrate(con, HASHRATE_WINDOW_S, now)
        h1 = _hashrate(con, 3600, now)
        acc = con.execute("SELECT COUNT(*) FROM shares WHERE diff>0 AND t>?",
                          (now - HASHRATE_WINDOW_S,)).fetchone()[0]
    finally:
        con.close()
    return {"hashrate_hps": cur, "window_seconds": HASHRATE_WINDOW_S,
            "method": "sum(accepted share diff, last 600s) * 2^32 / 600 "
                      "(the pool's own pool_stats() definition)",
            "hashrate_1h_hps": h1, "accepted_shares_in_window": acc,
            "sampled_at": now}


def ep_hashrate_history(bucket):
    if bucket not in (600, 3600, 86400):
        bucket = 3600
    con = db_ro()
    try:
        rows = con.execute(
            "SELECT (t/?)*? AS b, "
            "COALESCE(SUM(CASE WHEN diff>0 THEN diff ELSE 0 END),0) AS dsum, "
            "SUM(CASE WHEN diff>0 THEN 1 ELSE 0 END) AS acc, "
            "SUM(CASE WHEN diff<0 THEN 1 ELSE 0 END) AS rej "
            "FROM shares GROUP BY b ORDER BY b", (bucket, bucket)).fetchall()
        total = con.execute("SELECT COUNT(*), MIN(t), MAX(t) FROM shares").fetchone()
    finally:
        con.close()
    out = [{"bucket_start": int(r["b"]),
            "hashrate_hps_avg": (r["dsum"] * 4294967296 / bucket) if r["dsum"] else 0.0,
            "accepted": int(r["acc"] or 0), "rejected": int(r["rej"] or 0)}
           for r in rows]
    return {"bucket_seconds": bucket, "history": out,
            "recorded_shares_total": total[0],
            "earliest_share_t": total[1], "latest_share_t": total[2],
            "history_limit": "pool.py prunes shares older than 7 days; "
                              "history can never exceed that retention."}


def ep_workers():
    now = int(time.time())
    con = db_ro()
    try:
        miners = con.execute(
            "SELECT COUNT(DISTINCT miner) FROM workers WHERE last_seen > ?",
            (now - ONLINE_WINDOW_S,)).fetchone()[0]
        workers = con.execute(
            "SELECT COUNT(*) FROM workers WHERE last_seen > ?",
            (now - ONLINE_WINDOW_S,)).fetchone()[0]
        known = con.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
    finally:
        con.close()
    return {"miners_online": miners, "workers_online": workers,
            "online_window_seconds": ONLINE_WINDOW_S,
            "worker_rows_total": known,
            "note": "aggregate counts only; worker rows include test-era "
                    "registrations, so rows_total is not a miner count. "
                    "Online uses the pool's own 600 s last_seen window."}


def ep_shares():
    now = int(time.time())
    con = db_ro()
    try:
        acc = con.execute("SELECT COUNT(*) FROM shares WHERE diff>0").fetchone()[0]
        rej = con.execute("SELECT COUNT(*) FROM shares WHERE diff<0").fetchone()[0]
        acc24 = con.execute("SELECT COUNT(*) FROM shares WHERE diff>0 AND t>?",
                            (now - 86400,)).fetchone()[0]
        rej24 = con.execute("SELECT COUNT(*) FROM shares WHERE diff<0 AND t>?",
                            (now - 86400,)).fetchone()[0]
    finally:
        con.close()
    live = pool_stats_live() or {}
    counters = (live.get("abuse") or {}).get("counters") or {}
    return {"recorded": {"accepted_total": acc, "rejected_total": rej,
                         "accepted_24h": acc24, "rejected_24h": rej24,
                         "source": "pool.db shares table (diff>0 accepted; "
                                   "diff<0 = recorded reject marker)"},
            "reject_reasons": {},
            "reject_reasons_note": "pool.db records rejects as diff=-1 rows "
                                   "with no reason column; reasons exist "
                                   "only transiently in stratum responses "
                                   "and are not recoverable from the records.",
            "pool_process_counters": {
                "shares_accepted": counters.get("shares_accepted"),
                "shares_rejected": counters.get("shares_rejected"),
                "note": "in-memory counters of the running pool process "
                        "(reset on restart); may differ from recorded DB "
                        "totals — e.g. duplicate shares are counted in "
                        "memory but return before any DB row is written."}}


def ep_blocks(limit):
    limit = max(1, min(int(limit or 50), 200))
    con = db_ro()
    try:
        rows = con.execute(
            "SELECT chain, height, hash, t FROM blocks ORDER BY t DESC "
            "LIMIT ?", (limit,)).fetchall()
        total = con.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        by_chain = con.execute(
            "SELECT chain, COUNT(*) c FROM blocks GROUP BY chain").fetchall()
    finally:
        con.close()
    return {"blocks": [{"chain": r["chain"], "height": r["height"],
                        "hash": r["hash"], "time": r["t"]} for r in rows],
            "total": total,
            "by_chain": {r["chain"]: r["c"] for r in by_chain},
            "note": "finder/address fields exist in pool.db and are "
                    "deliberately never returned by this API."}


def ep_fees():
    return dict(FEES)


def ep_payouts():
    live = pool_stats_live() or {}
    return {"payouts_enabled": bool(live.get("payouts_enabled", False)),
            "method": FEES["payout_method"],
            "thresholds": FEES["payout_thresholds"],
            "threshold_sats": FEES["payout_threshold_sats"],
            "schedule": FEES["payout_schedule"],
            "interval_seconds": FEES["payout_interval_seconds"],
            "note": "payouts_enabled is the pool's live flag (currently "
                    "false: payouts are NOT armed and no payout will occur). "
                    "No per-miner balances or payout rows are exposed."}


ROUTES = {"/api/v1/pool/status": ep_status,
          "/api/v1/pool/hashrate": ep_hashrate,
          "/api/v1/pool/workers": ep_workers,
          "/api/v1/pool/shares": ep_shares,
          "/api/v1/pool/fees": ep_fees,
          "/api/v1/pool/payouts": ep_payouts}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        obj = dict(obj)
        obj["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if code >= 400 and "error" in obj:
            obj["code"] = code
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=10")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path.rstrip("/") or "/"
        q = parse_qs(u.query)
        try:
            if p in ("/api/v1/pool", "/"):
                return self._send(200, {"api": "shitcoin-pool public API v1",
                    "pool": "node2 BTC stratum pool (separate from node1 "
                            "SHIT web-miner pool)",
                    "endpoints": sorted(ROUTES) + [
                        "/api/v1/pool/hashrate/history",
                        "/api/v1/pool/blocks", "/health"]})
            if p == "/health":
                return self._send(200, {"ok": True})
            if p == "/api/v1/pool/hashrate/history":
                b = int((q.get("bucket") or [3600])[0])
                return self._send(200, ep_hashrate_history(b))
            if p == "/api/v1/pool/blocks":
                lim = int((q.get("limit") or [50])[0])
                return self._send(200, ep_blocks(lim))
            if p in ROUTES:
                return self._send(200, ROUTES[p]())
            return self._send(404, {"error": "not found"})
        except ValueError:
            return self._send(400, {"error": "invalid numeric parameter"})
        except Exception:
            return self._send(500, {"error": "internal error"})

    def _method_not_allowed(self):
        self._send(405, {"error": "method not allowed: read-only API"})

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
