<p align="center">
  <img src="assets/shitcoin-logo.png" width="180" alt="Shitcoin logo">
</p>
<p align="center">
  <img src="doc/shitcoin-wordmark.png" width="420" alt="Shitcoin">
</p>

Shitcoin integration/staging tree
=====================================

Read the [Shitcoin whitepaper](doc/whitepaper.md) ([PDF](doc/whitepaper.pdf)) — merged mining with Bitcoin, the UTXO↔NEVM bridge (Shitbridge), ShitSwap DEX, and the ShitLottery (90% to the winner, 10% burned). NEVM smart contracts live in [contracts/](contracts/).

[![Build binaries](https://github.com/ShitcoinBTC/ShitcoinBtc/actions/workflows/build-binaries.yml/badge.svg)](https://github.com/ShitcoinBTC/ShitcoinBtc/actions/workflows/build-binaries.yml)

For an immediately usable, binary version of the Shitcoin Core software, see the
[releases page](https://github.com/ShitcoinBTC/ShitcoinBtc/releases).

Further information about Shitcoin Core is available in the [doc folder](/doc).

Current release and chain facts
-------------------------------

- **Current version: v5.0.9.** The network runs v5.0.9, and that is the
  version operators should run. The v5.0.9 node files are on the project
  downloads page: https://shitcoinbtc.xyz/downloads/ — the `syscoind`
  file there has a SHA256 checksum beginning `436dba8c`
  (`436dba8c3bd7983887cc3d14945143ff13ea8db95ffaa1e1c28de8a7337f2422`).
  Check your download against `SHA256SUMS.txt` from the same page
  before you install it.
- The newest release on the GitHub releases page is v5.0.7 (2026-10-17).
  That is an older release; the network has moved on to v5.0.9.
- **AuxPoW chain ID: 29.** This changed at block 88,000 on 2026-10-18.
  It was 16 before block 88,000. Since the change, SHIT and SYS can
  share one merge-mining tree under Bitcoin as the real parent chain.
- **NEVM chain ID: 20026.** This is a different number from the AuxPoW
  chain ID. Do not confuse them: 29 is the AuxPoW chain ID for merged
  mining, 20026 is the chain ID for NEVM smart contracts and wallets.
- **sysgeth:** with v5.0.9 the daemon manages sysgeth itself — it
  launches the `sysgeth` binary found beside `syscoind` and passes
  `--syscoin` and `--nevmpub` itself, driven by `syscoin.conf`
  (`zmqpubnevm=tcp://127.0.0.1:9377` plus the `gethcommandline` lines,
  including the required `--syscoin.statebootstrap.disable`). Do not
  run sysgeth as a separate service with this build. Without the
  `zmqpubnevm` line, NEVM blocks fail with `nevm-data-not-found`.
  See `doc/node-operator.md` for the full recipe.

Join the mining pool
---------------------

Use these settings in your ASIC or mining software:

```text
Pool URL:  stratum+tcp://170.101.111.196:3333
Username:  your own Bitcoin address (.worker optional, for example .rig1)
Password:  your SHIT address (or x if you only want Bitcoin)
```

Putting your SHIT address in the password field registers it when your miner connects. The first SHIT address registered wins, and reconnecting can never replace it. If the password is x, or anything that is not a valid SHIT address, no address is registered and your SHIT builds up and waits. The signed message form on the pool dashboard is now only for changing an address that is already on file.

Taproot (bc1p) and exchange Bitcoin addresses can register a SHIT address in the password field like everyone else, but they cannot sign a message, so an address registered for them cannot be changed later.

Check your stats on the [pool dashboard](http://170.101.111.196:8080/).

Pool operators running the stratum proxy: use the current proxy file
from [contrib/pool-onboarding/](contrib/pool-onboarding/). It was
updated on the morning of the block 88,000 fork so it stamps AuxPoW
chain ID 16 below block 88,000 and chain ID 29 at block 88,000 and
above. The current server proxy file has a SHA256 checksum beginning
`d5379a47` (`d5379a475e4b6c544fa55b97866388fda47b064687edfb7a07c8ca733c3416a3`).
See the pool onboarding README for the update and check steps.

Renting hash? Use host 170.101.111.196 on port 3334 instead of 3333 (starts at difficulty 1,048,576), with the same password recipe: your SHIT address registers on connect. If you pin a difficulty with password `d=<diff>` instead, no SHIT address is registered and your SHIT builds up and waits. Port 3333 stays the general port.

Payouts are PPLNS and run hourly, starting over 0.0005 BTC and 100 SHIT. Pool fee: 0.002 percent of Bitcoin block rewards, and 1 percent of merge-mined SHIT.

What is Shitcoin Core?
----------------

Shitcoin Core is an experimental digital currency that enables instant payments to anyone, anywhere in the world. SHIT uses peer-to-peer technology to operate with no central authority: managing transactions and issuing money are carried out collectively by the network. Shitcoin Core is the name of the software which enables the use of this currency.

For more information, as well as an immediately usable, binary version of the Shitcoin Core software, see https://shitcoinbtc.xyz/.

Shitcoin is a merge-minable SHA256 coin which provides an array of useful services which leverage the bitcoin protocol and blockchain technology.

Hybrid layer 2 PoW/PoS consensus with bonded validator system (masternodes). Trustless sidechain access to the NEVM and back through a custom permissionless/trustless sidechain technology (Shitbridge). Decentralized governance (blockchain pays for work via proposals and masternode votes). Digital asset creation and management.

Interoperability between UTXO assets and the SHIT-20 NEVM account model through a trustless, zero-custodian, zero-counterparty internal bridge (Shitbridge).

Shitcoin is a merge-minable SHA256 coin which provides an array of useful services which leverage the bitcoin protocol and blockchain technology. It enables turing complete smart contracts running in the NEVM (Network-enhanced Virtual Machine) to leverage bitcoin security through merged-mining. Scaling the technology will happen on layer 2 (zkRollups for NEVM and Lightning Networks for UTXO assets).

- Block time: 30 seconds target
- Halving interval: 1051200 (~1 year)
- Max supply: 21,000,000,000 SHIT (21 billion, hard cap)
  - Block 1 allocations: 18% presale (3.78B) + 14% team (2.94B) + 2% coin support (420M) + 6% wrapping reserve (1.26B, backs wrapped SHIT), enforced by consensus
  - Vault yield reserve: 17% (3.57B) as a protocol-level reserve (no custodian) — minted as vault yield, capped at 47.6M SHIT/year over 75 years
  - Native vault: `vaultlock` / `vaultclaim` / `vaultinfo` RPCs; 1–5 year timelocks at 4/6/9/12/15% APY; requires holding any amount of BTC (wallet-enforced)
  - Mining: 43% (9.03B) via block rewards — ~198.07 SHIT per block at launch, deflated 2 percent per year over a 100-year tail (merge-mined with Bitcoin)
  - 10 percent to governance proposals
  - 90 percent split with miner/masternode of which:
    - 25 percent to miner
    - 75 percent to masternode
- 50 percent of the transaction fees paid to masternode
- Masternode minimum subsidy (before seniority): 5.275 SHIT (can not go below this amount even accounting for deflation)
- NEVM subsidy (EIP1559): 10.55 SHIT (static, not deflating)
- SHA256 Proof of Work
- Merge-mined with Bitcoin — merge-mining is mandatory from block 1 (standalone mining is rejected by consensus)
- Masternode collateral requirement: 100000 
- Masternode seniority: 35 percent increase after 1051200 blocks (~1 year), 100 percent increase after 2628000 blocks (~2.5 years)
- Governance proposals payout schedule: every 17520 blocks (~1 month)
- Governance funding per round (Approx. 2m shit per month to start)
- Governance funding gets 5% deflation per round (superblock). See formula below
- Codebase based off of latest Bitcoin Core (https://github.com/bitcoin/bitcoin)

For more information read the Shitcoin whitepaper.

License
-------

Shitcoin Core is released under the terms of the MIT license. See [COPYING](COPYING) for more
information or see https://opensource.org/licenses/MIT.

Development Process
-------------------

The `master` branch is regularly built (see `doc/build-*.md` for instructions) and tested, but it is not guaranteed to be
completely stable. [Tags](https://github.com/ShitcoinBTC/ShitcoinBtc/tags) are created
regularly from release branches to indicate new official, stable release versions of Shitcoin Core.

The contribution workflow is described in [CONTRIBUTING.md](CONTRIBUTING.md)
and useful hints for developers can be found in [doc/developer-notes.md](doc/developer-notes.md).

Testing
-------

Testing and code review is the bottleneck for development; we get more pull
requests than we can review and test on short notice. Please be patient and help out by testing
other people's pull requests, and remember this is a security-critical project where any mistake might cost people
lots of money.

### Automated Testing

Developers are strongly encouraged to write [unit tests](src/test/README.md) for new code, and to
submit new unit tests for old code. Unit tests can be compiled and run
(assuming they weren't disabled in configure) with: `make check`. Further details on running
and extending unit tests can be found in [/src/test/README.md](/src/test/README.md).

There are also [regression and integration tests](/test), written
in Python.
These tests can be run (if the [test dependencies](/test) are installed) with: `test/functional/test_runner.py`

The CI (Continuous Integration) systems make sure that every pull request is built for Windows, Linux, and macOS,
and that unit/sanity tests are run automatically.

### Manual Quality Assurance (QA) Testing

Changes should be tested by somebody other than the developer who wrote the
code. This is especially important for large or high-risk changes. It is useful
to add a test plan to the pull request description if testing the changes is
not straightforward.

Translations
------------

Changes to translations as well as new translations can be submitted as
GitHub pull requests. See the [translation process](doc/translation_process.md)
for details on how this works.
