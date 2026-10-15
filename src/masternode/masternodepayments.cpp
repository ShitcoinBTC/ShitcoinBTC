// Copyright (c) 2014-2019 The Dash Core developers
// Distributed under the MIT/X11 software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <masternode/activemasternode.h>
#include <governance/governanceclasses.h>
#include <masternode/masternodepayments.h>
#include <masternode/masternodesync.h>
#include <netfulfilledman.h>
#include <netmessagemaker.h>
#include <spork.h>
#include <validation.h>

#include <evo/deterministicmns.h>
#include <evo/specialtx.h>
#include <string>

CMasternodePayments mnpayments;
void CheckAndWriteBudget(const CAmount& nSuperblockPayment, const CAmount& nPaymentLimit, const CAmount& nGovernanceBudgetUp, const CBlockIndex* pindex) {
    CAmount nGovernanceBudgetDown = (nPaymentLimit * CSuperblock::SHIFT_DOWN) / CSuperblock::SHIFT;
    if (nGovernanceBudgetDown < CSuperblock::SUPERBLOCK_BUDGET_MIN) {
        nGovernanceBudgetDown = CSuperblock::SUPERBLOCK_BUDGET_MIN;
    }
    CAmount nPaymentsLimitUp   = (nPaymentLimit * CSuperblock::SHIFT_HALF_UP)   / CSuperblock::SHIFT;
    CAmount nPaymentsLimitDown = (nPaymentLimit * CSuperblock::SHIFT_HALF_DOWN) / CSuperblock::SHIFT;

    CAmount nAdjustment = nPaymentLimit;
    if(nSuperblockPayment > 0 && nSuperblockPayment <= nPaymentsLimitDown) {
        nAdjustment = nGovernanceBudgetDown;
    } else if(nSuperblockPayment >= nPaymentsLimitUp) {
        nAdjustment = nGovernanceBudgetUp;
    }
    governance->m_sb->WriteCache(pindex->GetBlockHash(), nAdjustment);
    if(nAdjustment != nPaymentLimit)
        LogPrint(BCLog::GOBJECT, "%s -- Adjusting SB limit to %lld (from %lld) for block %s\n", __func__, nAdjustment, nPaymentLimit, pindex->GetBlockHash().GetHex());
}
/**
* IsBlockValueValid
*
*   Determine if coinbase outgoing created money is the correct value
*
*   Superblocks are removed (consensus change). Historically, blocks at
*   superblock heights could output much higher amounts of coins, paid as
*   governance budget in the coinbase -- newly minted issuance on top of
*   the block subsidy. That allowance no longer exists at any height:
*   every block is held to the plain block-reward cap, where blockReward
*   is GetBlockSubsidy (already net of the withheld 10%, which is never
*   issued) plus the block's fees. No height, triggered superblock, or
*   budget state grants any exception.
*/

bool IsBlockValueValid(const CBlock& block, const CBlockIndex* pindex, const CAmount &blockReward, std::string& strErrorRet, bool fJustCheck, bool check_superblock)
{
    // Superblocks removed: the plain block-reward cap applies at EVERY
    // height, including former superblock heights (multiples of the
    // superblock cycle), before and after DIP0003Height alike. The
    // governance budget machinery below this function historically granted
    // an allowance of blockReward + a ratcheting budget (default
    // 2,000,000 SHIT per cycle, up to 5,000,000) of extra minting in the
    // superblock coinbase, accepted outright when masternode sync state
    // was incomplete or superblock checking was skipped. None of those
    // paths exist anymore: no triggered superblock, budget cache entry,
    // or sync state can make a coinbase above blockReward valid.
    // fJustCheck and check_superblock are retained in the signature for
    // call-site compatibility and no longer affect the verdict.
    bool isBlockRewardValueMet = (block.vtx[0]->GetValueOut() <= blockReward);
    const int nBlockHeight = pindex->nHeight;
    strErrorRet = "";

    LogPrint(BCLog::MNPAYMENTS, "block.vtx[0]->GetValueOut() %lld <= blockReward %lld\n", block.vtx[0]->GetValueOut(), blockReward);
    if (!isBlockRewardValueMet) {
        strErrorRet = strprintf("coinbase pays too much at height %d (actual=%d vs limit=%d), exceeded block reward, only regular blocks are allowed at this height",
                        nBlockHeight, block.vtx[0]->GetValueOut(), blockReward);
    }
    return isBlockRewardValueMet;
}

