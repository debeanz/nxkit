#!/bin/bash
# Super Mario Maker 2, on the Mac before the app build (nxkit.py build runs patches/<title ID>/mac_build.sh <work dir>): the course server inside the
# app, smm2recomp's core/crates/embed (github.com/smm2recomp/smm2recomp, GPL-3.0), built for iOS as a static library. It serves TGRCode's
# public course archive at 127.0.0.1 for the Course World patch (0020, dm/host/hle_smm2_online.c). Prints the build switches for the app:
#   CFLAGS=...   (dm/host/Makefile DM_EXTRA_CFLAGS)
#   LDFLAGS=...  (dm/ios/tools/build_ipa.sh DM_EXTRA_LDFLAGS)
# Everything else goes to stderr. Fails (exit 1) when the library can't be built: nxkit then stops, as the app would have no course server.
set -euo pipefail
WORK=$1
REF=18a4e880027f1432d3fc208e50f451d71b647c1c   # smm2recomp commit the patch was made against (its server API and 3.0.3 findings)
SRC=$WORK/smm2recomp
{
  if [ ! -d "$SRC/.git" ]; then
    git clone -q --filter=blob:none https://github.com/smm2recomp/smm2recomp.git "$SRC"
  fi
  git -C "$SRC" checkout -q "$REF"
  if ! command -v rustup > /dev/null; then   # (GitHub's macOS images have rustup; this covers one without it)
    curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
  fi
  [ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
  cd "$SRC/core"
  rustup show active-toolchain || true   # (core/rust-toolchain.toml pins it; rustup installs it on first use)
  rustup target add aarch64-apple-ios
  CARGO_PROFILE_RELEASE_DEBUG=0 cargo build -p mmk-embed --release --target aarch64-apple-ios
} >&2
LIB=$SRC/core/target/aarch64-apple-ios/release/libmmk_embed.a
[ -f "$LIB" ] || { echo "no $LIB" >&2; exit 1; }
ls -l "$LIB" >&2
echo "CFLAGS=-DDM_SMM2_EMBED=1"
echo "LDFLAGS=$LIB -framework Security"
