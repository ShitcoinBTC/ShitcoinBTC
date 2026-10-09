// Copyright (c) 2009-2010 Satoshi Nakamoto
// Copyright (c) 2009-2022 The Bitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <pow.h>

#include <arith_uint256.h>
#include <chain.h>
#include <primitives/block.h>
#include <uint256.h>

// Shitcoin emergency difficulty fork (2026-09-26).
//
// What happened: a miner produced ~14.4k blocks in ~5h, driving difficulty from
// 0.25 to 16384, then left. The legacy 1800-block retarget cannot recover from
// that: with no hashpower behind the stranded difficulty no blocks are found,
// so no retarget ever happens (deadlock: no blocks -> no retarget -> no blocks).
//
// The fix: from SHIT_DIFF_FORK_HEIGHT on, difficulty retargets EVERY block
// from the single most recent block interval, so it tracks hashpower
// immediately in both directions. The first post-fork block performs a
// one-time rebase to the last known-good pre-attack difficulty, clearing the
// stranded value.
//
// The controller: target_{n+1} = target_n * ((K+1)/K)^(dt-T), where
// dt = tip_time - parent_time clamped to [T/4, 4T], T = PowTargetSpacing
// (12s pre-NEVM, 30s post-NEVM), K = SHIT_DIFF_K. Computed with pure integer
// arithmetic (iterative multiply/divide), so every node gets the identical
// result; no floating point anywhere in consensus.
//
// Why this form: log(target_{n+1}/target_n) = (dt-T)*log(1+1/K) is LINEAR in
// dt, so E[log factor] = 0 at equilibrium (no long-term drift to zero or
// infinity), and factor(T) = 1 exactly (a block mined right on schedule
// changes nothing). Validated by simulation: steady state holds ~12s mean
// with no drift over 2000 blocks; a 1000x hashpower spike is absorbed within
// ~40-80 blocks; a 1000x hashpower drop recovers geometrically within ~20-40
// blocks. Per-block move is bounded (see below), so timestamp games are
// limited to the same range.
//
// NOTE: these are intentionally file-local constants rather than consensus
// params so the emergency rebuild only recompiles this file. A future cleanup
// should move them into Consensus::Params.
static const int64_t SHIT_DIFF_FORK_HEIGHT = 29196;
static const uint32_t SHIT_DIFF_REBASE_BITS = 0x1d03fffc; // blocks 14400/14788: difficulty 0.25
static const uint32_t SHIT_DIFF_K = 48; // controller gain; halflife ~ K*ln2 seconds of deviation
static const uint32_t SHIT_DIFF_MAX_TRANSITION = 8; // sanity bound for PermittedDifficultyTransition
// Shitcoin emergency difficulty fork #2 (2026-09-27).
// The per-block controller from fork #1 lets difficulty spike too aggressively
// when hashpower is bursty. A CPU miner drove it to 67099, stranding the chain
// (no blocks -> slow recovery). This fork performs a one-time rebase to
// CPU-mineable difficulty at SHIT_DIFF_FORK2_HEIGHT, then the per-block
// controller resumes from there.
static const int64_t SHIT_DIFF_FORK2_HEIGHT = 30950;
static const uint32_t SHIT_DIFF_FORK2_REBASE_BITS = 0x1d63ffff; // ~0.01 difficulty, CPU-mineable
// Shitcoin difficulty floor fork #3 (2026-10-03, Jodie's idea).
// Fixes the boom/bust oscillation: one bursty miner making ~every block plus
// the aggressive per-block controller (up to ~6.5x target move per block)
// whipsaws difficulty — long stall, collapse, flood of fast blocks, spike,
// stall again. From SHIT_DIFF_FORK3_HEIGHT on, the target may never exceed
// SHIT_DIFF_FLOOR_TARGET (difficulty never below ~0.01, CPU-mineable): the
// controller still eases during quiet spells, but never into the collapse
// that triggers the flood/spike. No rebase at the fork height — just clamp
// from here on; if the current target is already easier than the floor it is
// left alone until the controller hardens it back up.
// CONSENSUS: all nodes and all miners must upgrade before this height.
static const int64_t SHIT_DIFF_FORK3_HEIGHT = 48460;
static const uint32_t SHIT_DIFF_FLOOR_BITS = 0x1d63ffff; // ~0.01 difficulty, CPU-mineable
// Max single-block target move: ((K+1)/K)^(3T). T=12 -> ~2.1x, T=30 -> ~6.4x,
// both inside SHIT_DIFF_MAX_TRANSITION.

