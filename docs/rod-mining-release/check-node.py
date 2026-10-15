#!/usr/bin/env python3
"""
check-node.py -- ShitcoinBTC pool: Bitcoin Core node setup / status checker.

READ-ONLY. This script only *reads* from a Bitcoin Core node:
  getblockchaininfo, getchainstates (if available), getblocktemplate.
It never writes, never loads a snapshot, never sends transactions, and
never prints your RPC password.

What it checks
  1. Dependencies present (python3, and bitcoin-cli if --cli mode is used)
  2. Node RPC is reachable
  3. chain == "main"            -- FAILS LOUDLY on test / testnet4 / regtest / signet
  3b. BITCOIN chain identity    -- getblockhash 0 must be the real Bitcoin
     genesis block. This is what separates Bitcoin mainnet from any OTHER
     chain that also calls itself "main" (the ShitcoinBTC daemon does).
  4. Sync state: blocks vs headers, verificationprogress, initialblockdownload
  5. Snapshot (assumeutxo) state, if the node reports it via getchainstates
  6. Pool-can-obtain-work: getblocktemplate returns a valid mainnet template
     for the next block (height = tip + 1, previousblockhash = tip hash)

Usage -- pick ONE way to give it RPC access:

  A) Environment variables (recommended; keeps the password out of shell history):
       export BTC_RPC_USER=youruser
       export BTC_RPC_PASS=yourpassword
       export BTC_RPC_HOST=127.0.0.1      # optional, default 127.0.0.1
       export BTC_RPC_PORT=8332           # optional, default 8332 (mainnet)
       python3 check-node.py

  B) Read rpcuser/rpcpassword/rpcport straight from a bitcoin.conf:
       python3 check-node.py --conf ~/.bitcoin/bitcoin.conf
       python3 check-node.py --datadir ~/.bitcoin      # same, via datadir

  C) Explicit flags (password still via env, so it is not in `ps` output):
       BTC_RPC_PASS=yourpassword python3 check-node.py --host 127.0.0.1 --port 8332 --user youruser

Exit codes: 0 = PASS (pool can use this node), 1 = FAIL, 2 = PASS WITH WARNINGS.
Timeouts: node RPC can be slow while a snapshot is still validating in the
background (our reference node sometimes takes 10-45s per call). Default
per-call timeout is 60s; raise it with --timeout if your node is busy.
"""

import argparse
import json
import os
import shutil
import sys
import urllib.request
import urllib.error

RESULTS = []  # (status, name, detail)  status in PASS / FAIL / WARN / INFO


def report(status, name, detail=""):
    RESULTS.append((status, name, detail))
    line = f"[{status}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line, flush=True)


def parse_conf(path):
    """Parse bitcoin.conf for RPC settings. Main section only (ignores
    [test]/[regtest] etc. sections are NOT read; top-level keys win).
    Returns dict. Password value is used for auth but NEVER printed."""
    cfg = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        in_section = False
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                in_section = True
                continue
            if in_section:
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


class Rpc:
    def __init__(self, host, port, user, password, timeout):
        self.url = f"http://{host}:{port}/"
        self.timeout = timeout
        import base64
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = f"Basic {token}"
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        body = json.dumps(
            {"jsonrpc": "1.0", "id": self._id, "method": method, "params": params or []}
        ).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": self.auth},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode())
        if data.get("error"):
            raise RuntimeError(f"{method}: {data['error']}")
        return data["result"]


