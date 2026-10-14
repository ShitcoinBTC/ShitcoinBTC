#!/usr/bin/env bash

# ---------------------------------------
# WARNING: REGTEST / LOCAL TESTING ONLY
# ---------------------------------------
# This script mines by looping `generatetoaddress`, which creates
# blocks on demand. That only works on regtest, where blocks can be
# generated locally for testing.
#
# It CANNOT mine production SHIT. Shitcoin mainnet is merge-mined with
# Bitcoin: consensus rejects any mainnet block without a valid AuxPoW
# proof anchored in a real Bitcoin mainnet block ("no-auxpow").
# Standalone mining is not a thing on mainnet, no matter how long this
# loop runs.
#
# For real mining, see doc/bitcoin-mainnet-mining.md: point SHA-256
# hardware at a Shitcoin-aware pool, or (pool operators) run a real
# Bitcoin Core mainnet node as the parent chain.

# ---------------------------------------
# Configuration
# ---------------------------------------
WALLET_NAME="myminewallet"

# ---------------------------------------
# 0. Refuse to run outside regtest
# ---------------------------------------
CHAIN=$(syscoin-cli getblockchaininfo 2>/dev/null | sed -n 's/.*"chain"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
if [ "$CHAIN" != "regtest" ]; then
  echo "ERROR: mine.sh is a regtest-only testing script, but the node reports chain='${CHAIN:-unknown}'." >&2
  echo "Production SHIT is merge-mined with real Bitcoin mainnet — see doc/bitcoin-mainnet-mining.md." >&2
  exit 1
fi

# ---------------------------------------
# 1. Ensure a wallet is available
# ---------------------------------------
echo "Checking if wallet \"$WALLET_NAME\" exists..."

# If the wallet is not listed, create it
if ! syscoin-cli listwallets 2>/dev/null | grep -q "$WALLET_NAME"; then
  echo "Wallet \"$WALLET_NAME\" not found. Creating wallet..."
  # createwallet parameters can differ by Syscoin version; this is the basic usage
  syscoin-cli createwallet "$WALLET_NAME"
fi

# Attempt to load the wallet (in case it's newly created or was unloaded)
# Some versions of Syscoin automatically load a newly created wallet; if so, this command is harmless.
syscoin-cli loadwallet "$WALLET_NAME" 2>/dev/null

# ---------------------------------------
# 2. Get or generate a new address from that wallet
# ---------------------------------------
echo "Retrieving a new address from wallet \"$WALLET_NAME\"..."
MINING_ADDRESS=$(syscoin-cli -rpcwallet="$WALLET_NAME" getnewaddress)
echo "Mining to address: $MINING_ADDRESS"

# ---------------------------------------
# 3. Wait until Masternode sync is complete
# ---------------------------------------
echo "Starting Masternode Sync Checker..."

while true; do
  SYNC_STATUS=$(syscoin-cli mnsync status 2>/dev/null)

  # Check if "IsSynced": true
  if echo "$SYNC_STATUS" | grep -q '"IsSynced": true'; then
    echo "Masternode is fully synced!"
    break
  else
    echo "Masternode not synced yet. Running 'mnsync next'..."
    syscoin-cli mnsync next
  fi

  # Wait a bit before checking again
  sleep 10
done

# ---------------------------------------
# 4. Continuously mine blocks
# ---------------------------------------
echo "Starting continuous mining..."

while true; do
  echo "Generating a new block to address: $MINING_ADDRESS"
  # If 'generate' is unsupported, switch to 'generatetoaddress' as shown here:
  syscoin-cli -rpcwallet="$WALLET_NAME" generatetoaddress 1 "$MINING_ADDRESS" 1000000000

  # Sleep briefly to avoid spamming
  sleep 5
done