// Shitcoin: per-block difficulty retarget, single-interval controller.
// Returns the exact nBits for the block AFTER pindexLast. Pure function of
// chain data, so every node computes the same value (consensus-critical).
static unsigned int CalculatePerBlockWorkRequired(const CBlockIndex* pindexLast, const Consensus::Params& params)
{
    if (pindexLast->pprev == nullptr) {
        // Unreachable on mainnet (fork is far past genesis); keep difficulty
        // unchanged rather than crashing if it ever happens.
        return pindexLast->nBits;
    }
    const int64_t nextHeight = pindexLast->nHeight + 1;
    // Target spacing follows the NEVM flag (12s pre-NEVM, 30s post-NEVM).
    const int64_t spacing = params.PowTargetSpacing(nextHeight);
    // Single-block interval, clamped to [spacing/4, spacing*4]. The clamp
    // bounds the per-block difficulty move and limits timestamp games.
    int64_t dt = pindexLast->GetBlockTime() - pindexLast->pprev->GetBlockTime();
    const int64_t minDt = spacing / 4;
    const int64_t maxDt = spacing * 4;
    if (dt < minDt) dt = minDt;
    if (dt > maxDt) dt = maxDt;
    const int64_t u = dt - spacing; // |u| <= 3*spacing (<= 90 post-NEVM)

    arith_uint256 bnNew;
    bnNew.SetCompact(pindexLast->nBits);
    // target *= ((K+1)/K)^u, applied iteratively so every intermediate stays
    // in 256 bits and is bit-identical on all platforms (no floats).
    // u > 0 (slow block): ease. u < 0 (fast block): harden.
    const arith_uint256 bnK(SHIT_DIFF_K);
    const arith_uint256 bnKp1(SHIT_DIFF_K + 1);
    if (u > 0) {
        for (int64_t i = 0; i < u; ++i) {
            bnNew *= (SHIT_DIFF_K + 1);
            bnNew /= bnK;
        }
    } else if (u < 0) {
        for (int64_t i = 0; i < -u; ++i) {
            bnNew *= SHIT_DIFF_K;
            bnNew /= bnKp1;
        }
    }

    const arith_uint256 bnPowLimit = UintToArith256(params.powLimit);
    if (bnNew > bnPowLimit) bnNew = bnPowLimit;
    // Fork #3: difficulty floor. Never let the target go easier than the
    // CPU-mineable floor — this is what breaks the collapse/flood/spike cycle.
    if (nextHeight >= SHIT_DIFF_FORK3_HEIGHT) {
        arith_uint256 bnFloorTarget;
        bnFloorTarget.SetCompact(SHIT_DIFF_FLOOR_BITS);
        if (bnNew > bnFloorTarget) bnNew = bnFloorTarget;
    }
    if (bnNew == 0) bnNew = arith_uint256(1); // unreachable; never strand the chain
    return bnNew.GetCompact();
}

