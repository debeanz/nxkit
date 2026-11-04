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
# The workflow's build cache (NXKIT_CACHE, kept between builds of this game): the library built for this commit, used as it is (open-source code only, no game data)
CACHED=${NXKIT_CACHE:+$NXKIT_CACHE/libmmk_embed-$REF.a}
if [ -n "$CACHED" ] && [ -f "$CACHED" ]; then
  echo "course server library from the build cache: $CACHED" >&2
  ls -l "$CACHED" >&2
  echo "CFLAGS=-DDM_SMM2_EMBED=1"
  echo "LDFLAGS=$CACHED -framework Security"
  echo "ENV IOS_PULL=1"
  exit 0
fi
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
if [ -n "$CACHED" ]; then   # (kept for the next build; older commits' copies go)
  rm -f "$NXKIT_CACHE"/libmmk_embed-*.a
  cp "$LIB" "$CACHED" && LIB=$CACHED
fi
echo "CFLAGS=-DDM_SMM2_EMBED=1"
echo "LDFLAGS=$LIB -framework Security"
# The pulling vertex libraries (<key>_bp) go into the app although the profile's pso_mtl4 is off: the launcher's Metal 4 switch then gets hybrid pipelines (a draw
# whose pipeline is still compiling draws through a prebuilt one instead of waiting: the shader-compile stutters). build_ipa.sh left them out with pso_mtl4 off
echo "ENV IOS_PULL=1"
