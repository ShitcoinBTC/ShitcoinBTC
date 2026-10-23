<p align="center">
  <img src="shitcoin-logo.png" width="140" alt="Shitcoin logo">
</p>

Shitcoin Core
=============

**New here?** Start with the [Shitcoin whitepaper](whitepaper.md) ([PDF](whitepaper.pdf)) — it covers merged mining with Bitcoin, bridge tech, the NEVM/EVM layer, the ShitSwap DEX, and the ShitLottery game (90% winner / 10% burn). The NEVM smart contracts live in [../contracts/](../contracts/).

Setup
---------------------
Shitcoin Core builds the backbone of the Shitcoin network. It is a fork of Syscoin Core, and the node programs are still named `syscoind` and `syscoin-cli`. It downloads and, by default, stores the entire history of Shitcoin transactions. Depending on the speed of your computer and network connection, the synchronization process can take anywhere from a few hours to a day or more.

To download Shitcoin Core, use the downloads page on the project website: [shitcoinbtc.xyz/downloads](https://shitcoinbtc.xyz/downloads/). The network runs **v5.0.9**, the current version; the `syscoind` file on that page has a SHA-256 hash beginning `436dba8c`. Check your download against the `SHA256SUMS.txt` file on the same page. The newest release on this repository's [GitHub releases page](https://github.com/ShitcoinBTC/ShitcoinBTC/releases) is v5.0.7, published 2026-10-17 — an older release.

Running
---------------------
The following are some helpful notes on how to run Shitcoin Core on your native platform. Running a node, the two chain IDs, how the daemon manages sysgeth for the NEVM layer, and the merge-mining proxy are covered in [Running a Shitcoin Node: Operator Notes](node-operator.md).

### Unix

Unpack the files into a directory and run:

- `bin/syscoind` (headless node)
- `bin/syscoin-cli` (command line control for a running node)

If you run the NEVM layer, install the `sysgeth` binary beside `syscoind`: with v5.0.9 the daemon launches sysgeth itself and passes `--nevmpub` from `zmqpubnevm=tcp://127.0.0.1:9377` in `syscoin.conf` (see [node-operator.md](node-operator.md) for the full recipe). Do not run sysgeth as a separate service. Without the `zmqpubnevm` line, NEVM blocks fail with `nevm-data-not-found`.

### Need Help?

* Start with the documentation in this folder and the [Shitcoin whitepaper](whitepaper.md).
* Ask for help in the ShitcoinBTC community channels linked from the [root README](/README.md).

Building
---------------------
The following are developer notes on how to build Syscoin Core on your native platform. They are not complete guides, but include notes on the necessary libraries, compile flags, etc.

- [Dependencies](dependencies.md)
- [macOS Build Notes](build-osx.md)
- [Unix Build Notes](build-unix.md)
- [Windows Build Notes](build-windows.md)
- [FreeBSD Build Notes](build-freebsd.md)
- [OpenBSD Build Notes](build-openbsd.md)
- [NetBSD Build Notes](build-netbsd.md)
- [Android Build Notes](build-android.md)

Development
---------------------
The Syscoin repo's [root README](/README.md) contains relevant information on the development process and automated testing.

- [Developer Notes](developer-notes.md)
- [Productivity Notes](productivity.md)
- [Release Process](release-process.md)
- [Source Code Documentation (External Link)](https://doxygen.bitcoincore.org/)
- [Translation Process](translation_process.md)
- [Translation Strings Policy](translation_strings_policy.md)
- [JSON-RPC Interface](JSON-RPC-interface.md)
- [Unauthenticated REST Interface](REST-interface.md)
- [Shared Libraries](shared-libraries.md)
- [BIPS](bips.md)
- [Dnsseed Policy](dnsseed-policy.md)
- [Benchmarking](benchmarking.md)
- [Internal Design Docs](design/)

### Resources
* Discuss on the [SyscoinTalk](https://syscointalk.org/) forums, in the [Development & Technical Discussion board](https://syscointalk.org/index.php?board=6.0).
* Discuss project-specific development on #syscoin-core-dev on Freenode. If you don't have an IRC client, use [webchat here](http://webchat.freenode.net/?channels=syscoin-core-dev).
* Discuss general Syscoin development on #syscoin-dev on Freenode. If you don't have an IRC client, use [webchat here](http://webchat.freenode.net/?channels=syscoin-dev).

### Miscellaneous
- [Running a Shitcoin Node: Operator Notes](node-operator.md)
- [Assets Attribution](assets-attribution.md)
- [bitcoin.conf Configuration File](syscoin-conf.md)
- [CJDNS Support](cjdns.md)
- [Files](files.md)
- [Fuzz-testing](fuzzing.md)
- [I2P Support](i2p.md)
- [Init Scripts (systemd/upstart/openrc)](init.md)
- [Managing Wallets](managing-wallets.md)
- [Multisig Tutorial](multisig-tutorial.md)
- [P2P bad ports definition and list](p2p-bad-ports.md)
- [PSBT support](psbt.md)
- [Reduce Memory](reduce-memory.md)
- [Reduce Traffic](reduce-traffic.md)
- [Tor Support](tor.md)
- [Transaction Relay Policy](policy/README.md)
- [ZMQ](zmq.md)

License
---------------------
Distributed under the [MIT software license](/COPYING).
