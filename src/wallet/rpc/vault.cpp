// Copyright (c) 2026 The Shitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <addresstype.h>
#include <base58.h>
#include <chainparams.h>
#include <consensus/amount.h>
#include <core_io.h>
#include <crypto/sha256.h>
#include <hash.h>
#include <interfaces/chain.h>
#include <key.h>
#include <key_io.h>
#include <outputtype.h>
#include <primitives/transaction.h>
#include <pubkey.h>
#include <rpc/util.h>
#include <script/interpreter.h>
#include <script/script.h>
#include <script/vault.h>
#include <uint256.h>
#include <util/error.h>
#include <common/args.h>
#include <script/signingprovider.h>
#include <util/fs.h>
#include <util/strencodings.h>
#include <util/time.h>
#include <wallet/coincontrol.h>
#include <wallet/rpc/util.h>
#include <wallet/scriptpubkeyman.h>
#include <wallet/spend.h>
#include <wallet/wallet.h>

#include <univalue.h>

// OpenSSL provides the TLS client used by the BTC co-hold check below. The
// node does not otherwise link libssl; see the -lssl -lcrypto additions to
// the link line in src/Makefile.am.
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

#include <algorithm>
#include <cassert>
#include <chrono>
#include <compat/compat.h>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iterator>
#include <limits>
#include <map>
#include <mutex>
#include <string>
#include <util/sock.h>
#include <vector>

