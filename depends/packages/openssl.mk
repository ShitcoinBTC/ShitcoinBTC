package=openssl
$(package)_version=3.0.16
$(package)_download_path=https://www.openssl.org/source/
$(package)_file_name=$(package)-$($(package)_version).tar.gz
$(package)_sha256_hash=57e03c50feab5d31b152af2b764f10379aecd8ee92f16c985983ce4a99f7ef86

# OpenSSL's Configure wants an explicit target and cross-compile prefix.
# We only need the static libssl/libcrypto for the vault BTC-explorer TLS
# client; skip shared libs, tests and docs to keep the build lean.
define $(package)_set_vars
  $(package)_config_opts=--prefix=$($($(package)_type)_prefix)
  $(package)_config_opts+=--openssldir=$($($(package)_type)_prefix)/etc/ssl
  $(package)_config_opts+=--libdir=lib
  $(package)_config_opts+=no-shared no-tests no-capieng
endef

# Configure target per host OS/arch. mingw and linux pass
# --cross-compile-prefix so Configure picks up the depends cross toolchain
# (<triplet>-gcc). darwin cannot use a prefix: the SDK-aware compiler is
# `clang --target=<triplet> -isysroot...`, which a prefix cannot express
# (and Configure would prepend the prefix to CC= anyway), so pass it and
# the binutils explicitly as CC=/AR=/RANLIB= assignments instead.
# These names MUST be uppercase: OpenSSL's Configure only recognises
# uppercase make-variable assignments on its command line (its %user
# table is keyed CC/AR/RANLIB/...). Lowercase cc=/ar=/ranlib= are
# silently ignored, leaving the target template's default CC="cc" —
# the build machine's native compiler — which compiles x86_64 inline
# asm (crypto/bn/asm/x86_64-gcc.c) for the wrong target and fails with
# "invalid output constraint '=a' in asm" on x86_64-apple-darwin.
$(package)_config_opts_mingw32=--cross-compile-prefix=$(host)-
$(package)_config_opts_linux=--cross-compile-prefix=$(host)-
$(package)_config_opts_darwin=CC="$(host_CC)" AR="$(host_AR)" RANLIB="$(host_RANLIB)"
# The darwin linker flags from hosts/darwin.mk must reach Configure too:
# without them OpenSSL's links (e.g. providers/legacy.dylib, apps/openssl)
# invoke the build machine's GNU ld, which rejects the Mach-O link with
# "ld: unrecognised emulation mode: llvm". Configure routes bare -f/-m
# options into its added CFLAGS (also present on its link lines) and
# -Wl, options into its added LDFLAGS. Use joined = forms only: a bare
# "-mmacos-version-min 11.0" would make Configure treat "11.0" as a
# second target and abort.
$(package)_config_opts_darwin+=-fuse-ld=lld -mlinker-version=$(LLD_VERSION) -mmacos-version-min=$(OSX_MIN_VERSION)
$(package)_config_opts_darwin+=-Wl,-platform_version,macos,$(OSX_MIN_VERSION),$(OSX_SDK_VERSION) -Wl,-no_adhoc_codesign

$(package)_target_mingw32_x86_64=mingw64
$(package)_target_linux_x86_64=linux-x86_64
$(package)_target_linux_aarch64=linux-aarch64
$(package)_target_linux_arm=linux-armv4
$(package)_target_linux_riscv64=linux-riscv64
$(package)_target_linux_powerpc64=linux-ppc64
$(package)_target_linux_powerpc64le=linux-ppc64le
$(package)_target_darwin_x86_64=darwin64-x86_64-cc
$(package)_target_darwin_aarch64=darwin64-arm64-cc
$(package)_target_darwin_arm64=darwin64-arm64-cc

define $(package)_config_cmds
  ./Configure $(openssl_target_$(host_os)_$(host_arch)) $(openssl_config_opts)
endef

define $(package)_build_cmds
  $(MAKE)
endef

define $(package)_stage_cmds
  $(MAKE) DESTDIR=$($(package)_staging_dir) install_sw
endef
