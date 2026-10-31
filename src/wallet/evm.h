// Copyright (c) 2026 The Shitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#ifndef SYSCOIN_WALLET_EVM_H
#define SYSCOIN_WALLET_EVM_H

#include <key.h>

#include <cstdint>
#include <string>

class CWallet;

namespace wallet {

//! Parameters for an EIP-155 legacy NEVM transaction, as collected by the GUI
//! from the NEVM JSON-RPC endpoint. Big integers are decimal strings.
//!
//! "SHIT-20" is this chain's name for the ERC-20-compatible token interface
//! (balanceOf/decimals/symbol/transfer) on the Shitcoin NEVM chain.
struct EVMTxParams {
    std::string to;         //!< 0x-prefixed 20-byte hex destination (EOA or token contract)
    std::string valueWei;   //!< decimal wei (0 for SHIT-20 transfers)
    std::string dataHex;    //!< 0x-prefixed calldata (SHIT-20 transfer payload, or empty)
    std::string nonce;      //!< decimal transaction count
    std::string gasPriceWei;//!< decimal wei per gas
    std::string gasLimit;   //!< decimal gas limit
    uint64_t chainId{0};    //!< EIP-155 chain id (20026 on Shitcoin NEVM mainnet)
};

//! Derive the NEVM (Ethereum-style) address for a private key:
//! last 20 bytes of keccak256(uncompressed secp256k1 pubkey).
//! The same private key controls both the UTXO address and this EVM address.
std::string EVMAddressHex(const CKey& key);

//! Fetch the wallet's EVM signing key: preferably the key of the first
//! spendable receive address in the address book (so the EVM identity matches
//! an address the user recognizes), falling back to the first available
//! private key across the wallet's script pub key managers. Returns false
//! when the wallet is locked/encrypted or holds no private keys.
//! Call with the wallet's cs_wallet lock held.
bool GetWalletEVMKey(const CWallet& wallet, CKey& keyOut) EXCLUSIVE_LOCKS_REQUIRED(wallet.cs_wallet);

//! Build and EIP-155 sign a legacy NEVM transaction. Returns the signed raw
//! transaction as 0x-prefixed hex, or an empty string on failure.
std::string SignEVMTransaction(const CKey& key, const EVMTxParams& params);

} // namespace wallet

#endif // SYSCOIN_WALLET_EVM_H
