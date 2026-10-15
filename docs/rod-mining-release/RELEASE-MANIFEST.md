# RELEASE MANIFEST — mining setup release, complete component list

Staged 2026-10-14 on branch `release/rod-mining-2026-10-14` (public ShitcoinBTC chain
repo). **Nothing in this release is ShityDEX or any other built product** — the branch
diff against master contains mining/chain files only (verified at staging).
Mining is public per Jodie's rule; our built products are proprietary and are not in
this repo line.

## In the push (branch commits)

| Component | Where | Status |
|---|---|---|
| Chain source (syscoind v5.0.5) | repo master `984349c` — already public | ✅ in repo |
| Real-Bitcoin mining guide + node checker | commits `8b9cdb0`, `db13618` (`contrib/mining/`) | ✅ staged |
| `src/mine.sh` regtest guard; getwork wrapper test-only marking; README mining section | commits `8c2835d`, `45fcc08`, `643c124` | ✅ staged |
| Operator package (README/versions, SETUP, INTEGRATION, UPGRADE, ROOT-CAUSE-NOTE, HANDOVER-SUMMARY, POOL-NODE-GUIDE, check-node.py, RELEASE-NOTES-DRAFT) | `docs/rod-mining-release/` | ✅ staged |
| **Reference pool implementation** — the production pool software (`pool.py`, sha256 `1d15b29a…09ce`, byte-identical to the deployed build), sanitized `pool.conf.example` (all keys, placeholder values — no live credentials), reference-pool README | `docs/rod-mining-release/reference-pool/` | ✅ staged |

## Attached at publish time (GitHub release assets — not git-tree files)

| Component | Note |
|---|---|
| `syscoind` node binaries | The production build is sha256 `a60885f26b2f78954e4979738e9e4b06dc9658803f79fc80ce3bcbd57472c1c3` (410 MB). Attach the built binaries to the GitHub release at publish; a 410 MB binary does not belong in git history. Operators can always build from the public source instead — same tree, same hash class. |
| Wallet binaries | Already published in their own release (v1.0.1, 2026-10-09) — not duplicated here. |

## Assessed as NOT required for an external pool operator

| Component | Why not required / status |
|---|---|
| sysgeth (NEVM geth fork) | A **pool node does not need a local sysgeth**: per the verified pool-node guide, pool nodes run WITHOUT `zmqpubnevm` and validate NEVM in lockstep from P2P; `createauxblock` work issuance does not depend on a local geth feed. (Our *pool software's* optional SHIT leg uses a NEVM feed on the pool host — an internal topology choice, off by default in `pool.conf.example`.) sysgeth source lives in its own fork and is a separate publication item if a full NEVM-validating node is wanted. |
| `utxo-935000.dat` snapshot | 9 GB data file; distribution is a separate pending decision. The setup works without it (sync from scratch); the guide states its status and the surviving hash prefix honestly. |
| Dashboard templates for the reference pool | Cosmetic web templates that live with the host deployment; the stratum/accounting/payout core (`pool.py`) is complete without them. Noted here so nobody mistakes their absence for a missing mining component. |

## Hard boundary check (run at staging)

`git diff origin/master --name-only` on the release branch: chain/mining/docs files
only — **zero ShityDEX, exchange, games, or other product files.** ShityDEX is not in
this repository line and nothing from it was staged.