bool IsBlockPayeeValid(CChain& activeChain, const CTransaction& txNew, int nBlockHeight, const CAmount &blockReward, const CAmount &fees, CAmount& nMNSeniorityRet, CAmount& nMNFloorDiffRet)
{

    // we are still using budgets, but we have no data about them anymore,
    // we can only check masternode payments

    const Consensus::Params& consensusParams = Params().GetConsensus();

    if(nBlockHeight < consensusParams.nSuperblockStartBlock) {
        // NOTE: old budget system is disabled since 12.1 and we should never enter this branch
        // anymore when sync is finished (on mainnet). We have no old budget data but these blocks
        // have tons of confirmations and can be safely accepted without payee verification
        LogPrint(BCLog::GOBJECT, "%s -- WARNING: Client synced but old budget system is disabled, accepting any payee\n", __func__);
        return true;
    }
    const CAmount nHalfFee = fees / 2;

    // Check for correct masternode payment
    if(CMasternodePayments::IsTransactionValid(activeChain, txNew, nBlockHeight, blockReward, nHalfFee, nMNSeniorityRet, nMNFloorDiffRet)) {
        LogPrint(BCLog::MNPAYMENTS, "%s -- Valid masternode payment at height %d\n", __func__, nBlockHeight);
        return true;
    }
    LogPrintf("%s -- ERROR: Invalid masternode payment detected at height %d\n", __func__, nBlockHeight); /* Continued */
    return false;
}

void FillBlockPayments(CChain& activeChain, CMutableTransaction& txNew, int nBlockHeight, const CAmount &blockReward, const CAmount &fees, std::vector<CTxOut>& voutMasternodePaymentsRet, std::vector<CTxOut>& voutSuperblockPaymentsRet)
{
    // only create superblocks if spork is enabled AND if superblock is actually triggered
    // (height should be validated inside)
    if(AreSuperblocksEnabled() &&
        CSuperblockManager::IsSuperblockTriggered(nBlockHeight)) {
            LogPrint(BCLog::GOBJECT, "%s -- triggered superblock creation at height %d\n", __func__, nBlockHeight);
            CSuperblockManager::GetSuperblockPayments(nBlockHeight, voutSuperblockPaymentsRet);
    }

    const CAmount nHalfFee = fees / 2;
    if (!CMasternodePayments::GetMasternodeTxOuts(activeChain, nBlockHeight, blockReward, voutMasternodePaymentsRet, nHalfFee)) {
        LogPrint(BCLog::MNPAYMENTS, "%s -- no masternode to pay (MN list probably empty)\n", __func__);
        return;
    }
	// miner takes 25% of the reward and half fees
    CAmount minerRewardWithMN = (blockReward + 3) / 4; 
    txNew.vout[0].nValue = voutMasternodePaymentsRet.empty()? blockReward: minerRewardWithMN;
    if(nHalfFee > 0)
	    txNew.vout[0].nValue += nHalfFee;
    // mn is paid 75% of block reward plus any seniority
    txNew.vout.insert(txNew.vout.end(), voutMasternodePaymentsRet.begin(), voutMasternodePaymentsRet.end());
    // superblock governance amount is added as extra
    txNew.vout.insert(txNew.vout.end(), voutSuperblockPaymentsRet.begin(), voutSuperblockPaymentsRet.end());
    std::string voutMasternodeStr;
    for (const auto& txout : voutMasternodePaymentsRet) {
        if (!voutMasternodeStr.empty())
            voutMasternodeStr += ",";
        voutMasternodeStr += txout.ToString();
    }

    LogPrint(BCLog::MNPAYMENTS, "%s -- nBlockHeight %d blockReward %lld voutMasternodePaymentsRet \"%s\"\n", __func__,
                            nBlockHeight, blockReward, voutMasternodeStr);
}

/**
*   GetMasternodeTxOuts
*
*   Get masternode payment tx outputs
*/