unsigned int GetNextWorkRequired(const CBlockIndex* pindexLast, const CBlockHeader *pblock, const Consensus::Params& params)
{
    assert(pindexLast != nullptr);
    unsigned int nProofOfWorkLimit = UintToArith256(params.powLimit).GetCompact();
    int64_t adjustmentInterval = params.DifficultyAdjustmentInterval(pindexLast->nHeight);

    // Shitcoin per-block difficulty retarget (emergency fork 2026-09-26).
    // Fork #2 (2026-09-27): one-time rebase to CPU-mineable difficulty.
    if (pindexLast->nHeight + 1 >= SHIT_DIFF_FORK2_HEIGHT) {
        if (pindexLast->nHeight + 1 == SHIT_DIFF_FORK2_HEIGHT) {
            // First block under fork #2: rebase to CPU-mineable difficulty.
            return SHIT_DIFF_FORK2_REBASE_BITS;
        }
        // After fork #2, use the per-block controller (same as fork #1).
        return CalculatePerBlockWorkRequired(pindexLast, params);
    }
    if (pindexLast->nHeight >= SHIT_DIFF_FORK_HEIGHT) {
        return CalculatePerBlockWorkRequired(pindexLast, params);
    }
    if (pindexLast->nHeight + 1 >= SHIT_DIFF_FORK_HEIGHT) {
        // First block under the new rules: one-time rebase to the last
        // known-good pre-attack difficulty, clearing the stranded value.
        return SHIT_DIFF_REBASE_BITS;
    }

    // Only change once per difficulty adjustment interval
    if ((pindexLast->nHeight+1) % adjustmentInterval != 0)
    {
        if (params.fPowAllowMinDifficultyBlocks)
        {
            // SYSCOIN Special difficulty rule for testnet:
            // If the new block's timestamp is more than 2* 10 minutes
            // then allow mining of a min-difficulty block.
            if (pblock->GetBlockTime() > pindexLast->GetBlockTime() + params.nPowTargetSpacing*8)
                return nProofOfWorkLimit;
            else
            {
                // Return the last non-special-min-difficulty-rules-block
                const CBlockIndex* pindex = pindexLast;
                while (pindex->pprev && pindex->nHeight % adjustmentInterval != 0 && pindex->nBits == nProofOfWorkLimit)
                    pindex = pindex->pprev;
                return pindex->nBits;
            }
        }
        return pindexLast->nBits;
    }

    // Go back by what we want to be 14 days worth of blocks
    int nHeightFirst = pindexLast->nHeight - (adjustmentInterval-1);
    assert(nHeightFirst >= 0);
    const CBlockIndex* pindexFirst = pindexLast->GetAncestor(nHeightFirst);
    assert(pindexFirst);

    return CalculateNextWorkRequired(pindexLast, pindexFirst->GetBlockTime(), params);
}

unsigned int CalculateNextWorkRequired(const CBlockIndex* pindexLast, int64_t nFirstBlockTime, const Consensus::Params& params)
{
    if (params.fPowNoRetargeting)
        return pindexLast->nBits;

    // Limit adjustment step
    int64_t nActualTimespan = pindexLast->GetBlockTime() - nFirstBlockTime;
    if(pindexLast->nHeight >= params.nBridgeStartBlock){
        if (nActualTimespan < 17280)// params.nPowTargetTimespan * (8/10)
            nActualTimespan = 17280;
        if (nActualTimespan > 27000) // params.nPowTargetTimespan * (10/8)
            nActualTimespan = 27000;
    }
    else{
        if (nActualTimespan < params.nPowTargetTimespan/4)
            nActualTimespan = params.nPowTargetTimespan/4;
        if (nActualTimespan > params.nPowTargetTimespan*4)
            nActualTimespan = params.nPowTargetTimespan*4;
    }
    // Retarget
    const arith_uint256 bnPowLimit = UintToArith256(params.powLimit);
    arith_uint256 bnNew;
    bnNew.SetCompact(pindexLast->nBits);
    bnNew *= nActualTimespan;
    bnNew /= params.nPowTargetTimespan;

    if (bnNew > bnPowLimit)
        bnNew = bnPowLimit;

    return bnNew.GetCompact();
}

