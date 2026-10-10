// Copyright (c) 2026 The Shitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#ifndef SHITCOIN_SCRIPT_VAULT_H
#define SHITCOIN_SCRIPT_VAULT_H

#include <crypto/sha256.h>
#include <pubkey.h>
#include <script/script.h>
#include <uint256.h>

#include <cassert>
#include <cstdint>
#include <vector>

//! Shitcoin native vault lock script template.
//
//! The vault output is a P2WSH of this witness script:
//!   <4-byte LE unix locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP <33-byte compressed pubkey> OP_CHECKSIG
//!
//! Strict timelock: there is no early-exit path. The principal can only be
//! spent by the pubkey owner once the chain's median-time-past reaches the
//! locktime. Vault yield is minted by consensus (CheckVaultYield), never by
//! script, and is drawn from the protocol-level nVaultReserveTotal — no
//! address, no custodian.
static constexpr size_t VAULT_WITNESS_SCRIPT_SIZE = 42;
//! Only timestamp-type locktimes are accepted in vault scripts.
static constexpr uint32_t VAULT_MIN_LOCKTIME = 500000000;

//! Build the vault witness script for pubkey, timelocked until nLockTime (unix).
inline CScript BuildVaultLockScript(uint32_t nLockTime, const CPubKey& pubkey)
{
    assert(nLockTime >= VAULT_MIN_LOCKTIME);
    assert(pubkey.IsFullyValid());
    std::vector<unsigned char> vchLockTime = {
        static_cast<unsigned char>(nLockTime & 0xff),
        static_cast<unsigned char>((nLockTime >> 8) & 0xff),
        static_cast<unsigned char>((nLockTime >> 16) & 0xff),
        static_cast<unsigned char>((nLockTime >> 24) & 0xff),
    };
    CScript script;
    script << vchLockTime << OP_CHECKLOCKTIMEVERIFY << OP_DROP;
    script << std::vector<unsigned char>(pubkey.begin(), pubkey.end());
    script << OP_CHECKSIG;
    return script;
}

//! Strict template match for a vault witness script. On success, fills in the
//! locktime and owner pubkey and returns true.
inline bool IsVaultLockScript(const CScript& witnessScript, uint32_t& nLockTimeOut, CPubKey& pubkeyOut)
{
    if (witnessScript.size() != VAULT_WITNESS_SCRIPT_SIZE) return false;
    // operator[] on CScript returns unsigned char.
    if (witnessScript[0] != 0x04) return false; // 4-byte data push
    const uint32_t nLockTime =
        static_cast<uint32_t>(witnessScript[1]) |
        (static_cast<uint32_t>(witnessScript[2]) << 8) |
        (static_cast<uint32_t>(witnessScript[3]) << 16) |
        (static_cast<uint32_t>(witnessScript[4]) << 24);
    if (nLockTime < VAULT_MIN_LOCKTIME) return false;
    if (witnessScript[5] != static_cast<unsigned char>(OP_CHECKLOCKTIMEVERIFY)) return false;
    if (witnessScript[6] != static_cast<unsigned char>(OP_DROP)) return false;
    if (witnessScript[7] != 0x21) return false; // 33-byte data push
    CPubKey pubkey(witnessScript.begin() + 8, witnessScript.begin() + 41);
    if (!pubkey.IsFullyValid()) return false;
    if (witnessScript[41] != static_cast<unsigned char>(OP_CHECKSIG)) return false;
    nLockTimeOut = nLockTime;
    pubkeyOut = pubkey;
    return true;
}

//! P2WSH scriptPubKey committing to a vault witness script.
inline CScript GetVaultLockP2WSH(const CScript& witnessScript)
{
    uint256 h;
    CSHA256().Write(witnessScript.data(), witnessScript.size()).Finalize(h.begin());
    CScript spk;
    spk << OP_0 << std::vector<unsigned char>(h.begin(), h.end());
    return spk;
}

#endif // SHITCOIN_SCRIPT_VAULT_H
