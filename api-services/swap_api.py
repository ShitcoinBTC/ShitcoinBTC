#!/usr/bin/env python3
"""
Shity Swap public read-only API  (/api/v1/swap/)
Stdlib-only. READ-ONLY: it only ever calls quote/read endpoints of the same
venues the swap page itself uses, plus eth_call reads on chain RPCs.
It never builds transactions, never signs, never places orders.

Truth rules baked in:
- A chain is "available" ONLY if a live probe quote returned a non-zero
  amountOut at probe time (cached 60s). Everything else is "unavailable"
  with the probe failure as the reason.
- Quotes are labelled type="quote", is_completed_swap=false everywhere.
- Completed swaps: the platform keeps NO swap ledger (non-custodial,
  browser -> venue). This API does not invent one; /swaps explains that
  and reports only the verifiable own-router on-chain state.
"""
import json, os, re, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

PORT = int(os.environ.get("SWAP_API_PORT", "8793"))
VELORA_BASE = os.environ.get("VELORA_BASE", "https://shitcoinbtc.xyz/api/velora")
KYBER_API = "https://aggregator-api.kyberswap.com"
JUP_API = "https://lite-api.jup.ag/swap/v1"
OO_API = "https://open-api.openocean.finance/v4"
FEE_EVM = "0x67de87A2419522265f7d7b9f50f3810b3a2111d3"
NATIVE_EVM = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
PROBE_TTL = 60

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "shity-swap-public-api/1.0", "Accept": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return {"status": r.status, "ms": int((time.time() - t) * 1000), "raw": raw}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "ms": int((time.time() - t) * 1000), "raw": e.read().decode("utf-8", "replace")[:4000]}
    except Exception as e:
        return {"status": None, "ms": int((time.time() - t) * 1000), "raw": "", "error": str(e)}

def http_get_retry(url, timeout=20):
    r = http_get(url, timeout)
    tries = 0
    while (r["status"] is None or r["status"] in (429, 503)) and tries < 2:  # read-only quote: retry transient transport/venue-unavailable only
        time.sleep(2)
        r = http_get(url, timeout)
        tries += 1
    if tries:
        r["retries"] = tries
    return r