namespace wallet {
namespace {

constexpr int64_t VAULT_MULT_SCALE{100000000};
constexpr int BTC_EXPLORER_TIMEOUT_SECONDS{15};
constexpr int64_t BTC_BALANCE_CACHE_SECONDS{600};
const std::string VAULT_CHALLENGE_PREFIX{"shitcoin-vault:"};
const std::string VAULT_RECORDS_FILE{"vaults.json"};

using SteadyClock = std::chrono::steady_clock;
using TimePoint = SteadyClock::time_point;

//! Split "<scheme>://<host>[:port][/path...]" into its parts. Returns false on malformed input.
static bool ParseExplorerUrl(const std::string& url, bool& use_tls, std::string& host, uint16_t& port, std::string& path)
{
    std::string rest;
    if (url.compare(0, 8, "https://") == 0) {
        use_tls = true;
        port = 443;
        rest = url.substr(8);
    } else if (url.compare(0, 7, "http://") == 0) {
        use_tls = false;
        port = 80;
        rest = url.substr(7);
    } else {
        return false;
    }
    const size_t slash = rest.find('/');
    const std::string authority = (slash == std::string::npos) ? rest : rest.substr(0, slash);
    path = (slash == std::string::npos) ? std::string("/") : rest.substr(slash);
    while (path.size() > 1 && path.back() == '/') path.pop_back();
    if (authority.empty()) return false;
    // Split host[:port]; tolerate bracketed IPv6 literals.
    if (authority.front() == '[') {
        const size_t close = authority.find(']');
        if (close == std::string::npos) return false;
        host = authority.substr(1, close - 1);
        if (close + 1 < authority.size()) {
            if (authority[close + 1] != ':') return false;
            const auto p = ToIntegral<uint16_t>(authority.substr(close + 2));
            if (!p) return false;
            port = *p;
        }
    } else {
        const size_t colon = authority.rfind(':');
        if (colon != std::string::npos && authority.find(':') == colon) {
            const auto p = ToIntegral<uint16_t>(authority.substr(colon + 1));
            if (!p) return false;
            port = *p;
            host = authority.substr(0, colon);
        } else {
            host = authority;
        }
    }
    return !host.empty();
}

//! Portable socket close (closesocket on Windows, close elsewhere).
static void VaultCloseSocket(SOCKET s)
{
#ifdef WIN32
    closesocket(s);
#else
    close(s);
#endif
}

//! Put a socket into non-blocking mode. Returns true on success.
static bool VaultSetNonBlocking(SOCKET s)
{
#ifdef WIN32
    u_long mode = 1;
    return ioctlsocket(s, FIONBIO, &mode) == 0;
#else
    const int old_flags = fcntl(s, F_GETFL, 0);
    if (old_flags < 0) return false;
    return fcntl(s, F_SETFL, old_flags | O_NONBLOCK) == 0;
#endif
}

static bool WaitForSocket(SOCKET fd, bool for_write, TimePoint deadline)
{
    const auto now = SteadyClock::now();
    if (now >= deadline) return false;
    const auto remain = std::chrono::duration_cast<std::chrono::microseconds>(deadline - now);
    fd_set fds;
    FD_ZERO(&fds);
    FD_SET(fd, &fds);
    struct timeval tv;
    tv.tv_sec = (long)(remain.count() / 1000000);
    tv.tv_usec = (long)(remain.count() % 1000000);
    // NB: the first select() argument is ignored on Windows.
    const int rc = select((int)fd + 1, for_write ? nullptr : &fds, for_write ? &fds : nullptr, nullptr, &tv);
    return rc > 0;
}

//! Non-blocking connect with a deadline; tries each addrinfo result in turn.
static SOCKET ConnectWithDeadline(const addrinfo* ai, TimePoint deadline, std::string& err)
{
    int last_err = 0;
    for (const addrinfo* p = ai; p != nullptr; p = p->ai_next) {
        const SOCKET fd = socket(p->ai_family, p->ai_socktype, p->ai_protocol);
        if (fd == INVALID_SOCKET) {
            last_err = WSAGetLastError();
            continue;
        }
        if (!VaultSetNonBlocking(fd)) {
            last_err = WSAGetLastError();
            VaultCloseSocket(fd);
            continue;
        }
        int rc = connect(fd, p->ai_addr, (int)p->ai_addrlen);
        // Windows reports WSAEWOULDBLOCK (not WSAEINPROGRESS) for a non-blocking
        // connect() that is still in progress; accept both. On POSIX
        // WSAEWOULDBLOCK is EWOULDBLOCK, which connect() never returns, so the
        // non-Windows behavior is unchanged.
        const int conn_err = WSAGetLastError();
        if (rc < 0 && conn_err != WSAEINPROGRESS && conn_err != WSAEWOULDBLOCK) {
            last_err = WSAGetLastError();
            VaultCloseSocket(fd);
            continue;
        }
        if (rc < 0) {
            if (!WaitForSocket(fd, /*for_write=*/true, deadline)) {
#ifdef WIN32
                last_err = WSAETIMEDOUT;
#else
                last_err = ETIMEDOUT;
#endif
                VaultCloseSocket(fd);
                continue;
            }
            int so_err = 0;
#ifdef WIN32
            int optlen = sizeof(so_err);
#else
            socklen_t optlen = sizeof(so_err);
#endif
            if (getsockopt(fd, SOL_SOCKET, SO_ERROR, (sockopt_arg_type)&so_err, &optlen) != 0 || so_err != 0) {
                last_err = (so_err != 0) ? so_err : WSAGetLastError();
                VaultCloseSocket(fd);
                continue;
            }
        }
        return fd; // connected; socket stays non-blocking
    }
    err = strprintf("BTC explorer: connection failed: %s", NetworkErrorString(last_err));
    return INVALID_SOCKET;
}

//! Non-blocking TLS handshake with certificate chain and hostname verification. Fail closed.
static bool TlsHandshake(SOCKET fd, const std::string& host, SSL_CTX*& ctx_out, SSL*& ssl_out, TimePoint deadline, std::string& err)
{
    ctx_out = nullptr;
    ssl_out = nullptr;
    SSL_CTX* ctx = SSL_CTX_new(TLS_client_method());
    if (!ctx) {
        err = "BTC explorer: TLS context creation failed";
        return false;
    }
    if (SSL_CTX_set_default_verify_paths(ctx) != 1) {
        err = "BTC explorer: cannot load system CA certificates; refusing to connect without chain verification";
        SSL_CTX_free(ctx);
        return false;
    }
    SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER, nullptr);
    SSL_CTX_set_min_proto_version(ctx, TLS1_2_VERSION);
    SSL* ssl = SSL_new(ctx);
    if (!ssl) {
        err = "BTC explorer: TLS session creation failed";
        SSL_CTX_free(ctx);
        return false;
    }
    // SNI, so hosts behind shared infrastructure present the right certificate.
    SSL_set_tlsext_host_name(ssl, host.c_str());
    SSL_set_fd(ssl, (int)fd);
    while (true) {
        const int rc = SSL_connect(ssl);
        if (rc == 1) break;
        const int ssl_err = SSL_get_error(ssl, rc);
        const bool want_read = (ssl_err == SSL_ERROR_WANT_READ);
        if ((want_read || ssl_err == SSL_ERROR_WANT_WRITE) && WaitForSocket(fd, /*for_write=*/!want_read, deadline)) {
            continue;
        }
        char buf[256];
        ERR_error_string_n(ERR_get_error(), buf, sizeof(buf));
        err = strprintf("BTC explorer: TLS handshake failed: %s", buf);
        SSL_free(ssl);
        SSL_CTX_free(ctx);
        return false;
    }
    // Hostname verification against the leaf certificate (not done for us by the handshake).
    bool host_ok = false;
    if (X509* cert = SSL_get_peer_certificate(ssl)) {
        host_ok = X509_check_host(cert, host.c_str(), 0, 0, nullptr) == 1;
        X509_free(cert);
    }
    if (!host_ok) {
        err = "BTC explorer: TLS certificate hostname mismatch";
        SSL_free(ssl);
        SSL_CTX_free(ctx);
        return false;
    }
    ctx_out = ctx;
    ssl_out = ssl;
    return true;
}

static bool SockSendAll(SOCKET fd, SSL* ssl, const std::string& data, TimePoint deadline, std::string& err)
{
    size_t sent = 0;
    while (sent < data.size()) {
        if (SteadyClock::now() >= deadline) {
            err = "BTC explorer: send timeout";
            return false;
        }
        int rc;
        if (ssl) {
            rc = SSL_write(ssl, data.data() + sent, (int)(data.size() - sent));
            if (rc <= 0) {
                const int ssl_err = SSL_get_error(ssl, rc);
                const bool want_read = (ssl_err == SSL_ERROR_WANT_READ);
                if ((want_read || ssl_err == SSL_ERROR_WANT_WRITE) && WaitForSocket(fd, /*for_write=*/!want_read, deadline)) {
                    continue;
                }
                err = "BTC explorer: TLS write failed";
                return false;
            }
        } else {
            rc = send(fd, data.data() + sent, (int)(data.size() - sent), MSG_NOSIGNAL);
            if (rc == SOCKET_ERROR) {
                const int wsa_err = WSAGetLastError();
                if ((wsa_err == WSAEWOULDBLOCK || wsa_err == WSAEINTR) && WaitForSocket(fd, /*for_write=*/true, deadline)) {
                    continue;
                }
                err = strprintf("BTC explorer: send failed: %s", NetworkErrorString(wsa_err));
                return false;
            }
        }
        sent += (size_t)rc;
    }
    return true;
}

static bool SockRecvAll(SOCKET fd, SSL* ssl, std::string& out, TimePoint deadline, std::string& err)
{
    out.clear();
    char buf[8192];
    while (true) {
        if (SteadyClock::now() >= deadline) {
            err = "BTC explorer: receive timeout";
            return false;
        }
        int rc;
        if (ssl) {
            rc = SSL_read(ssl, buf, sizeof(buf));
            if (rc <= 0) {
                const int ssl_err = SSL_get_error(ssl, rc);
                if (ssl_err == SSL_ERROR_ZERO_RETURN) break; // clean TLS shutdown
                const bool want_read = (ssl_err == SSL_ERROR_WANT_READ);
                if ((want_read || ssl_err == SSL_ERROR_WANT_WRITE) && WaitForSocket(fd, /*for_write=*/!want_read, deadline)) {
                    continue;
                }
                err = "BTC explorer: TLS read failed";
                return false;
            }
        } else {
            rc = recv(fd, buf, sizeof(buf), 0);
            if (rc == SOCKET_ERROR) {
                const int wsa_err = WSAGetLastError();
                if ((wsa_err == WSAEWOULDBLOCK || wsa_err == WSAEINTR) && WaitForSocket(fd, /*for_write=*/false, deadline)) {
                    continue;
                }
                err = strprintf("BTC explorer: receive failed: %s", NetworkErrorString(wsa_err));
                return false;
            }
            if (rc == 0) break; // EOF
        }
        if (out.size() + (size_t)rc > (1u << 20)) {
            err = "BTC explorer: response too large";
            return false;
        }
        out.append(buf, rc);
    }
    return true;
}

//! Synchronous HTTP(S) GET with an overall timeout. Any failure -> false (fail closed).
static bool HttpGet(const std::string& url, std::string& body_out, std::string& err)
{
    bool use_tls = false;
    std::string host, path;
    uint16_t port = 0;
    if (!ParseExplorerUrl(url, use_tls, host, port, path)) {
        err = strprintf("BTC explorer: malformed URL (want http(s)://host[:port][/prefix]): %s", url);
        return false;
    }
    const TimePoint deadline = SteadyClock::now() + std::chrono::seconds(BTC_EXPLORER_TIMEOUT_SECONDS);

    addrinfo hints{};
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_family = AF_UNSPEC;
    addrinfo* ai = nullptr;
    if (getaddrinfo(host.c_str(), std::to_string(port).c_str(), &hints, &ai) != 0 || ai == nullptr) {
        err = strprintf("BTC explorer: DNS resolution failed for %s", host);
        return false;
    }
    const SOCKET fd = ConnectWithDeadline(ai, deadline, err);
    freeaddrinfo(ai);
    if (fd == INVALID_SOCKET) return false;

    SSL_CTX* ctx = nullptr;
    SSL* ssl = nullptr;
    if (use_tls && !TlsHandshake(fd, host, ctx, ssl, deadline, err)) {
        VaultCloseSocket(fd);
        return false;
    }

    // HTTP/1.0 with "Connection: close" keeps response parsing trivial
    // (no chunked transfer encoding); explorer responses always fit in memory.
    const std::string request = "GET " + path + " HTTP/1.0\r\n"
                                "Host: " + host + "\r\n"
                                "User-Agent: ShitcoinCore-vault\r\n"
                                "Accept: application/json\r\n"
                                "Connection: close\r\n\r\n";
    const bool ok = SockSendAll(fd, ssl, request, deadline, err) &&
                    SockRecvAll(fd, ssl, body_out, deadline, err);
    if (ssl) {
        SSL_shutdown(ssl);
        SSL_free(ssl);
        SSL_CTX_free(ctx);
    }
    VaultCloseSocket(fd);
    if (!ok) return false;

    // Minimal response validation: status 200 on the status line, then headers/body split.
    const size_t eol = body_out.find("\r\n");
    const size_t ok_pos = body_out.find(" 200 ");
    if (eol == std::string::npos || ok_pos == std::string::npos || ok_pos > eol) {
        err = "BTC explorer: unexpected HTTP status";
        return false;
    }
    const size_t hdr_end = body_out.find("\r\n\r\n");
    if (hdr_end == std::string::npos) {
        err = "BTC explorer: malformed HTTP response";
        return false;
    }
    body_out = body_out.substr(hdr_end + 4);
    return true;
}

// The Shitcoin chain cannot observe the Bitcoin chain; this is enforced by the
// official wallet software, not by consensus.
//
// CheckBTCCoHold proves, as wallet policy, that the user controls a Bitcoin
// P2PKH address with a non-zero balance:
//  (a) ownership: the address must match the key recovered from a 65-byte
//      compact signature over the Bitcoin "Signed Message" hash of `challenge`, and
//  (b) balance: the address's confirmed balance per the configured explorer
//      (-vaultbtcexplorer, default https://blockstream.info/api) must be > 0 sats.
// Any non-zero balance qualifies; there is no price feed. Balance results are
// cached for 10 minutes per address (tradeoff: a balance that moves on-chain
// inside the window is not re-checked), but every network or parse error fails
// closed. Users produce the signature in any Bitcoin wallet (e.g. Electrum) by
// signing the challenge message shown by the vault RPCs.
static std::mutex g_btcCacheMutex;
static std::map<std::string, std::pair<int64_t, bool>> g_btcBalanceCache; // address -> (cache expiry, has_balance)

static uint256 BitcoinMessageHash(const std::string& message)
{
    HashWriter hasher{};
    // NB: the Bitcoin magic, NOT the Syscoin MESSAGE_MAGIC used by MessageHash():
    // the signature is created by a Bitcoin wallet.
    hasher << std::string("Bitcoin Signed Message:\n") << message;
    return hasher.GetHash();
}

bool CheckBTCCoHold(const std::string& btcAddress, const std::string& btcSigBase64, const std::string& challenge, std::string& err)
{
    // (a) Ownership: base58check P2PKH (version 0x00) + compact signature recovery.
    std::vector<unsigned char> vchAddr;
    if (!DecodeBase58Check(btcAddress, vchAddr, 100) || vchAddr.size() != 21 || vchAddr[0] != 0x00) {
        err = "invalid BTC address: must be a base58check P2PKH address (version 0x00)";
        return false;
    }
    const auto sigOpt = DecodeBase64(btcSigBase64);
    if (!sigOpt || sigOpt->size() != 65) {
        err = "invalid BTC signature: must be a 65-byte compact signature, base64-encoded";
        return false;
    }
    CPubKey recovered;
    if (!recovered.RecoverCompact(BitcoinMessageHash(challenge), *sigOpt) || !recovered.IsFullyValid()) {
        err = "BTC signature: could not recover a valid public key";
        return false;
    }
    const uint160 addrHash(Span<const unsigned char>(vchAddr.data() + 1, 20));
    if (Hash160(recovered) != addrHash) {
        err = "BTC signature was not made by the private key for the given BTC address";
        return false;
    }

    // (b) Balance, with a short static cache keyed by address.
    const int64_t now = GetTime();
    {
        std::lock_guard<std::mutex> lock(g_btcCacheMutex);
        const auto it = g_btcBalanceCache.find(btcAddress);
        if (it != g_btcBalanceCache.end() && it->second.first > now) {
            if (!it->second.second) {
                err = "BTC address has no balance (cached; re-checked every 10 minutes)";
                return false;
            }
            return true;
        }
    }

    std::string explorer = gArgs.GetArg("-vaultbtcexplorer", "https://blockstream.info/api");
    while (!explorer.empty() && explorer.back() == '/') explorer.pop_back();
    const std::string url = explorer + "/address/" + btcAddress; // base58 is URL-safe

    std::string body;
    if (!HttpGet(url, body, err)) {
        return false; // fail closed; err already describes the problem
    }
    UniValue val;
    if (!val.read(body) || !val.isObject()) {
        err = "BTC explorer returned invalid JSON";
        return false;
    }
    const UniValue& chain_stats = val.find_value("chain_stats");
    if (!chain_stats.isObject()) {
        err = "BTC explorer response missing chain_stats";
        return false;
    }
    const UniValue& funded_v = chain_stats.find_value("funded_txo_sum");
    const UniValue& spent_v = chain_stats.find_value("spent_txo_sum");
    if (!funded_v.isNum() || !spent_v.isNum()) {
        err = "BTC explorer response missing balance fields";
        return false;
    }
    const int64_t funded = funded_v.getInt<int64_t>();
    const int64_t spent = spent_v.getInt<int64_t>();
    if (funded < 0 || spent < 0 || spent > funded) {
        err = "BTC explorer returned inconsistent balance stats";
        return false;
    }
    const bool has_balance = (funded - spent) > 0;
    {
        std::lock_guard<std::mutex> lock(g_btcCacheMutex);
        g_btcBalanceCache[btcAddress] = {now + BTC_BALANCE_CACHE_SECONDS, has_balance};
    }
    if (!has_balance) {
        err = "BTC address has no confirmed balance";
        return false;
    }
    return true;
}

//! Directory holding this wallet's files (the database parent), e.g. for vaults.json.
static fs::path VaultRecordsPath(const CWallet& wallet)
{
    const fs::path dir = fs::absolute(fs::PathFromString(wallet.GetDatabase().Filename())).parent_path();
    return dir / fs::PathFromString(VAULT_RECORDS_FILE);
}

static UniValue ReadVaultRecords(const CWallet& wallet)
{
    UniValue records(UniValue::VARR);
    const fs::path path = VaultRecordsPath(wallet);
    std::ifstream file(fs::PathToString(path), std::ios::binary);
    if (!file.is_open()) return records; // no locks recorded yet
    std::string content((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    UniValue val;
    if (!val.read(content) || !val.isArray()) {
        throw JSONRPCError(RPC_MISC_ERROR, strprintf("vault records file is corrupt: %s", fs::PathToString(path)));
    }
    return val;
}

//! Atomic write of the records file (write tmp + rename).
static void WriteVaultRecords(const CWallet& wallet, const UniValue& records)
{
    const fs::path path = VaultRecordsPath(wallet);
    const fs::path tmp = fs::PathFromString(fs::PathToString(path) + ".tmp");
    {
        std::ofstream file(fs::PathToString(tmp), std::ios::binary | std::ios::trunc);
        if (!file.is_open()) {
            throw JSONRPCError(RPC_MISC_ERROR, strprintf("cannot write vault records file: %s", fs::PathToString(tmp)));
        }
        file << records.write(2) << "\n";
        file.flush();
        if (!file) {
            throw JSONRPCError(RPC_MISC_ERROR, "failed while writing vault records file");
        }
    }
    std::error_code ec;
    fs::rename(tmp, path, ec);
    if (ec) {
        throw JSONRPCError(RPC_MISC_ERROR, strprintf("failed to replace vault records file: %s", ec.message()));
    }
}

//! Chain median-time-past, preferred for maturity checks; falls back to node
//! time minus 2h (conservative: locks mature later, never earlier).
static int64_t VaultChainMTP(const CWallet& wallet)
{
    int64_t mtp = 0;
    if (wallet.chain().findBlock(wallet.GetLastBlockHash(), interfaces::FoundBlock().mtpTime(mtp)) && mtp > 0) {
        return mtp;
    }
    return GetTime() - 7200;
}

//! Vault tier from elapsed lock time: highest tier whose duration threshold
//! (30d, 6m, 1y, 5y) the elapsed time reaches; 0 below the shortest tier.
//! Identical to the consensus tier schedule used by CheckVaultYield.
static int VaultTierForElapsed(int64_t spendMTP, int64_t lockMTP)
{
    const int64_t elapsed = spendMTP - lockMTP;
    const auto& secs = Params().GetConsensus().nVaultTierSecs;
    for (int i = 3; i >= 0; --i) {
        if (elapsed >= secs[i]) return i + 1;
    }
    return 0;
}

//! Yield entitlement in sats: principal * (mult[tier-1] - 1e8) / 1e8, floored.
//! Integer-only (128-bit multiply on GCC/Clang, exact 64-bit decomposition on
//! MSVC, never float), identical to the consensus formula.
//! Tier 0 (below shortest lock): no yield.
static CAmount VaultEntitlement(CAmount principal, int64_t spendMTP, int64_t lockMTP)
{
    const int tier = VaultTierForElapsed(spendMTP, lockMTP);
    if (tier <= 0) return 0;
    const int64_t mult = Params().GetConsensus().nVaultTierMult[tier - 1];
    const int64_t multDelta = mult - VAULT_MULT_SCALE;
    assert(multDelta > 0);
#ifdef __SIZEOF_INT128__
    // Fast path (GCC/Clang): full 128-bit precision multiply.
    const __int128 entitlement = (__int128)principal * multDelta / VAULT_MULT_SCALE;
    assert(entitlement >= 0);
    return (CAmount)entitlement;
#else
    // MSVC has no __int128: decompose so every intermediate fits in 64 bits.
    // principal is MoneyRange-checked (0 <= P <= 2.1e18) and
    // multDelta = nVaultTierMult[tier-1] - 1e8 <= 101135718, so with D = 1e8:
    //   floor(P*(M-D)/D) = (P/D)*(M-D) + floor(((P%D)*(M-D))/D)
    // (P/D)*(M-D) <= 2.2e18 < 2^63 and ((P%D)*(M-D)) <= 1.1e16 < 2^63:
    // exact, no precision loss versus the 128-bit path. Same decomposition as
    // CheckVaultYield in validation.cpp.
    const CAmount nQuot = principal / VAULT_MULT_SCALE;
    const CAmount nRem = principal % VAULT_MULT_SCALE;
    const CAmount entitlement = (nQuot * multDelta) + ((nRem * multDelta) / VAULT_MULT_SCALE);
    assert(entitlement >= 0);
    return entitlement;
#endif
}

//! Parse a vault lock duration label ("30d", "6m", "1y", "5y") to seconds.
static int64_t ParseVaultDuration(const std::string& label)
{
    if (label == "30d") return 2592000;
    if (label == "6m") return 15768000;
    if (label == "1y") return 31536000;
    if (label == "5y") return 157680000;
    throw JSONRPCError(RPC_INVALID_PARAMETER,
        "duration must be one of \"30d\", \"6m\", \"1y\", \"5y\"");
}

//! Duration seconds back to its canonical label (for display/records).
static std::string VaultDurationLabel(int64_t secs)
{
    if (secs == 2592000) return "30d";
    if (secs == 15768000) return "6m";
    if (secs == 31536000) return "1y";
    if (secs == 157680000) return "5y";
    return std::to_string(secs) + "s";
}

//! A fresh compressed pubkey owned by the wallet, with its address.
//! Works for legacy and descriptor wallets.
static CPubKey GetNewVaultPubKey(CWallet& wallet, std::string& addressOut)
{
    if (!wallet.CanGetAddresses()) {
        throw JSONRPCError(RPC_WALLET_ERROR, "Error: This wallet has no available keys");
    }
    auto op_dest = wallet.GetNewDestination(OutputType::BECH32, /*label=*/"");
    if (!op_dest) {
        throw JSONRPCError(RPC_WALLET_KEYPOOL_RAN_OUT, util::ErrorString(op_dest).original);
    }
    const CTxDestination dest = *op_dest;
    const auto* wpkh = std::get_if<WitnessV0KeyHash>(&dest);
    if (!wpkh) {
        throw JSONRPCError(RPC_WALLET_ERROR, "vault: could not derive P2WPKH destination");
    }
    const CKeyID keyid = ToKeyID(*wpkh);
    CPubKey pubkey;
    bool found = false;
    if (LegacyScriptPubKeyMan* legacy = wallet.GetLegacyScriptPubKeyMan()) {
        found = legacy->GetPubKey(keyid, pubkey);
    } else {
        // Descriptor wallets: resolve the pubkey through the solving provider
        // for the destination's script.
        for (ScriptPubKeyMan* spk_man : wallet.GetAllScriptPubKeyMans()) {
            auto provider = spk_man->GetSolvingProvider(GetScriptForDestination(dest));
            if (provider && provider->GetPubKey(keyid, pubkey)) { found = true; break; }
        }
    }
    if (!found) {
        throw JSONRPCError(RPC_WALLET_ERROR, "vault: could not retrieve fresh vault pubkey from wallet");
    }
    if (!pubkey.IsFullyValid() || !pubkey.IsCompressed()) {
        throw JSONRPCError(RPC_WALLET_ERROR, "vault: could not retrieve fresh vault pubkey from wallet");
    }
    addressOut = EncodeDestination(dest);
    return pubkey;
}

//! A fresh P2WPKH destination owned by the wallet (for claim payouts).
static CTxDestination GetNewVaultDestination(CWallet& wallet, std::string& addressOut)
{
    if (!wallet.CanGetAddresses()) {
        throw JSONRPCError(RPC_WALLET_ERROR, "Error: This wallet has no available keys");
    }
    auto op_dest = wallet.GetNewDestination(OutputType::BECH32, /*label=*/"");
    if (!op_dest) {
        throw JSONRPCError(RPC_WALLET_KEYPOOL_RAN_OUT, util::ErrorString(op_dest).original);
    }
    addressOut = EncodeDestination(*op_dest);
    return *op_dest;
}

//! Private key for a wallet-owned pubkey. Works for legacy and descriptor wallets.
static CKey GetVaultPrivKey(CWallet& wallet, const CPubKey& pubkey)
{
    CKey key;
    if (LegacyScriptPubKeyMan* legacy = wallet.GetLegacyScriptPubKeyMan()) {
        legacy->GetKey(pubkey.GetID(), key);
    } else {
        for (ScriptPubKeyMan* spk_man : wallet.GetAllScriptPubKeyMans()) {
            auto* desc = dynamic_cast<DescriptorScriptPubKeyMan*>(spk_man);
            if (!desc) continue;
            // GetSigningProvider(pubkey) always includes private keys.
            auto provider = desc->GetSigningProvider(pubkey);
            if (provider && provider->GetKey(pubkey.GetID(), key)) break;
        }
    }
    if (!key.IsValid()) {
        throw JSONRPCError(RPC_WALLET_ERROR, "vault: private key not available (is the wallet unlocked?)");
    }
    return key;
}

} // namespace

RPCHelpMan vaultlock()
{
    return RPCHelpMan{"vaultlock",
        "\nLock SHIT into a native vault timelock.\n"
        "Creates a P2WSH output paying to \"<locktime> CHECKLOCKTIMEVERIFY DROP <pubkey> CHECKSIG\"\n"
        "where <pubkey> is a fresh key owned by this wallet and <locktime> is now + duration.\n"
        "Durations: \"30d\" (2% APY), \"6m\" (3% APY), \"1y\" (4% APY), \"5y\" (15% APY).\n"
        "Yield accrues per the consensus vault tiers and is minted when the lock is\n"
        "claimed with vaultclaim after maturity.\n"
        "\n"
        "Requires a Bitcoin co-hold proof: <btc_address> must be a P2PKH address with a\n"
        "non-zero BTC balance, and <btc_sig> a base64 compact signature (made in any\n"
        "Bitcoin wallet, e.g. Electrum) over the message \"shitcoin-vault:<vault owner address>\",\n"
        "where <vault owner address> is the fresh address this command generates for the lock.\n"
        "The Shitcoin chain cannot observe the Bitcoin chain; this check is enforced by\n"
        "the official wallet software, not by consensus." +
        HELP_REQUIRING_PASSPHRASE,
        {
            {"amount", RPCArg::Type::AMOUNT, RPCArg::Optional::NO, "The amount in SHIT to lock."},
            {"duration", RPCArg::Type::STR, RPCArg::Optional::NO, "Lock duration: \"30d\", \"6m\", \"1y\" or \"5y\"."},
            {"btc_address", RPCArg::Type::STR, RPCArg::Optional::NO, "Bitcoin P2PKH address proving the BTC co-hold."},
            {"btc_sig", RPCArg::Type::STR, RPCArg::Optional::NO, "Base64 compact Bitcoin signature over \"shitcoin-vault:<vault owner address>\"."},
        },
        RPCResult{
            RPCResult::Type::OBJ, "", "",
            {
                {RPCResult::Type::STR_HEX, "txid", "The lock transaction id."},
                {RPCResult::Type::NUM, "vout", "The vault output index."},
                {RPCResult::Type::NUM, "locktime", "Unix timelock committed in the vault script."},
                {RPCResult::Type::STR, "unlock_time_iso", "ISO-8601 UTC time when the lock matures."},
            },
        },
        RPCExamples{
            HelpExampleCli("vaultlock", "100 \"1y\" \"1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa\" \"H...\"") +
            HelpExampleRpc("vaultlock", "100, \"1y\", \"1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa\", \"H...\"")
        },
        [&](const RPCHelpMan& self, const node::JSONRPCRequest& request) -> UniValue
        {
            std::shared_ptr<CWallet> const pwallet = GetWalletForJSONRPCRequest(request);
            if (!pwallet) return UniValue::VNULL;

            pwallet->BlockUntilSyncedToCurrentChain();

            LOCK(pwallet->cs_wallet);

            EnsureWalletIsUnlocked(*pwallet);

            const CAmount amount = AmountFromValue(request.params[0]);
            if (!MoneyRange(amount) || amount <= 0) {
                throw JSONRPCError(RPC_INVALID_PARAMETER, "Invalid amount");
            }
            const int64_t durationSecs = ParseVaultDuration(request.params[1].get_str());
            const std::string btcAddress = request.params[2].get_str();
            const std::string btcSig = request.params[3].get_str();

            // Fresh vault owner key; its address anchors the BTC challenge.
            std::string ownerAddress;
            const CPubKey vaultPubKey = GetNewVaultPubKey(*pwallet, ownerAddress);

            // BTC co-hold check, challenged on the new vault owner address.
            std::string btcErr;
            if (!CheckBTCCoHold(btcAddress, btcSig, VAULT_CHALLENGE_PREFIX + ownerAddress, btcErr)) {
                throw JSONRPCError(RPC_INVALID_PARAMETER, strprintf("BTC co-hold check failed: %s", btcErr));
            }

            // Build the vault output: P2WSH of <locktime> CLTV DROP <pubkey> CHECKSIG.
            const int64_t now = GetTime();
            if (now + durationSecs > (int64_t)std::numeric_limits<uint32_t>::max()) {
                throw JSONRPCError(RPC_INTERNAL_ERROR, "computed locktime out of range");
            }
            const uint32_t nLockTime = (uint32_t)(now + durationSecs);
            if (nLockTime < VAULT_MIN_LOCKTIME) {
                throw JSONRPCError(RPC_INTERNAL_ERROR, "computed locktime below VAULT_MIN_LOCKTIME");
            }
            const CScript witnessScript = BuildVaultLockScript(nLockTime, vaultPubKey);
            const CScript spk = GetVaultLockP2WSH(witnessScript);
            uint256 witHash;
            CSHA256().Write(witnessScript.data(), witnessScript.size()).Finalize(witHash.begin());

            // Fund and broadcast through the normal wallet flow.
            CCoinControl coin_control;
            constexpr int RANDOM_CHANGE_POSITION = -1;
            const std::vector<CRecipient> recipients{{WitnessV0ScriptHash(witHash), amount, /*fSubtractFeeFromAmount=*/false}};
            auto res = CreateTransaction(*pwallet, recipients, RANDOM_CHANGE_POSITION, coin_control, /*sign=*/true);
            if (!res) {
                throw JSONRPCError(RPC_WALLET_INSUFFICIENT_FUNDS, util::ErrorString(res).original);
            }
            const CTransactionRef& tx = res->tx;
            uint32_t vout = 0;
            bool found = false;
            for (uint32_t i = 0; i < tx->vout.size(); ++i) {
                if (tx->vout[i].scriptPubKey == spk) {
                    vout = i;
                    found = true;
                    break;
                }
            }
            if (!found) {
                throw JSONRPCError(RPC_WALLET_ERROR, "vault: created transaction does not contain the vault output");
            }
            pwallet->CommitTransaction(tx, {}, /*orderForm=*/{});

            // Record the lock (atomic tmp+rename).
            UniValue records = ReadVaultRecords(*pwallet);
            UniValue rec(UniValue::VOBJ);
            rec.pushKV("txid", tx->GetHash().GetHex());
            rec.pushKV("vout", (int64_t)vout);
            rec.pushKV("locktime", (int64_t)nLockTime);
            rec.pushKV("pubkey_hex", HexStr(Span<const uint8_t>(vaultPubKey.data(), vaultPubKey.size())));
            rec.pushKV("witness_hex", HexStr(Span<const uint8_t>(witnessScript.data(), witnessScript.size())));
            rec.pushKV("amount_sats", amount);
            rec.pushKV("duration_secs", durationSecs);
            rec.pushKV("btc_address", btcAddress);
            rec.pushKV("claimed", false);
            records.push_back(rec);
            WriteVaultRecords(*pwallet, records);

            UniValue result(UniValue::VOBJ);
            result.pushKV("txid", tx->GetHash().GetHex());
            result.pushKV("vout", (int64_t)vout);
            result.pushKV("locktime", (int64_t)nLockTime);
            result.pushKV("unlock_time_iso", FormatISO8601DateTime(nLockTime));
            return result;
        },
    };
}

RPCHelpMan vaultclaim()
{
    return RPCHelpMan{"vaultclaim",
        "\nClaim all matured native vault locks recorded in <walletdir>/vaults.json.\n"
        "A lock is matured when its script locktime is at or before the chain's\n"
        "median-time-past. For each matured, unclaimed lock this builds a claim\n"
        "transaction spending the vault output:\n"
        "  vin: the vault outpoint, nSequence=0 (CLTV fails if nSequence is final),\n"
        "       nLockTime set to the script locktime,\n"
        "  witness: <signature by the vault pubkey> <vault witness script>,\n"
        "  vout: a single P2WPKH output to a fresh wallet address paying\n"
        "        principal + tier entitlement (minted by consensus).\n"
        "The BTC co-hold is re-proven per claim: <btc_sig> must be a fresh base64\n"
        "compact Bitcoin signature over \"shitcoin-vault:<destination address>\";\n"
        "<btc_address> may be omitted to reuse the address from the lock record.\n"
        "The Shitcoin chain cannot observe the Bitcoin chain; this check is enforced by\n"
        "the official wallet software, not by consensus." +
        HELP_REQUIRING_PASSPHRASE,
        {
            {"btc_address", RPCArg::Type::STR, RPCArg::Optional::OMITTED, "Bitcoin P2PKH address proving the BTC co-hold (default: the address from the lock record)."},
            {"btc_sig", RPCArg::Type::STR, RPCArg::Optional::OMITTED, "Fresh base64 compact Bitcoin signature over \"shitcoin-vault:<destination address>\". Required."},
        },
        RPCResult{
            RPCResult::Type::ARR, "", "Claim transaction ids, one per claimed lock.",
            {
                {RPCResult::Type::STR_HEX, "txid", "A claim transaction id."},
            },
        },
        RPCExamples{
            HelpExampleCli("vaultclaim", "\"1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa\" \"H...\"") +
            HelpExampleRpc("vaultclaim", "\"1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa\", \"H...\"")
        },
        [&](const RPCHelpMan& self, const node::JSONRPCRequest& request) -> UniValue
        {
            std::shared_ptr<CWallet> const pwallet = GetWalletForJSONRPCRequest(request);
            if (!pwallet) return UniValue::VNULL;

            pwallet->BlockUntilSyncedToCurrentChain();

            LOCK(pwallet->cs_wallet);

            EnsureWalletIsUnlocked(*pwallet);

            if (request.params[1].isNull()) {
                throw JSONRPCError(RPC_INVALID_PARAMETER, "vaultclaim: btc_sig is required (a fresh Bitcoin signature over \"shitcoin-vault:<destination address>\")");
            }
            const bool haveBtcAddr = !request.params[0].isNull();
            const std::string btcAddressParam = haveBtcAddr ? request.params[0].get_str() : "";
            const std::string btcSig = request.params[1].get_str();

            const int64_t spendMTP = VaultChainMTP(*pwallet);
            UniValue records = ReadVaultRecords(*pwallet);
            UniValue claimedTxids(UniValue::VARR);

            for (size_t i = 0; i < records.size(); ++i) {
                const UniValue& rec = records[i];
                for (const char* key : {"txid", "vout", "locktime", "amount_sats", "duration_secs", "witness_hex", "pubkey_hex", "btc_address", "claimed"}) {
                    if (!rec.exists(key)) {
                        throw JSONRPCError(RPC_MISC_ERROR, strprintf("vault: lock record is missing '%s'", key));
                    }
                }
                if (rec.find_value("claimed").get_bool()) continue;

                const uint256 txid = uint256S(rec.find_value("txid").get_str());
                const uint32_t vout = (uint32_t)rec.find_value("vout").getInt<int64_t>();
                const uint32_t locktime = (uint32_t)rec.find_value("locktime").getInt<int64_t>();
                const CAmount principal = rec.find_value("amount_sats").getInt<CAmount>();
                const int64_t durationSecs = rec.find_value("duration_secs").getInt<int64_t>();
                const std::string btcAddress = haveBtcAddr ? btcAddressParam : rec.find_value("btc_address").get_str();
                const std::vector<unsigned char> witnessBytes = ParseHex(rec.find_value("witness_hex").get_str());
                const std::vector<unsigned char> pubkeyBytes = ParseHex(rec.find_value("pubkey_hex").get_str());

                if (locktime > spendMTP) continue; // not matured yet
                if (!MoneyRange(principal) || principal <= 0 || durationSecs <= 0) {
                    throw JSONRPCError(RPC_MISC_ERROR, strprintf("vault: corrupt lock record for %s", txid.GetHex()));
                }

                // Re-derive and strict-match the witness script from the record.
                const CScript witnessScript(witnessBytes.begin(), witnessBytes.end());
                uint32_t scriptLocktime = 0;
                CPubKey vaultPubKey(pubkeyBytes.begin(), pubkeyBytes.end());
                CPubKey scriptPubKey;
                if (!IsVaultLockScript(witnessScript, scriptLocktime, scriptPubKey) ||
                    scriptLocktime != locktime || scriptPubKey != vaultPubKey) {
                    throw JSONRPCError(RPC_MISC_ERROR, strprintf("vault: lock record failed script validation for %s", txid.GetHex()));
                }

                // Fresh payout destination; its address anchors this claim's BTC challenge.
                std::string destAddress;
                const CTxDestination payoutDest = GetNewVaultDestination(*pwallet, destAddress);

                std::string btcErr;
                if (!CheckBTCCoHold(btcAddress, btcSig, VAULT_CHALLENGE_PREFIX + destAddress, btcErr)) {
                    throw JSONRPCError(RPC_INVALID_PARAMETER, strprintf("BTC co-hold check failed for lock %s: %s", txid.GetHex(), btcErr));
                }

                // Tier + entitlement, identical to the consensus formula.
                const int64_t lockMTP = (int64_t)locktime - durationSecs;
                const CAmount entitlement = VaultEntitlement(principal, spendMTP, lockMTP);
                const CAmount payout = principal + entitlement;
                if (!MoneyRange(payout)) {
                    throw JSONRPCError(RPC_WALLET_ERROR, strprintf("vault: claim payout out of range for lock %s", txid.GetHex()));
                }

                // Build the claim: nLockTime = script locktime, nSequence = 0
                // (REQUIRED: CLTV fails if nSequence is final), single P2WPKH output.
                CMutableTransaction mtx;
                mtx.nVersion = 2;
                mtx.nLockTime = locktime;
                mtx.vin.emplace_back(COutPoint(txid, vout), CScript(), /*nSequenceIn=*/0);
                mtx.vout.emplace_back(payout, GetScriptForDestination(payoutDest));

                // Sign the P2WSH input: witness = <sig> <witnessScript>.
                const CKey key = GetVaultPrivKey(*pwallet, vaultPubKey);
                const uint256 sighash = SignatureHash(witnessScript, CTransaction(mtx), /*nIn=*/0, SIGHASH_ALL, principal, SigVersion::WITNESS_V0);
                std::vector<unsigned char> sig;
                if (!key.Sign(sighash, sig)) {
                    throw JSONRPCError(RPC_WALLET_ERROR, strprintf("vault: signing failed for lock %s", txid.GetHex()));
                }
                sig.push_back((unsigned char)SIGHASH_ALL);
                mtx.vin[0].scriptWitness.stack.push_back(sig);
                mtx.vin[0].scriptWitness.stack.emplace_back(witnessScript.begin(), witnessScript.end());
                const CTransactionRef tx = MakeTransactionRef(std::move(mtx));

                std::string broadcastErr;
                if (!pwallet->chain().broadcastTransaction(tx, DEFAULT_TRANSACTION_MAXFEE, /*relay=*/true, broadcastErr)) {
                    throw JSONRPCError(RPC_WALLET_ERROR, strprintf("vault: claim broadcast failed for lock %s: %s", txid.GetHex(), broadcastErr));
                }
                pwallet->CommitTransaction(tx, {}, /*orderForm=*/{});

                // Mark claimed. UniValue arrays are const-accessed, so rebuild the
                // array with the updated record and persist it atomically.
                UniValue fresh(UniValue::VARR);
                for (size_t j = 0; j < records.size(); ++j) {
                    if (j == i) {
                        UniValue updated = records[j];
                        updated.pushKV("claimed", true);
                        fresh.push_back(updated);
                    } else {
                        fresh.push_back(records[j]);
                    }
                }
                WriteVaultRecords(*pwallet, fresh);
                records = std::move(fresh);
                claimedTxids.push_back(tx->GetHash().GetHex());
            }
            return claimedTxids;
        },
    };
}

RPCHelpMan vaultinfo()
{
    return RPCHelpMan{"vaultinfo",
        "\nList the native vault locks recorded in <walletdir>/vaults.json, with their\n"
        "maturity status and accrued yield entitlement.\n"
        "Status is \"locked\", \"matured\" (locktime at or before chain median-time-past),\n"
        "or \"claimed\". The tier and accrued entitlement use the consensus formula\n"
        "with the current chain median-time-past as the spend time.",
        {},
        RPCResult{
            RPCResult::Type::ARR, "", "",
            {
                {RPCResult::Type::OBJ, "", "",
                {
                    {RPCResult::Type::STR_HEX, "txid", "The lock transaction id."},
                    {RPCResult::Type::NUM, "vout", "The vault output index."},
                    {RPCResult::Type::STR_AMOUNT, "amount", "Locked principal, in SHIT."},
                    {RPCResult::Type::STR, "duration", "Lock duration label (30d/6m/1y/5y)."},
                    {RPCResult::Type::NUM, "locktime", "Unix timelock of the vault script."},
                    {RPCResult::Type::STR, "unlock_time", "ISO-8601 UTC time when the lock matures."},
                    {RPCResult::Type::STR, "status", "One of \"locked\", \"matured\", \"claimed\"."},
                    {RPCResult::Type::NUM, "tier", "Vault tier (1-4) for the elapsed lock time; 0 below 30d."},
                    {RPCResult::Type::NUM, "accrued_entitlement_sats", "Yield entitlement accrued so far, in sats."},
                }},
            },
        },
        RPCExamples{
            HelpExampleCli("vaultinfo", "") +
            HelpExampleRpc("vaultinfo", "")
        },
        [&](const RPCHelpMan& self, const node::JSONRPCRequest& request) -> UniValue
        {
            std::shared_ptr<CWallet> const pwallet = GetWalletForJSONRPCRequest(request);
            if (!pwallet) return UniValue::VNULL;

            pwallet->BlockUntilSyncedToCurrentChain();

            LOCK(pwallet->cs_wallet);

            const int64_t nowMTP = VaultChainMTP(*pwallet);
            const UniValue records = ReadVaultRecords(*pwallet);
            UniValue result(UniValue::VARR);
            for (size_t i = 0; i < records.size(); ++i) {
                const UniValue& rec = records[i];
                for (const char* key : {"txid", "vout", "locktime", "amount_sats", "duration_secs", "claimed"}) {
                    if (!rec.exists(key)) {
                        throw JSONRPCError(RPC_MISC_ERROR, strprintf("vault: lock record is missing '%s'", key));
                    }
                }
                const std::string txid = rec.find_value("txid").get_str();
                const int64_t vout = rec.find_value("vout").getInt<int64_t>();
                const CAmount principal = rec.find_value("amount_sats").getInt<CAmount>();
                const int64_t durationSecs = rec.find_value("duration_secs").getInt<int64_t>();
                const int64_t locktime = rec.find_value("locktime").getInt<int64_t>();
                const bool claimed = rec.exists("claimed") && rec.find_value("claimed").get_bool();

                const int64_t lockMTP = locktime - durationSecs;
                const int tier = VaultTierForElapsed(nowMTP, lockMTP);
                const CAmount accrued = VaultEntitlement(principal, nowMTP, lockMTP);

                UniValue entry(UniValue::VOBJ);
                entry.pushKV("txid", txid);
                entry.pushKV("vout", vout);
                entry.pushKV("amount", ValueFromAmount(principal));
                entry.pushKV("duration", VaultDurationLabel(durationSecs));
                entry.pushKV("locktime", locktime);
                entry.pushKV("unlock_time", FormatISO8601DateTime(locktime));
                entry.pushKV("status", claimed ? "claimed" : (locktime <= nowMTP ? "matured" : "locked"));
                entry.pushKV("tier", tier);
                entry.pushKV("accrued_entitlement_sats", accrued);
                result.push_back(entry);
            }
            return result;
        },
    };
}

} // namespace wallet
