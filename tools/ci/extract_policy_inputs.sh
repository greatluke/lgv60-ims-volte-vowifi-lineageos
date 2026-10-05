#!/usr/bin/env bash
# Pull the SELinux policy inputs init compiles at boot (plat, vendor, product,
# odm) out of a flashable ROM zip, so validate_substrate.py can run the exact
# boot-time secilc compile against the built substrate.
#   $1 = rom zip     $2 = output dir  (gets system/ vendor/ product/ odm/,
#                                      each holding that partition's etc/selinux)
# Best effort: a partition that can't be found is skipped, and the validator
# then skips the compile check instead of failing the build.
set -euo pipefail
ZIP=$1
OUT=$2
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"
PARTS=(system vendor product odm)

names=$(unzip -Z1 "$ZIP")
if grep -qx 'payload.bin' <<<"$names"; then
    unzip -o "$ZIP" payload.bin -d "$WORK" >/dev/null
    payload_dumper --partitions "$(IFS=,; echo "${PARTS[*]}")" --out "$WORK/pd" "$WORK/payload.bin" >/dev/null || true
    rm -f "$WORK/payload.bin"
else
    for p in "${PARTS[@]}"; do
        cand=$(grep -iE "(^|/)$p\.img(\.br)?$" <<<"$names" | head -n1 || true)
        [ -n "$cand" ] || continue
        unzip -o "$ZIP" "$cand" -d "$WORK/raw" >/dev/null
        src="$WORK/raw/$cand"
        case "$cand" in *.br) brotli -d "$src" -o "${src%.br}"; src="${src%.br}" ;; esac
        mkdir -p "$WORK/pd"
        if [ "$(head -c4 "$src" | xxd -p)" = "3aff26ed" ]; then simg2img "$src" "$WORK/pd/$p.img"; else mv "$src" "$WORK/pd/$p.img"; fi
    done
fi

for p in "${PARTS[@]}"; do
    img="$WORK/pd/$p.img"
    [ -s "$img" ] || { echo "-- $p: not in the zip, skipped"; continue; }
    rm -rf "${OUT:?}/$p"
    # system is system-as-root (/system/etc/selinux); the rest mount at their root
    if [ "$(dd if="$img" bs=1 skip=1024 count=4 2>/dev/null | xxd -p)" = "e2e1f5e0" ]; then
        # EROFS (LineageOS ships vendor/odm this way)
        fsck.erofs --extract="$WORK/x-$p" "$img" >/dev/null 2>&1 || true
        for d in system/etc/selinux etc/selinux; do
            [ -d "$WORK/x-$p/$d" ] && { cp -r "$WORK/x-$p/$d" "$OUT/$p"; break; }
        done
        rm -rf "$WORK/x-$p"
    else
        for d in /system/etc/selinux /etc/selinux; do
            if debugfs -R "stat $d" -- "$img" 2>&1 | grep -q "Type: directory"; then
                debugfs -R "rdump $d $WORK" -- "$img" >/dev/null 2>&1
                mv "$WORK/selinux" "$OUT/$p"
                break
            fi
        done
    fi
    if [ -d "$OUT/$p" ]; then echo "-- $p: $(find "$OUT/$p" -type f | wc -l) policy files"
    else echo "-- $p: no etc/selinux found (unreadable filesystem?), skipped"; fi
    rm -f "$img"
done
