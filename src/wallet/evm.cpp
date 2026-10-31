// Copyright (c) 2026 The Shitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <wallet/evm.h>

#include <key.h>
#include <secp256k1.h>
#include <secp256k1_recovery.h>
#include <sync.h>
#include <util/strencodings.h>
#include <wallet/scriptpubkeyman.h>
#include <wallet/wallet.h>
#include <nevm/address.h>
#include <nevm/common.h>
#include <nevm/rlp.h>
#include <nevm/sha3.h>

namespace wallet {
namespace {

secp256k1_context* EvmSignContext()
{
    static secp256k1_context* ctx = secp256k1_context_create(SECP256K1_CONTEXT_SIGN);
    return ctx;
}

//! Parse a decimal string into a u256. Returns false on bad input.
bool ParseDecU256(const std::string& str, dev::u256& out)
{
    out = 0;
    if (str.empty()) return false;
    for (char c : str) {
        if (c < '0' || c > '9') return false;
        out *= 10;
        out += static_cast<unsigned>(c - '0');
    }
    return true;
}

//! Parse a 0x-prefixed (or bare) 40-hex-char string into an EVM address.
bool ParseEVMAddress(const std::string& str, dev::Address& out)
{
    std::string hex = str;
    if (hex.size() >= 2 && hex[0] == '0' && (hex[1] == 'x' || hex[1] == 'X')) {
        hex = hex.substr(2);
    }
    if (hex.size() != 40 || !IsHex(hex)) return false;
    std::vector<unsigned char> bytes = ParseHex(hex);
    if (bytes.size() != 20) return false;
    dev::bytesRef ref = out.ref();
    memcpy(ref.data(), bytes.data(), 20);
    return true;
}

//! Parse 0x-prefixed (or bare) hex into bytes. Empty string -> empty bytes.
bool ParseHexBytes(const std::string& str, dev::bytes& out)
{
    std::string hex = str;
    if (hex.size() >= 2 && hex[0] == '0' && (hex[1] == 'x' || hex[1] == 'X')) {
        hex = hex.substr(2);
    }
    if (!hex.empty() && !IsHex(hex)) return false;
    std::vector<unsigned char> raw = ParseHex(hex);
    out.assign(raw.begin(), raw.end());
    return true;
}

//! Build a big-endian u256 from 32 raw bytes.
dev::u256 U256FromBytes(const unsigned char* data)
{
    dev::u256 out = 0;
    for (int i = 0; i < 32; ++i) {
        out <<= 8;
        out += data[i];
    }
    return out;
}

} // namespace

std::string EVMAddressHex(const CKey& key)
{
    if (!key.IsValid()) return "";
    secp256k1_pubkey pubkey;
    if (!secp256k1_ec_pubkey_create(EvmSignContext(), &pubkey, key.begin())) {
        return "";
    }
    unsigned char uncompressed[65];
    size_t len = sizeof(uncompressed);
    if (!secp256k1_ec_pubkey_serialize(EvmSignContext(), uncompressed, &len, &pubkey, SECP256K1_EC_UNCOMPRESSED) ||
        len != sizeof(uncompressed)) {
        return "";
    }
    // keccak256 of the 64-byte uncompressed pubkey (no 0x04 prefix); address
    // is the last 20 bytes.
    dev::h256 hash = dev::sha3(dev::bytesConstRef(uncompressed + 1, 64));
    dev::Address addr;
    dev::bytesRef ref = addr.ref();
    dev::bytesConstRef href = hash.ref();
    memcpy(ref.data(), href.data() + 12, 20);
    return "0x" + addr.hex();
}

namespace {
//! Try to fetch the private key for a destination from one script pub key manager.
bool GetKeyForEVM(ScriptPubKeyMan* spk_man, const CTxDestination& dest, CKey& keyOut)
{
    const CScript script = GetScriptForDestination(dest);
    if (auto* desc_man = dynamic_cast<DescriptorScriptPubKeyMan*>(spk_man)) {
        std::unique_ptr<FlatSigningProvider> provider = desc_man->GetSigningProvider(script, /*include_private=*/true);
        if (!provider) return false;
        const CKeyID keyid = GetKeyForDestination(*provider, dest);
        return !keyid.IsNull() && provider->GetKey(keyid, keyOut) && keyOut.IsValid();
    }
    if (auto* legacy_man = dynamic_cast<LegacyScriptPubKeyMan*>(spk_man)) {
        const CKeyID keyid = GetKeyForDestination(*legacy_man, dest);
        return !keyid.IsNull() && legacy_man->GetKey(keyid, keyOut) && keyOut.IsValid();
    }
    return false;
}
} // namespace

bool GetWalletEVMKey(const CWallet& wallet, CKey& keyOut) EXCLUSIVE_LOCKS_REQUIRED(wallet.cs_wallet)
{
    AssertLockHeld(wallet.cs_wallet);
    // Prefer the key of the first spendable receive address in the address
    // book, so the EVM identity corresponds to an address the user recognizes.
    for (const auto& [dest, entry] : wallet.m_address_book) {
        if (entry.purpose != AddressPurpose::RECEIVE) continue;
        if (!(wallet.IsMine(dest) & ISMINE_SPENDABLE)) continue;
        for (ScriptPubKeyMan* spk_man : wallet.GetAllScriptPubKeyMans()) {
            if (GetKeyForEVM(spk_man, dest, keyOut)) return true;
        }
    }
    // Fall back to the first available private key in the wallet (covers
    // fresh wallets whose address book is still empty).
    for (ScriptPubKeyMan* spk_man : wallet.GetAllScriptPubKeyMans()) {
        if (auto* desc_man = dynamic_cast<DescriptorScriptPubKeyMan*>(spk_man)) {
            LOCK(desc_man->cs_desc_man);
            for (const auto& [keyid, key] : desc_man->GetKeys()) {
                if (key.IsValid()) {
                    keyOut = key;
                    return true;
                }
            }
        } else if (auto* legacy_man = dynamic_cast<LegacyScriptPubKeyMan*>(spk_man)) {
            for (const CKeyID& keyid : legacy_man->GetKeys()) {
                CKey key;
                if (legacy_man->GetKey(keyid, key) && key.IsValid()) {
                    keyOut = key;
                    return true;
                }
            }
        }
    }
    return false;
}

std::string SignEVMTransaction(const CKey& key, const EVMTxParams& params)
{
    if (!key.IsValid()) return "";

    dev::u256 nonce, gasPrice, gasLimit, value;
    if (!ParseDecU256(params.nonce, nonce)) return "";
    if (!ParseDecU256(params.gasPriceWei, gasPrice)) return "";
    if (!ParseDecU256(params.gasLimit, gasLimit)) return "";
    if (!ParseDecU256(params.valueWei, value)) return "";
    if (params.chainId == 0) return "";

    dev::Address to;
    if (!ParseEVMAddress(params.to, to)) return "";
    dev::bytes data;
    if (!ParseHexBytes(params.dataHex, data)) return "";

    // EIP-155 signing payload: [nonce, gasPrice, gasLimit, to, value, data, chainId, 0, 0]
    dev::RLPStream signing(9);
    signing.append(nonce);
    signing.append(gasPrice);
    signing.append(gasLimit);
    signing.append(to);
    signing.append(value);
    signing.append(data);
    signing.append(dev::u256(params.chainId));
    signing.append(dev::u256(0));
    signing.append(dev::u256(0));
    dev::h256 sighash = dev::sha3(signing.out());

    secp256k1_context* ctx = EvmSignContext();
    secp256k1_ecdsa_recoverable_signature recsig;
    if (!secp256k1_ecdsa_sign_recoverable(ctx, &recsig, sighash.ref().data(), key.begin(), nullptr, nullptr)) {
        return "";
    }
    unsigned char compact[64];
    int recid = 0;
    secp256k1_ecdsa_recoverable_signature_serialize_compact(ctx, compact, &recid, &recsig);

    // Enforce low-S (EIP-2 style) and fix the recovery id accordingly.
    secp256k1_ecdsa_signature sig;
    if (!secp256k1_ecdsa_signature_parse_compact(ctx, &sig, compact)) return "";
    if (secp256k1_ecdsa_signature_normalize(ctx, &sig, &sig)) {
        recid ^= 1;
    }
    if (!secp256k1_ecdsa_signature_serialize_compact(ctx, compact, &sig)) return "";

    dev::u256 r = U256FromBytes(compact);
    dev::u256 s = U256FromBytes(compact + 32);
    dev::u256 v = dev::u256(params.chainId) * 2 + 35 + recid;

    dev::RLPStream tx(9);
    tx.append(nonce);
    tx.append(gasPrice);
    tx.append(gasLimit);
    tx.append(to);
    tx.append(value);
    tx.append(data);
    tx.append(v);
    tx.append(r);
    tx.append(s);
    return "0x" + HexStr(tx.out());
}

} // namespace wallet
