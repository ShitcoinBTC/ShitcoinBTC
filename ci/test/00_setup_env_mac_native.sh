#!/usr/bin/env bash
#
# Copyright (c) 2019-present The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

export LC_ALL=C.UTF-8

export HOST=aarch64-apple-darwin
export PIP_PACKAGES="zmq"
export GOAL="install"
export SYSCOIN_CONFIG="--with-gui --with-miniupnpc --with-natpmp --enable-reduce-exports"
export TEST_RUNNER_EXTRA="--exclude interface_zmq_nevm"
export CI_OS_NAME="macos"
export NO_DEPENDS=1
export OSX_SDK=""
export CCACHE_MAXSIZE=400M
export CPATH="/opt/homebrew/include"
export LDFLAGS="-L/opt/homebrew/lib -L/opt/homebrew/opt $LDFLAGS"
# Set PKG_CONFIG_PATH explicitly so the dashbls sub-configure sees a consistent
# environment across the two configure invocations in 06_script_a.sh (the main
# configure.ac sets it dynamically via brew, which trips the sub-configure's
# cache validation with "PKG_CONFIG_PATH was not set in the previous run").
export PKG_CONFIG_PATH="/opt/homebrew/lib/pkgconfig:/opt/homebrew/share/pkgconfig:/opt/homebrew/opt/qt@5/lib/pkgconfig:/opt/homebrew/opt/sqlite3/lib/pkgconfig:$PKG_CONFIG_PATH"
export RUN_FUZZ_TESTS=true
export FUZZ_TESTS_CONFIG="--exclude banman"  # https://github.com/bitcoin/bitcoin/issues/27924