def main():
    ap = argparse.ArgumentParser(description="Read-only Bitcoin Core checker for pool operators")
    ap.add_argument("--host", default=os.environ.get("BTC_RPC_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--user", default=os.environ.get("BTC_RPC_USER", ""))
    ap.add_argument("--conf", default=None, help="path to bitcoin.conf to read credentials from")
    ap.add_argument("--datadir", default=None, help="bitcoind datadir (uses <datadir>/bitcoin.conf)")
    ap.add_argument("--timeout", type=float, default=60.0, help="per-RPC-call timeout seconds (default 60)")
    args = ap.parse_args()

    password = os.environ.get("BTC_RPC_PASS", "")
    user = args.user
    port = args.port

    conf_path = args.conf
    if args.datadir and not conf_path:
        conf_path = os.path.join(args.datadir, "bitcoin.conf")
    conf = {}
    if conf_path:
        if not os.path.isfile(conf_path):
            report("FAIL", "bitcoin.conf readable", f"not found: {conf_path}")
            return finish()
        try:
            conf = parse_conf(conf_path)
        except OSError as exc:
            report("FAIL", "bitcoin.conf readable", str(exc))
            return finish()
        report("PASS", "bitcoin.conf readable", conf_path)
        if not user:
            user = conf.get("rpcuser", "")
        if not password:
            password = conf.get("rpcpassword", "")
        if port is None and conf.get("rpcport"):
            try:
                port = int(conf["rpcport"])
            except ValueError:
                pass
        # Surface non-secret config facts the pool cares about (values only).
        for key in ("chain", "testnet", "regtest", "signet", "prune", "server", "dbcache"):
            if key in conf:
                report("INFO", f"conf {key}", conf[key])
        if conf.get("rpcauth") and not user:
            report("WARN", "RPC credentials",
                   "conf uses rpcauth= (hashed) -- this script needs the matching "
                   "plaintext user/password via BTC_RPC_USER / BTC_RPC_PASS")

    if port is None:
        port = int(os.environ.get("BTC_RPC_PORT", "8332"))

    # ---- Check 1: dependencies -------------------------------------------
    missing = []
    if sys.version_info < (3, 8):
        missing.append("python>=3.8")
    cli = shutil.which("bitcoin-cli")
    if missing:
        report("FAIL", "dependencies", "missing: " + ", ".join(missing))
    else:
        report("PASS", "dependencies",
               f"python {sys.version_info.major}.{sys.version_info.minor}; "
               f"bitcoin-cli: {cli or 'not in PATH (not required for RPC mode)'}")

    if not user or not password:
        report("FAIL", "RPC credentials supplied",
               "set BTC_RPC_USER/BTC_RPC_PASS, or use --conf/--datadir with rpcuser/rpcpassword")
        return finish()

    rpc = Rpc(args.host, port, user, password, args.timeout)

    # ---- Check 2: RPC reachable ------------------------------------------
    try:
        info = rpc.call("getblockchaininfo")
    except Exception as exc:  # noqa: BLE001 - report any failure verbatim
        report("FAIL", "RPC reachable", f"{args.host}:{port} -- {exc}")
        return finish()
    report("PASS", "RPC reachable", f"{args.host}:{port}")

    # ---- Check 3: chain == main ------------------------------------------
    chain = info.get("chain", "?")
    if chain != "main":
        print()
        print("!" * 72)
        print(f"!!! WRONG CHAIN: this node is on '{chain}', NOT Bitcoin mainnet !!!")
        print("!!! The pool will NOT use this node. Do NOT point miners at it. !!!")
        print("!!! Remove chain/testnet/regtest/signet settings and restart on main. !!!")
        print("!" * 72)
        report("FAIL", "chain is mainnet", f"chain='{chain}'")
        return finish()
    report("PASS", "chain is mainnet", "chain='main'")

    # ---- Check 3b: BITCOIN chain identity (genesis hash) ------------------
    # chain == "main" is NOT identity: the ShitcoinBTC daemon (a Syscoin/
    # Bitcoin fork) also reports chain="main" -- for ITS mainnet. Block 0 is
    # identity: only the real Bitcoin chain starts from the Bitcoin genesis
    # block. A pruned node retains the block index, so this works pruned too.
    BITCOIN_GENESIS = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
    BITCOIN_935000 = "0000000000000000000147034958af1652b2b91bba607beacc5e72a56f0fb5ee"
    identity_problems = []
    try:
        genesis = rpc.call("getblockhash", [0])
        if genesis != BITCOIN_GENESIS:
            identity_problems.append(
                f"getblockhash 0 = {genesis}, expected Bitcoin genesis {BITCOIN_GENESIS}")
    except Exception as exc:  # noqa: BLE001
        identity_problems.append(f"getblockhash 0 failed ({exc}) -- cannot prove this is Bitcoin")
    tip_height = info.get("blocks")
    if not identity_problems and isinstance(tip_height, int) and tip_height >= 935000:
        try:
            h935 = rpc.call("getblockhash", [935000])
            if h935 != BITCOIN_935000:
                identity_problems.append(
                    f"getblockhash 935000 = {h935}, expected {BITCOIN_935000}")
        except Exception as exc:  # noqa: BLE001
            identity_problems.append(f"getblockhash 935000 failed ({exc})")
    if identity_problems:
        print()
        print("!" * 72)
        print("!!! WRONG CHAIN: this is NOT the Bitcoin chain !!!")
        print("!!! A chain named 'main' is not necessarily Bitcoin mainnet -- !!!")
        print("!!! e.g. a ShitcoinBTC daemon also reports chain='main'.        !!!")
        print("!!! The pool will NOT use this node. Do NOT point miners at it. !!!")
        print("!" * 72)
        report("FAIL", "bitcoin chain identity", "; ".join(identity_problems))
        return finish()
    report("PASS", "bitcoin chain identity",
           "getblockhash 0 is the Bitcoin genesis block"
           + ("; block 935000 also matches" if isinstance(tip_height, int) and tip_height >= 935000 else ""))

    # ---- Check 4: sync state ----------------------------------------------
    blocks = info.get("blocks")
    headers = info.get("headers")
    best = info.get("bestblockhash", "")
    progress = info.get("verificationprogress", 0)
    ibd = info.get("initialblockdownload", True)
    pruned = info.get("pruned", False)
    report("INFO", "tip", f"blocks={blocks} headers={headers} bestblockhash={best}")
    report("INFO", "pruned", str(pruned) + (f" (pruneheight={info.get('pruneheight')})" if pruned else ""))
    if (blocks is not None and headers is not None and blocks == headers
            and not ibd and progress >= 0.999999):
        report("PASS", "synced to tip", f"blocks == headers == {blocks}, verificationprogress={progress:.6f}")
    elif blocks is not None and headers:
        pct = 100.0 * blocks / headers if headers else 0
        report("WARN", "sync in progress",
               f"blocks={blocks} / headers={headers} ({pct:.2f}%), "
               f"verificationprogress={progress:.6f}, initialblockdownload={ibd} "
               "-- node is usable once blocks == headers; pool work follows the tip it has")
    else:
        report("WARN", "sync state", "blocks/headers missing from getblockchaininfo")

    # ---- Check 5: snapshot (assumeutxo) state, if detectable --------------
    try:
        states = rpc.call("getchainstates")
        entries = states.get("chainstates", []) if isinstance(states, dict) else []
        if len(entries) > 1:
            desc = []
            bg_validated = None
            for e in entries:
                is_snap = bool(e.get("snapshot_blockhash") or e.get("from_snapshot_blockhash"))
                role = "snapshot chainstate (live tip)" if is_snap else "background chainstate (validating history)"
                desc.append(f"{role}: height={e.get('blocks')} validated={e.get('validated')}")
                if e.get("validated") is False:
                    bg_validated = False
            report("WARN", "snapshot validation still running",
                   "; ".join(desc) +
                   " -- this is NORMAL after loadtxoutset: the node serves the tip "
                   "now and validates the snapshot history in the background (hours to days). "
                   "Expect slow RPC until it finishes.")
        elif len(entries) == 1:
            report("PASS", "snapshot state",
                   f"single chainstate, validated={entries[0].get('validated')} "
                   "-- no unvalidated snapshot outstanding (either fully synced from scratch "
                   "or background validation has completed)")
        else:
            report("INFO", "snapshot state", "getchainstates returned no chainstate list")
    except Exception as exc:  # noqa: BLE001
        report("INFO", "snapshot state", f"not detectable via getchainstates ({exc}) -- not a failure")

    # ---- Check 6: pool can obtain valid work (getblocktemplate) ----------
    try:
        tpl = rpc.call("getblocktemplate", [{"rules": ["segwit"]}])
    except Exception as exc:  # noqa: BLE001
        report("FAIL", "getblocktemplate (pool can obtain work)", str(exc))
        return finish()

    problems = []
    if blocks is not None and tpl.get("height") != blocks + 1:
        problems.append(f"template height {tpl.get('height')} != tip+1 {blocks + 1}")
    if best and tpl.get("previousblockhash") != best:
        problems.append("template previousblockhash != bestblockhash")
    if not tpl.get("bits"):
        problems.append("template missing bits")
    if not isinstance(tpl.get("coinbasevalue"), int) or tpl.get("coinbasevalue", 0) <= 0:
        problems.append("template coinbasevalue missing/not positive")
    if problems:
        report("FAIL", "getblocktemplate (pool can obtain work)", "; ".join(problems))
    else:
        report("PASS", "getblocktemplate (pool can obtain work)",
               f"height={tpl.get('height')} coinbasevalue={tpl.get('coinbasevalue')} sats "
               f"txs={len(tpl.get('transactions', []))} bits={tpl.get('bits')}")

    return finish()


def finish():
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    print()
    if fails:
        print(f"VERDICT: FAIL -- {len(fails)} failing check(s): " + ", ".join(r[1] for r in fails))
        return 1
    if warns:
        print(f"VERDICT: PASS WITH WARNINGS -- {len(warns)} warning(s): " + ", ".join(r[1] for r in warns))
        return 2
    print("VERDICT: PASS -- this node is on Bitcoin mainnet, synced, and can give the pool valid work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
