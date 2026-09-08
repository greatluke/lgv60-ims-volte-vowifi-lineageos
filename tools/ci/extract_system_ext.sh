#!/usr/bin/env bash
# Pull system_ext.img out of a flashable ROM zip.
#   $1 = rom zip     $2 = output system_ext.img path
# Handles: A/B OTA (payload.bin), plain image zips, sparse images.
set -euo pipefail
ZIP=$1
OUT=$2
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

names=$(unzip -Z1 "$ZIP")

if grep -qx 'payload.bin' <<<"$names"; then
    echo "== A/B OTA: extracting system_ext from payload.bin =="
    unzip -o "$ZIP" payload.bin -d "$WORK" >/dev/null
    python3 -m payload_dumper --partitions system_ext --out "$WORK/pd" "$WORK/payload.bin" >/dev/null
    src="$WORK/pd/system_ext.img"
else
    cand=$(grep -iE '(^|/)system_ext\.img(\.[a-z0-9]+)?$' <<<"$names" | head -n1 || true)
    [ -n "$cand" ] || { echo "no payload.bin and no system_ext.img in the zip" >&2; exit 2; }
    echo "== image zip: $cand =="
    unzip -o "$ZIP" "$cand" -d "$WORK" >/dev/null
    src="$WORK/$cand"
    case "$cand" in
        *.br)  brotli -d "$src" -o "${src%.br}"; src="${src%.br}" ;;
    esac
fi

# de-sparse if needed
if [ "$(head -c4 "$src" | xxd -p)" = "3aff26ed" ]; then
    echo "== simg2img =="
    simg2img "$src" "$OUT"
else
    cp "$src" "$OUT"
fi

# must be ext4 and must NOT already carry the LG IMS graft
file "$OUT" | grep -q 'ext[234] filesystem' || { echo "not an ext filesystem" >&2; exit 3; }
if ! debugfs -R "stat /bin/ipsecd" -- "$OUT" 2>&1 | grep -q "File not found"; then
    echo "this system_ext already contains LG IMS content (/bin/ipsecd present) -- nothing to do" >&2
    exit 4
fi
echo "== ok: $(du -h "$OUT" | cut -f1) stock system_ext, no LG graft =="