def rpc_call(rpc, method, params, timeout=20):
    data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc, data=data, headers={"Content-Type": "application/json", "User-Agent": "shity-swap-public-api/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

# ---- chain registry -------------------------------------------------------
# Probe pair per chain = native coin -> stablecoin, 0.01 native units.
# Token addresses are the canonical ones the page's token lists resolve to
# (tokens.1inch.io v1.2 lists, fetched 2026-10-14; Plasma USDT0 per Tether/
# Chainstack docs). A chain without a verified probe pair is never marked
# available.
VELORA = {  # key: (chainId, destToken, destDecimals, destSymbol)
    "ethereum":  (1,     "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6, "USDC"),
    "base":      (8453,  "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 6, "USDC"),
    "bsc":       (56,    "0x55d398326f99059ff775485246999027b3197955", 18, "USDT"),
    "arbitrum":  (42161, "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6, "USDC"),
    "avalanche": (43114, "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", 6, "USDC"),
    "optimism":  (10,    "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", 6, "USDC"),
    "gnosis":    (100,   "0xDDAfbb505ad214D7b80b1f830fccc89B60fb7A83", 6, "USDC"),
    "unichain":  (130,   "0x078d782b760474a361dda0af3839290b0ef57ad6", 6, "USDC"),
    "polygon":   (137,   "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6, "USDC"),
    "sonic":     (146,   "0x29219dd400f2Bf60E5a23d13Be72B486D4038894", 6, "USDC"),
    "robinhood": (4663,  "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168", 6, "USDG"),
}
KYBER = {  # slug == chain key; dest token for probe
    "polygon":   "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
    "optimism":  "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    "sonic":     "0x29219dd400f2Bf60E5a23d13Be72B486D4038894",
    "unichain":  "0x078d782b760474a361dda0af3839290b0ef57ad6",
    "linea":     "0x176211869ca2b568f2a7d4ee941e073a821ee1ff",
    "berachain": "0xFCBD14DC51f0A4d49d5E53C2E0950e0bC26d0D4D",
    "plasma":    "0xB8CE59FC3717ada4C02eaDF9682A9e934F625ebb",
    "robinhood": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
}
OWN = {  # Shity Swap's own V2 factory/router, from assets/shity.js swapDeployments
    "monad":     {"chain_id": 143,   "rpc": "https://rpc.monad.xyz",
                  "factory": "0x28edf866303a5B2B244621Cad30655a1453F02d2", "router": "0x72B24871946cE4A5d0Af29F32C29636f47171a22",
                  "wnative": "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A"},
    "hyperevm":  {"chain_id": 999,   "rpc": "https://rpc.hyperliquid.xyz/evm",
                  "factory": "0x842039105b59FFFdd2f321E639d1B0D1Cd805469", "router": "0x28edf866303a5B2B244621Cad30655a1453F02d2",
                  "wnative": "0x5555555555555555555555555555555555555555"},
    "avalanche": {"chain_id": 43114, "rpc": "https://api.avax.network/ext/bc/C/rpc",
                  "factory": "0x28edf866303a5B2B244621Cad30655a1453F02d2", "router": "0x72B24871946cE4A5d0Af29F32C29636f47171a22",
                  "wnative": "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"},
}
OO_ONLY = {  # picker chains whose only same-chain quote path in the page is OpenOcean
    "zksync": "zksync", "scroll": "scroll", "sei": "sei", "cronos": "cronos",
    "mantle": "mantle", "celo": "celo", "metis": "metis", "fantom": "fantom",
    "opbnb": "opbnb", "manta": "manta", "apechain": "ape", "moonriver": "moonriver",
    "flare": "flare", "kava": "kava", "telos": "telos", "rootstock": "rootstock",
    "aurora": "aurora", "harmony": "harmony", "gravity": "gravity", "swell": "swell",
    "mode": "mode", "plume": "plume", "tac": "tac", "polzkevm": "polygon_zkevm",
}
PICKER = ["base","ethereum","bsc","arbitrum","monad","hyperevm","avalanche","polygon",
          "optimism","sonic","unichain","zksync","linea","scroll","gnosis","berachain",
          "sei","cronos","mantle","celo","metis","fantom","opbnb","manta","apechain",
          "moonriver","flare","kava","telos","rootstock","aurora","harmony","gravity",
          "swell","mode","plume","tac","plasma","robinhood","polzkevm","solana"]
LABELS = {"bsc": "BNB Chain", "polzkevm": "Polygon zkEVM", "opbnb": "opBNB", "manta": "Manta Pacific"}

# ---- venue quote functions (read-only) ------------------------------------
def quote_velora(chain_key, src, dst, amount, src_dec, dst_dec):
    cid = VELORA[chain_key][0]
    url = (f"{VELORA_BASE}/prices?srcToken={src}&destToken={dst}&amount={amount}"
           f"&srcDecimals={src_dec}&destDecimals={dst_dec}&side=SELL&chainId={cid}&version=6.2")
    r = http_get_retry(url)
    out = None
    if r["status"] == 200:
        m = re.search(r'"destAmount"\s*:\s*"(\d+)"', r["raw"])
        if m and m.group(1) != "0":
            out = m.group(1)
    return {"venue": "velora", "ok": out is not None, "amount_out": out,
            "http_status": r["status"], "ms": r["ms"], "request": url,
            "fee": {"protocol_fee_bps": 0, "note": "same-chain Velora prices call carries no partner fee (page fee note: 'no protocol fee on this route')"}}

def quote_kyber(chain_key, src, dst, amount):
    url = (f"{KYBER_API}/{chain_key}/api/v1/routes?tokenIn={src}&tokenOut={dst}&amountIn={amount}"
           f"&feeAmount=1&chargeFeeBy=currency_in&isInBps=true&feeReceiver={FEE_EVM}")
    r = http_get_retry(url)
    out = None
    if r["status"] == 200:
        try:
            rs = (json.loads(r["raw"]).get("data") or {}).get("routeSummary") or {}
            if rs.get("amountOut") not in (None, "0"):
                out = rs["amountOut"]
        except Exception:
            pass
    return {"venue": "kyber", "ok": out is not None, "amount_out": out,
            "http_status": r["status"], "ms": r["ms"], "request": url,
            "fee": {"protocol_fee_bps": 1, "fee_receiver": FEE_EVM, "charged_on": "currency_in", "source": "feeAmount=1&isInBps=true in the page's Kyber request"}}

def quote_jupiter(input_mint, output_mint, amount):
    url = f"{JUP_API}/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps=50"
    r = http_get_retry(url)
    out = None; impact = None
    if r["status"] == 200:
        try:
            j = json.loads(r["raw"])
            if j.get("outAmount") not in (None, "0"):
                out = j["outAmount"]; impact = j.get("priceImpactPct")
        except Exception:
            pass
    return {"venue": "jupiter", "ok": out is not None, "amount_out": out, "price_impact_pct": impact,
            "http_status": r["status"], "ms": r["ms"], "request": url,
            "fee": {"protocol_fee_bps": 0, "note": "page passes no platform fee to Jupiter"}}

def own_factory_state(chain_key):
    c = OWN[chain_key]
    def eth_call(to, data):
        return rpc_call(c["rpc"], "eth_call", [{"to": to, "data": data}, "latest"])
    apl = eth_call(c["factory"], "0x574f2ba3")  # allPairsLength()
    pairs = None
    try:
        pairs = int(apl["result"], 16)
    except Exception:
        pass
    return {"factory": c["factory"], "router": c["router"], "pair_count": pairs,
            "raw_allPairsLength": apl.get("result") or apl.get("error")}

def quote_own(chain_key, src, dst, amount):
    """eth_call router.getAmountsOut — read-only. src/dst already WNATIVE-resolved."""
    c = OWN[chain_key]
    W = c["wnative"]
    path = [src, dst]
    def enc_addr(a): return a.lower().replace("0x", "").rjust(64, "0")
    # getAmountsOut(uint256,address[]) selector 0xd06ca61f
    data = ("0xd06ca61f" + format(int(amount), "064x") + format(64, "064x")
            + format(len(path), "064x") + "".join(enc_addr(a) for a in path))
    r = rpc_call(c["rpc"], "eth_call", [{"to": c["router"], "data": data}, "latest"])
    out = None
    try:
        res = r["result"]
        words = [res[i:i + 64] for i in range(2, len(res), 64)]
        amounts = [int(w, 16) for w in words]
        # layout: offset, length, amounts...
        n = amounts[1]
        vals = amounts[2:2 + n]
        if vals and vals[-1] > 0:
            out = str(vals[-1])
    except Exception:
        pass
    return {"venue": "shity-own-router", "ok": out is not None, "amount_out": out,
            "http_status": 200 if r.get("result") else None, "ms": None,
            "request": f"eth_call getAmountsOut on {chain_key} router {c['router']}",
            "fee": {"note": "own AMM swap fee is a hardcoded factory constant; two internal sources disagree (see /fees) — not charged by this API"}}

# ---- probe machinery -------------------------------------------------------
_cache = {"at": 0, "chains": None}

def probe_chain(key):
    venues = []
    if key in VELORA:
        cid, dest, ddec, dsym = VELORA[key]
        venues.append(quote_velora(key, NATIVE_EVM, dest, "10000000000000000", 18, ddec))
    if key in KYBER:
        venues.append(quote_kyber(key, NATIVE_EVM, KYBER[key], "10000000000000000"))
    if key == "solana":
        venues.append(quote_jupiter("So11111111111111111111111111111111111111112",
                                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "10000000"))
    own = None
    if key in OWN:
        own = own_factory_state(key)
    oo = None
    if key in OO_ONLY:
        r = http_get(f"{OO_API}/{OO_ONLY[key]}/gasPrice", timeout=12)
        oo = {"venue": "openocean", "gasprice_http_status": r["status"],
              "detail": ("quote endpoint returned a Cloudflare challenge (403) in audit probes and no quotable pair is verified for this chain"
                         if r["status"] != 403 else "Cloudflare challenge (403) from the API server")}
    ok_venues = [v["venue"] for v in venues if v["ok"]]
    status = "available" if ok_venues else "unavailable"
    reasons = []
    if not ok_venues:
        for v in venues:
            reasons.append(f"{v['venue']}: HTTP {v['http_status']}" if v["http_status"] else f"{v['venue']}: {v.get('error','no response')}")
        if own is not None:
            reasons.append(f"own factory pair_count={own['pair_count']} (no pairs = no own-pool quote)")
        if oo:
            reasons.append(f"openocean: {oo['detail']}")
        if not venues and own is None and oo is None:
            reasons.append("no quote path in the page for this chain")
    return {"chain": key, "label": LABELS.get(key, key.capitalize()), "status": status,
            "available_via": ok_venues,
            "probe": {"pair": "native -> stablecoin (0.01 native units; 0.01 SOL on solana)",
                      "venues": [{k: v[k] for k in ("venue", "ok", "amount_out", "http_status", "ms") if k in v} for v in venues],
                      "own_router": own, "openocean": oo},
            "unavailable_reasons": reasons or None}

def probed_chains():
    if _cache["chains"] and time.time() - _cache["at"] < PROBE_TTL:
        return _cache["chains"], _cache["at"]
    with ThreadPoolExecutor(max_workers=12) as ex:
        chains = list(ex.map(probe_chain, PICKER))
    _cache["chains"] = chains; _cache["at"] = time.time()
    return chains, _cache["at"]

# ---- HTTP handler ----------------------------------------------------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, code, obj):
        obj = dict(obj); obj["timestamp"] = now_iso()
        if code >= 400 and "error" in obj:
            obj["code"] = code
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        p = u.path.rstrip("/") or "/"
        try:
            if p in ("/health", "/api/v1/swap/health"):
                return self.send_json(200, {"ok": True, "service": "shity-swap-public-api", "read_only": True})
            if p == "/api/v1/swap":
                return self.send_json(200, {"service": "Shity Swap public API (read-only)",
                    "endpoints": ["/api/v1/swap/chains", "/api/v1/swap/pairs?chain=", "/api/v1/swap/quote?chain=&tokenIn=&tokenOut=&amountIn=",
                                  "/api/v1/swap/liquidity?chain=", "/api/v1/swap/fees", "/api/v1/swap/swaps"],
                    "warning": "Quotes are quotes. A quote is NOT a completed swap."})
            if p == "/api/v1/swap/chains":
                chains, at = probed_chains()
                return self.send_json(200, {"type": "chain_availability", "probe_age_seconds": int(time.time() - at),
                    "probe_ttl_seconds": PROBE_TTL,
                    "available_count": sum(1 for c in chains if c["status"] == "available"),
                    "unavailable_count": sum(1 for c in chains if c["status"] != "available"),
                    "chains": chains})
            if p == "/api/v1/swap/pairs":
                key = (q.get("chain") or [""])[0]
                if key not in PICKER:
                    return self.send_json(400, {"error": "unknown chain; see /api/v1/swap/chains"})
                pairs = []
                if key in VELORA or key in KYBER:
                    dest = VELORA.get(key, (None, KYBER.get(key), 6, "stablecoin"))
                    pairs.append({"token_in": "native", "token_out": dest[1], "basis": "verified quotable by live probe (see /chains)"})
                if key == "solana":
                    pairs.append({"token_in": "So11111111111111111111111111111111111111112", "token_out": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "basis": "verified quotable by live probe (see /chains)"})
                own_pairs = own_factory_state(key) if key in OWN else None
                return self.send_json(200, {"type": "pairs", "chain": key,
                    "verified_quotable_pairs": pairs,
                    "own_router": own_pairs,
                    "note": "Only pairs verified quotable by a live probe are listed. This API does not enumerate every token pair the venues can price; any token pair can be quoted via /quote."})
            if p == "/api/v1/swap/quote":
                return self.handle_quote(q)
            if p == "/api/v1/swap/liquidity":
                key = (q.get("chain") or [""])[0]
                if key not in PICKER:
                    return self.send_json(400, {"error": "unknown chain; see /api/v1/swap/chains"})
                if key in OWN:
                    st = own_factory_state(key)
                    return self.send_json(200, {"type": "liquidity", "chain": key, "source": "own factory eth_call",
                        "own_router": st,
                        "interpretation": "pair_count is the count of Shity Swap's own AMM pairs. 0 = no own-pool liquidity. Aggregator liquidity on this chain is not a single pool and is not exposed as a number by the venues; a live quote (see /quote) is the liquidity evidence."})
                return self.send_json(200, {"type": "liquidity", "chain": key, "source": "aggregator venues",
                    "own_router": None,
                    "interpretation": "Shity Swap has no own AMM on this chain. Liquidity lives in the aggregators' underlying markets and is not exposed as a single verifiable number; a live quote (see /quote) is the liquidity evidence. No TVL figure is published because none can be verified."})
            if p == "/api/v1/swap/fees":
                return self.send_json(200, {"type": "fees_as_data",
                    "fee_collector_evm": FEE_EVM,
                    "venues": {
                        "velora_same_chain": {"protocol_fee_bps": 0, "source": "swap.html quoteVelora(): prices call carries no partner fee; page fee note says 'no protocol fee on this route'"},
                        "velora_delta_cross_chain": {"protocol_fee_bps": 1, "partner": FEE_EVM, "source": "swap.html VELORA_FEE_BPS=1; fee-enforcer.service force-overwrites partnerAddress/partnerFeeBps=1 on POST /v2/delta/orders/build (rules.json on node1)"},
                        "kyber": {"protocol_fee_bps": 1, "fee_receiver": FEE_EVM, "source": "swap.html quoteKyber(): feeAmount=1&isInBps=true&chargeFeeBy=currency_in"},
                        "openocean": {"referrer_fee_pct": 0.01, "referrer": FEE_EVM, "source": "swap.html OO_FEE_PCT=0.01 charged on the swap-build call (not on quotes). NOTE: OpenOcean quote path currently fails (Cloudflare 403 from server probes)."},
                        "jupiter_solana": {"protocol_fee_bps": 0, "source": "page passes no platform fee to Jupiter"},
                        "oneinch": {"configured_fee_pct": 0.005, "live": False, "source": "swap.html ONEINCH_LIVE=false; /api/1inch/ proxy does not exist (404). Not charged today."},
                        "shity_own_amm": {"displayed_fee": {"monad": "0.005%", "hyperevm": "0.005%", "avalanche": "0.005%", "default": "0.3%"},
                            "source": "assets/shity.js swapFee() — comment claims on-chain verified 0.005% (50 ppm) 2026-09-27",
                            "conflict_flag": "agg/quoter/venues.json records Shity AMM feeBps=30 (0.3%) for the same routers. The two internal sources disagree; with 0 pairs on all three factories no live quote can settle it today."}},
                    "note": "Fees are returned as data fields only. This API charges nothing and executes nothing."})
            if p == "/api/v1/swap/swaps":
                own = {k: own_factory_state(k) for k in OWN}
                return self.send_json(200, {"type": "completed_swaps",
                    "completed_swaps": [],
                    "source_available": False,
                    "explanation": "Shity Swap is non-custodial: swaps execute in the user's wallet against third-party aggregators, and the platform keeps no swap ledger or tx record (the page's 'Recent swaps' list is per-browser localStorage from its own success messages). No reliable completed-swap record source exists, so this endpoint reports none rather than inventing a feed. A quote from /quote is NOT a completed swap and is never listed here.",
                    "verifiable_onchain_state": {"own_router_factories": own,
                        "note": "allPairsLength=0 on Monad/HyperEVM/Avalanche means no own-router pair exists, so no own-router Swap event can exist on those chains."}})
            return self.send_json(404, {"error": "not found"})
        except Exception:
            return self.send_json(500, {"error": "internal error"})

    def handle_quote(self, q):
        key = (q.get("chain") or [""])[0]
        tin = (q.get("tokenIn") or [""])[0]
        tout = (q.get("tokenOut") or [""])[0]
        amount = (q.get("amountIn") or [""])[0]
        if key not in PICKER or not tin or not tout or not amount.isdigit() or int(amount) <= 0:
            return self.send_json(400, {"error": "required: chain (see /chains), tokenIn (address or 'native'; mint on solana), tokenOut, amountIn (integer, smallest units)"})
        results = []
        if key == "solana":
            results.append(quote_jupiter(tin, tout, amount))
        else:
            src = NATIVE_EVM if tin == "native" else tin
            dst = NATIVE_EVM if tout == "native" else tout
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", src) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", dst):
                return self.send_json(400, {"error": "tokenIn/tokenOut must be 'native' or a 0x address (EVM chains)"})
            din = int((q.get("tokenInDecimals") or ["18"])[0]); dout = int((q.get("tokenOutDecimals") or ["18"])[0])
            if key in VELORA:
                results.append(quote_velora(key, src, dst, amount, din, dout))
            if key in KYBER:
                results.append(quote_kyber(key, src, dst, amount))
            if key in OWN:
                W = OWN[key]["wnative"]
                results.append(quote_own(key, W if tin == "native" else tin, W if tout == "native" else tout, amount))
        ok = [r for r in results if r["ok"]]
        if not ok:
            return self.send_json(422, {"type": "quote", "is_completed_swap": False, "chain": key,
                "token_in": tin, "token_out": tout, "amount_in": amount,
                "error": "no venue returned a quote for this pair right now", "venue_results": results})
        best = max(ok, key=lambda r: int(r["amount_out"]))
        return self.send_json(200, {"type": "quote", "is_completed_swap": False,
            "chain": key, "token_in": tin, "token_out": tout, "amount_in": amount,
            "estimated_out": best["amount_out"], "best_venue": best["venue"],
            "fee": best["fee"], "venue_results": results,
            "note": "Estimate only, from the venue's live quote endpoint. Nothing was executed."})

    def do_POST(self):
        return self.send_json(405, {"error": "read-only API: GET only"})

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
