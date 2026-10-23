# Shitcoin Smart Contracts (NEVM)

Solidity contracts for the Shitcoin NEVM (EVM-equivalent, chain ID 20026).
Deploy with any Ethereum tooling — Hardhat, Foundry, Remix — pointed at a
Shitcoin NEVM RPC endpoint. Gas is paid in SHIT.

## Contracts

### ShitLottery.sol
On-chain lottery. Players buy tickets with native SHIT; when the round closes,
anyone can trigger the draw. Winner takes **90%** of the pot, **10% is burned**
to `0x000000000000000000000000000000000000dEaD`.

- Constructor: `ShitLottery(ticketPriceWei, roundLengthBlocks)`
- `buyTickets(count)` — payable, send `count * ticketPrice`
- `drawWinner()` — permissionless, callable after the sale window closes
- `getRound(id)` — round summary

Example: 100 SHIT tickets, ~1 day rounds (576 blocks at 150s/block):
`new ShitLottery(100 ether, 576)`

> Randomness note: the draw uses the closing block's blockhash. Fun-grade, not
> casino-grade — see whitepaper §7.2 and the contract's dev comments.

### ShitSwap.sol
Minimal constant-product AMM (Uniswap V2 design): `ShitSwapFactory` deploys
`ShitSwapPair` pools via CREATE2. 0.3% swap fee accrues to LPs. Each pair is
itself an ERC-20 LP token ("SLP").

- `ShitSwapFactory.createPair(tokenA, tokenB)` — permissionless pool creation
- `pair.mint(to)` — add liquidity (send tokens to the pair first)
- `pair.burn(to)` — remove liquidity (send LP tokens to the pair first)
- `pair.swap(amount0Out, amount1Out, to)` — trade (send input tokens first)

In production, wrap these with a router contract that handles the
send-then-call pattern and slippage checks in one transaction.

### ShitVault.sol
Time-locked SHIT vault. Lock native SHIT for **1 to 5 years**; longer locks
earn higher APY, paid from a governance-funded reward pool:

| Lock | APY |
|------|-----|
| 1 year | 4% |
| 2 years | 6% |
| 3 years | 9% |
| 4 years | 12% |
| 5 years | 15% |

- `createLock(years)` — payable, `years` in 1..5; returns a lock id
- `accruedRewards(lockId)` — view earned-but-unclaimed rewards
- `claimRewards(lockId)` — claim rewards without unlocking principal
- `withdraw(lockId)` — after expiry: principal + rewards
- `emergencyWithdraw(lockId)` — exit early: **10% of principal burned**,
  rewards forfeited. Diamond hands only.
- `fundRewards()` / `setTierAPY(years, bps)` — governance (owner) functions

**Note:** ShitVault.sol is the DeFi companion vault (with a 10%-burn early
exit). The canonical, trustless vault is native: `vaultlock`/`vaultclaim`/
`vaultinfo` RPCs backed by a consensus-level 3.57B SHIT reserve that nobody
holds — yield is minted by the protocol, capped at 47.6M SHIT/year over
75 years. See whitepaper §7.4.

## Build artifacts

`build/` contains compiler output for every contract (`abi` + `bytecode`,
compiled with solc 0.8.24), ready for any deployment tooling:

- `build/ShitSwapFactory.json`, `build/ShitSwapPair.json`
- `build/ShitLottery.json`
- `build/ShitVault.json`

## Deploying

```bash
pip install web3
export RPC_URL="https://<your-shitcoin-nevm-rpc>"
export PRIVATE_KEY="0x..."   # deployer key — never commit this
python3 deploy.py --ticket-price 100 --round-blocks 576
```

This deploys `ShitSwapFactory`, `ShitLottery`, and `ShitVault` in order and
writes their addresses to `deployments.json`. The deployer needs SHIT for gas.

## Disclaimer
Reference implementations, not audits. Review and test before mainnet use.
