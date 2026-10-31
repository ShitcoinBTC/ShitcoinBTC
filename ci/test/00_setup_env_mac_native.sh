#!/usr/bin/env bash
#
# Copyright (c) 2019-present The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

export LC_ALL=C.UTF-8

export HOST=x86_64-apple-darwin
export PIP_PACKAGES="zmq"
export GOAL="install"
export SYSCOIN_CONFIG="--with-gui --with-miniupnpc --with-natpmp --enable-reduce-exports"
export TEST_RUNNER_EXTRA="--exclude interface_zmq_nevm"
export CI_OS_NAME="macos"
export NO_DEPENDS=1
export OSX_SDK=""
export CCACHE_MAXSIZE=400M
# Homebrew's prefix is /opt/homebrew on ARM64 runners but /usr/local on
# Intel (x86_64) runners such as macos-15-intel, which this job now uses.
# Hardcoding /opt/homebrew pointed the compiler/linker at directories
# that do not exist on Intel. Ask brew for the real prefix.
if command -v brew >/dev/null 2>&1; then
  BREW_PREFIX="$(brew --prefix)"
else
  BREW_PREFIX="/opt/homebrew"
fi
export CPATH="${BREW_PREFIX}/include"
export LDFLAGS="-L${BREW_PREFIX}/lib -L${BREW_PREFIX}/opt $LDFLAGS"
# Set PKG_CONFIG_PATH explicitly, with the exact brew sqlite3/qt@5
# entries configure.ac would add, so the value is identical when the
# main configure starts and when it invokes the dashbls sub-configure
# with the shared config.cache. Otherwise the sub-configure aborts:
# "'PKG_CONFIG_PATH' was not set in the previous run" (autoconf
# precious-variable cache check). configure.ac now also only prepends
# these entries when they are missing, keeping the value stable.
if command -v brew >/dev/null 2>&1; then
  export PKG_CONFIG_PATH="$(brew --prefix qt@5 2>/dev/null)/lib/pkgconfig:$(brew --prefix sqlite3 2>/dev/null)/lib/pkgconfig:${BREW_PREFIX}/lib/pkgconfig:${BREW_PREFIX}/share/pkgconfig:$PKG_CONFIG_PATH"
fi
export RUN_FUZZ_TESTS=true
export FUZZ_TESTS_CONFIG="--exclude banman"  # https://github.com/bitcoin/bitcoin/issues/27924