bool CMasternodePayments::GetMasternodeTxOuts(CChain& activeChain, int nBlockHeight, const CAmount &blockReward, std::vector<CTxOut>& voutMasternodePaymentsRet, const CAmount &nHalfFee)
{
    // make sure it's not filled yet
    voutMasternodePaymentsRet.clear();
    CAmount nMNSeniorityRet;
    CAmount nMNFloorDiffRet;
    int nCollateralHeight;
    if(!GetBlockTxOuts(activeChain, nBlockHeight, blockReward, voutMasternodePaymentsRet, nHalfFee, nMNSeniorityRet, nMNFloorDiffRet, nCollateralHeight)) {
        LogPrintf("CMasternodePayments::%s -- no payee (deterministic masternode list empty)\n", __func__);
        return false;
    }

    for (const auto& txout : voutMasternodePaymentsRet) {
        CTxDestination dest;
        ExtractDestination(txout.scriptPubKey, dest);

        LogPrintf("CMasternodePayments::%s -- Masternode payment %lld to %s\n", __func__, txout.nValue, EncodeDestination(dest));
    }

    return true;
}
CAmount GetBlockMNSubsidy(const CAmount &nBlockReward, unsigned int nHeight, const Consensus::Params& consensusParams, unsigned int nStartHeight, CAmount& nMNSeniorityRet, CAmount& nMNFloorDiffRet)
{
    // MN takes 75% of the subsidy
    CAmount nSubsidy = nBlockReward*0.75;
    
    // ensure that if subsidy is less than min amount then stick to min for long term MN full node/chainlock incentive
    const CAmount &nMinMN = consensusParams.nMinMNSubsidySats;
    if(nSubsidy < nMinMN) {
        nMNFloorDiffRet = nMinMN-nSubsidy;
        nSubsidy = nMinMN;
    }
    if (nHeight > 0 && nStartHeight > 0) {
        const double fSubsidyAdjustmentPercentage = consensusParams.Seniority(nHeight, nStartHeight);
        if(fSubsidyAdjustmentPercentage > 0){
            nMNSeniorityRet = nSubsidy*fSubsidyAdjustmentPercentage;
            nSubsidy += nMNSeniorityRet;
        }
    }
    return nSubsidy;
}
bool CMasternodePayments::GetBlockTxOuts(CChain& activeChain, int nBlockHeight, const CAmount &blockReward, std::vector<CTxOut>& voutMasternodePaymentsRet, const CAmount &nHalfFee, CAmount& nMNSeniorityRet, CAmount &nMNFloorDiffRet, int& nCollateralHeightRet)
{
    voutMasternodePaymentsRet.clear();
    CDeterministicMNCPtr dmnPayee;
    {
        LOCK(cs_main);
        const CBlockIndex* pindex = activeChain[nBlockHeight - 1];
        if(!pindex)
            return false;
        dmnPayee = deterministicMNManager->GetListForBlock(pindex).GetMNPayee();
        if (!dmnPayee) {
            return false;
        }
    }
    nCollateralHeightRet = dmnPayee->pdmnState->nCollateralHeight;
    CAmount masternodeReward = GetBlockMNSubsidy(blockReward, nBlockHeight, Params().GetConsensus(), nCollateralHeightRet, nMNSeniorityRet, nMNFloorDiffRet) + nHalfFee;

    CAmount operatorReward = 0;
    if (dmnPayee->nOperatorReward != 0 && dmnPayee->pdmnState->scriptOperatorPayout != CScript()) {
        // This calculation might eventually turn out to result in 0 even if an operator reward percentage is given.
        // This will however only happen in a few years when the block rewards drops very low.
        operatorReward = (masternodeReward * dmnPayee->nOperatorReward) / 10000;
        masternodeReward -= operatorReward;
    }

    if (masternodeReward > 0) {
        voutMasternodePaymentsRet.emplace_back(masternodeReward, dmnPayee->pdmnState->scriptPayout);
    }
    if (operatorReward > 0) {
        voutMasternodePaymentsRet.emplace_back(operatorReward, dmnPayee->pdmnState->scriptOperatorPayout);
    }

    return true;
}

bool CMasternodePayments::IsTransactionValid(CChain& activeChain, const CTransaction& txNew, int nBlockHeight, const CAmount &blockReward, const CAmount& nHalfFee, CAmount& nMNSeniorityRet, CAmount &nMNFloorDiffRet)
{
    if (!deterministicMNManager || !deterministicMNManager->IsDIP3Enforced(nBlockHeight)) {
        // can't verify historical blocks here
        return true;
    }

    std::vector<CTxOut> voutMasternodePayments;
    int nCollateralHeight;
    if (!GetBlockTxOuts(activeChain, nBlockHeight, blockReward, voutMasternodePayments, nHalfFee, nMNSeniorityRet, nMNFloorDiffRet, nCollateralHeight)) {
        LogPrintf("CMasternodePayments::%s -- ERROR failed to get payees for block at height %s\n", __func__, nBlockHeight);
        return true;
    }

    for (const auto& txout : voutMasternodePayments) {
        bool found = ranges::any_of(txNew.vout, [&txout](const auto& txout2) {return txout == txout2;});
        if (!found) {
            CTxDestination dest;
            if (!ExtractDestination(txout.scriptPubKey, dest))
                assert(false);
            LogPrintf("CMasternodePayments::%s -- ERROR failed to find expected payee %s in block at height %s\n", __func__, EncodeDestination(dest), nBlockHeight);
            return false;
        }
    }
    return true;
}
