#!/usr/bin/env bash
# Build a static secilc from AOSP's external/selinux (not upstream: Android
# policy uses AOSP-only policy capabilities such as functionfs_seclabel).
#   $1 = AOSP tag (e.g. android-16.0.0_r4)    $2 = output dir (gets ./secilc)
set -euo pipefail
TAG=$1
OUT=$2
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"
# googlesource throttles CI runners (503s), the +archive tarball endpoint
# especially; a shallow clone with retries is more reliable.
for i in 1 2 3 4 5; do
    git -c advice.detachedHead=false clone -q --depth 1 -b "$TAG" \
        https://android.googlesource.com/platform/external/selinux "$WORK/sel" && break
    rm -rf "$WORK/sel"; echo "clone attempt $i failed, retrying" >&2; sleep $((i * 20))
done
test -d "$WORK/sel/libsepol"
# newer gcc trips AOSP's -Werror on an unused function; build without it
make -C "$WORK/sel/libsepol" -j"$(nproc)" CFLAGS="-O2 -fno-semantic-interposition" >/dev/null
make -C "$WORK/sel/libsepol" install DESTDIR="$WORK/root" PREFIX=/usr >/dev/null
rm -f "$WORK"/root/usr/lib/libsepol.so*      # link secilc statically
make -C "$WORK/sel/secilc" secilc CFLAGS="-O2 -I$WORK/root/usr/include" LDFLAGS="-L$WORK/root/usr/lib" >/dev/null
cp "$WORK/sel/secilc/secilc" "$OUT/"
echo "secilc ($TAG) -> $OUT/secilc"