// Check that on difficulty adjustments, the new difficulty does not increase
// or decrease beyond the permitted limits.
bool PermittedDifficultyTransition(const Consensus::Params& params, int64_t height, uint32_t old_nbits, uint32_t new_nbits)
{
    if (params.fPowAllowMinDifficultyBlocks) return true;
    // Shitcoin per-block retarget (emergency fork 2026-09-26): from the fork
    // on, difficulty may move up to SHIT_DIFF_MAX_TRANSITION per block in
    // either direction (loose sanity bound; the actual controller moves far
    // less); the fork block itself performs the one-time rebase and is exempt.
    // Fork #2 (2026-09-27) rebases again at SHIT_DIFF_FORK2_HEIGHT.
    if (height >= SHIT_DIFF_FORK_HEIGHT) {
        if (height == SHIT_DIFF_FORK_HEIGHT) return true;
        if (height == SHIT_DIFF_FORK2_HEIGHT) return true;
        const arith_uint256 pow_limit = UintToArith256(params.powLimit);
        arith_uint256 observed_new_target;
        observed_new_target.SetCompact(new_nbits);

        arith_uint256 largest_difficulty_target;
        largest_difficulty_target.SetCompact(old_nbits);
        largest_difficulty_target *= SHIT_DIFF_MAX_TRANSITION;
        if (largest_difficulty_target > pow_limit) {
            largest_difficulty_target = pow_limit;
        }
        arith_uint256 maximum_new_target;
        maximum_new_target.SetCompact(largest_difficulty_target.GetCompact());
        if (maximum_new_target < observed_new_target) return false;

        arith_uint256 smallest_difficulty_target;
        smallest_difficulty_target.SetCompact(old_nbits);
        smallest_difficulty_target /= arith_uint256(SHIT_DIFF_MAX_TRANSITION);
        arith_uint256 minimum_new_target;
        minimum_new_target.SetCompact(smallest_difficulty_target.GetCompact());
        if (minimum_new_target > observed_new_target) return false;
        return true;
    }
    // SYSCOIN
    if (height % params.DifficultyAdjustmentInterval(height-1) == 0) {
        int64_t smallest_timespan = params.nPowTargetTimespan/4;
        int64_t largest_timespan = params.nPowTargetTimespan*4;

        const arith_uint256 pow_limit = UintToArith256(params.powLimit);
        arith_uint256 observed_new_target;
        observed_new_target.SetCompact(new_nbits);

        // Calculate the largest difficulty value possible:
        arith_uint256 largest_difficulty_target;
        largest_difficulty_target.SetCompact(old_nbits);
        largest_difficulty_target *= largest_timespan;
        largest_difficulty_target /= params.nPowTargetTimespan;

        if (largest_difficulty_target > pow_limit) {
            largest_difficulty_target = pow_limit;
        }

        // Round and then compare this new calculated value to what is
        // observed.
        arith_uint256 maximum_new_target;
        maximum_new_target.SetCompact(largest_difficulty_target.GetCompact());
        if (maximum_new_target < observed_new_target) return false;

        // Calculate the smallest difficulty value possible:
        arith_uint256 smallest_difficulty_target;
        smallest_difficulty_target.SetCompact(old_nbits);
        smallest_difficulty_target *= smallest_timespan;
        smallest_difficulty_target /= params.nPowTargetTimespan;

        if (smallest_difficulty_target > pow_limit) {
            smallest_difficulty_target = pow_limit;
        }

        // Round and then compare this new calculated value to what is
        // observed.
        arith_uint256 minimum_new_target;
        minimum_new_target.SetCompact(smallest_difficulty_target.GetCompact());
        if (minimum_new_target > observed_new_target) return false;
    } else if (old_nbits != new_nbits) {
        return false;
    }
    return true;
}

bool CheckProofOfWork(uint256 hash, unsigned int nBits, const Consensus::Params& params)
{
    bool fNegative;
    bool fOverflow;
    arith_uint256 bnTarget;

    bnTarget.SetCompact(nBits, &fNegative, &fOverflow);

    // Check range
    if (fNegative || bnTarget == 0 || fOverflow || bnTarget > UintToArith256(params.powLimit))
        return false;

    // Check proof of work matches claimed amount
    if (UintToArith256(hash) > bnTarget)
        return false;

    return true;
}
