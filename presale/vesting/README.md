# ShitVesting — SHIT-20 presale vesting vault

Solidity vesting contract for the ShitcoinBtc NEVM sidechain (chain ID 20026).
Enforces the presale lock-up on-chain: **12 equal monthly unlocks per buyer, no cliff, linear**.

Buyer claims unlocked SHIT permissionlessly via `release()`; `releasable()` shows
what's claimable right now. Schedules are non-revocable — once registered, only
time unlocks the funds.

## Terms (enforced)

| Term | Value |
|---|---|
| Unlock schedule | 12 equal monthly tranches |
| Cliff | none — first 1/12 unlocks 1 month after start |
| Vesting math | `vested = total * min(elapsedMonths, 12) / 12` |
| Month length | 30 days |
| Claim | permissionless, buyer calls `release()` themselves |

Example: 24,000 SHIT starting 2026-10-01 → 2,000 SHIT claimable each month,
fully unlocked after 12 months.

## Deploy

Prerequisites: the SHIT-20 token contract deployed on the NEVM chain, and a
deployer key with NEVM gas funds.

1. Compile with solc 0.8.x (no external imports — the file is self-contained):
   ```
   solc --bin --abi presale/vesting/ShitVesting.sol -o presale/vesting/build/
   ```
2. Deploy with the SHIT-20 token address as the constructor argument:
   ```
   constructor(address token_)   # token_ = <SHIT_TOKEN_ADDRESS>
   ```
3. **Fund the vault**: transfer the total presale allocation of SHIT into the
   deployed contract address. Check with `vaultBalance()`.
4. **Register schedules** (owner only, once at launch). Single:
   ```
   register(beneficiary, amount, start)
   ```
   Or batched (cheaper — one tx for the whole presale list):
   ```
   registerBatch(beneficiaries[], amounts[], start)
   ```
   - `amount` is in token base units (e.g. wei-style, 18 decimals).
   - `start` is the unix timestamp the 12-month clock starts at — use the
     chain-launch timestamp for every buyer.
   - Registering an address twice **tops up** its schedule (same start kept).
5. Verify the contract source on the NEVM explorer (phase-2 Blockscout).
6. (Recommended) `transferOwnership()` to a multisig once registration closes,
   so no single key can register further schedules.

### Registration checklist

- [ ] Token contract deployed; address recorded
- [ ] ShitVesting deployed + source verified
- [ ] Vault funded: `vaultBalance()` ≥ sum of all schedule totals
- [ ] All buyer schedules registered (spot-check `schedules(addr)` and `releasable(addr)`)
- [ ] Ownership moved to multisig / timelock

## Security notes

- Test the full flow (register → warp 30 days → release) on a local NEVM devnet first.
- `release()` is reentrancy-guarded and follows checks-effects-interactions.
- `sweep()` can only rescue *non*-SHIT tokens; vested SHIT can never be pulled by the owner.
- Months are fixed 30-day periods, not calendar months — documented in the presale terms.
- Integer division rounds down; the 12th unlock releases any dust remainder, so the full total is always claimable by month 12.
- The contract trusts `block.timestamp` for unlock timing (standard for vesting; fine on a 30s-block chain).

## Files

- `ShitVesting.sol` — the contract (MIT licensed)
