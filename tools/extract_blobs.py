#!/usr/bin/env python3
"""Extract the LG V60 IMS/ePDG blobs needed by the build scripts.

Nothing proprietary is shipped in this repo; you provide:

  --lg-system-ext   an ext4 system_ext.img from LG V60 stock firmware
                    (unpack a KDZ with kdztools, or `dd` your own device's
                    system_ext partition).  Must be for the V60 ('timelm').
  --los-system-ext  a stock LineageOS system_ext.img for the build you run
                    (e.g. `unzip -o lineage-*.zip payload.bin; payload_dumper --partitions system_ext --out out/los payload.bin`).

Populates <out>/ (default: staging/) with:

  staging/paths.json           absolute paths of the two input images
  staging/lg-app/              application-layer blobs for the Magisk modules
  staging/native/              stroke + ipsecd placeholders (you patch these,
                               see docs/PATCH-RECIPES.md)
  staging/patched/             drop the 3 re-packed APKs here (see PATCH-RECIPES.md)

The substrate builder reads the LG binaries / libs / init.rc / /etc/ipsec /
SELinux policy straight from --lg-system-ext via debugfs, so those are not
copied out here.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# app-layer files pulled out of the LG system_ext for v60_ims_volte
LG_APP = {
    "priv-app/Ims6/Ims6.apk":                       "priv-app/Ims6/Ims6.apk",
    "priv-app/lgdataservice/lgdataservice.apk":     "priv-app/lgdataservice/lgdataservice.apk",
    "framework/com.lge.ims.httpTxn.jar":            "framework/com.lge.ims.httpTxn.jar",
    "framework/com.lge.jansky.jar":                 "framework/com.lge.jansky.jar",
    "framework/com.lge.wfcsupport.jar":             "framework/com.lge.wfcsupport.jar",
    "framework/lgdataservice-manager.jar":          "framework/lgdataservice-manager.jar",
    "framework/lgsvcitems.jar":                     "framework/lgsvcitems.jar",
    "lib64/libims.lge.so":                          "lib64/libims.lge.so",
    "lib64/libimsmmpf.lge.so":                      "lib64/libimsmmpf.lge.so",
    "lib64/libimswms.lge.so":                       "lib64/libimswms.lge.so",
    "lib64/libimscamerajni.lge.so":                 "lib64/libimscamerajni.lge.so",
    "lib64/libhidltransport.so":                    "lib64/libhidltransport.so",
    "lib64/libLgeProductFeatures2.so":              "lib64/libLgeProductFeatures2.so",
    "lib64/vendor.lge.hardware.soi@1.0.so":         "lib64/vendor.lge.hardware.soi@1.0.so",
    "lib64/vendor.lge.hardware.vss_ims@1.0.so":     "lib64/vendor.lge.hardware.vss_ims@1.0.so",
    "etc/permissions/privapp-permissions-v60-lg-ims.xml":         "etc/permissions/privapp-permissions-v60-lg-ims.xml",
    "etc/permissions/privapp-permissions-v60-lg-data-service.xml":"etc/permissions/privapp-permissions-v60-lg-data-service.xml",
    "etc/permissions/com.lge.ims.httpTxn.xml":      "etc/permissions/com.lge.ims.httpTxn.xml",
    "etc/permissions/com.lge.jansky.xml":           "etc/permissions/com.lge.jansky.xml",
    "etc/permissions/com.lge.wfcsupport.xml":       "etc/permissions/com.lge.wfcsupport.xml",
    "etc/permissions/lgdataservice-manager.xml":    "etc/permissions/lgdataservice-manager.xml",
    "etc/permissions/lgsvcitems.xml":               "etc/permissions/lgsvcitems.xml",
    "etc/selinux/plat_mac_permissions.xml":         "etc/selinux/plat_mac_permissions.xml",
    "etc/ike_conf.xml":                             "etc/ike_conf.xml",
    "etc/mapcon_conf.xml":                          "etc/mapcon_conf.xml",
}

# files the substrate builder expects to find in --lg-system-ext (sanity check only)
LG_SUBSTRATE_REQUIRED = [
    "bin/lge_ims_phone_provider", "bin/imsipsecclient", "bin/imsipsecstarter",
    "bin/ipsecd", "bin/starter", "bin/charon", "bin/ipsec",
    "lib64/libcharon.so", "lib64/libstrongswan.so", "lib64/libsimaka.so",
    "etc/init/init.lge.iwlan.rc", "etc/init/init.lge.ims.rc",
    "etc/init/lge_ims_phone_provider.rc",
    "etc/selinux/system_ext_sepolicy.cil",
    "etc/selinux/system_ext_property_contexts",
    "etc/selinux/system_ext_service_contexts",
    "etc/selinux/system_ext_file_contexts",
]


def dfs(img: Path, cmd: str) -> str:
    r = subprocess.run(["debugfs", "-R", cmd, "--", str(img)],
                       capture_output=True, text=True)
    return r.stdout + r.stderr


def exists(img: Path, path: str) -> bool:
    return "File not found" not in dfs(img, f"stat /{path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lg-system-ext", required=True, type=Path)
    ap.add_argument("--los-system-ext", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=Path("staging"))
    a = ap.parse_args()

    for p in (a.lg_system_ext, a.los_system_ext):
        if not p.is_file():
            sys.exit(f"not a file: {p}")

    # sanity-check the LG image
    missing = [f for f in LG_SUBSTRATE_REQUIRED if not exists(a.lg_system_ext, f)]
    if missing:
        sys.exit("this system_ext.img is missing LG IMS content (wrong firmware "
                 "or wrong partition?):\n  " + "\n  ".join(missing))

    out = a.out
    (out / "lg-app").mkdir(parents=True, exist_ok=True)
    (out / "lg-src").mkdir(parents=True, exist_ok=True)
    (out / "native").mkdir(parents=True, exist_ok=True)
    (out / "patched").mkdir(parents=True, exist_ok=True)

    # stock ipsecd, as the input to tools/patch_ipsecd.py (PATCH-RECIPES.md 5)
    dfs(a.lg_system_ext, f"dump -p /bin/ipsecd {out/'lg-src'/'ipsecd'}")

    (out / "paths.json").write_text(json.dumps({
        "lg_system_ext": str(a.lg_system_ext.resolve()),
        "los_system_ext": str(a.los_system_ext.resolve()),
    }, indent=2))

    n = 0
    for src, dst in LG_APP.items():
        if not exists(a.lg_system_ext, src):
            print(f"  skip (absent): {src}")
            continue
        d = out / "lg-app" / dst
        d.parent.mkdir(parents=True, exist_ok=True)
        dfs(a.lg_system_ext, f"dump -p /{src} {d}")
        if not d.exists() or d.stat().st_size == 0:
            sys.exit(f"failed to extract {src}")
        n += 1
    print(f"extracted {n} app-layer files -> {out/'lg-app'}")

    (out / "native" / "README.txt").write_text(
        "Place the two patched native binaries here:\n"
        "  native/stroke   - strongSwan client, ABI patch + NDK build (PATCH-RECIPES.md 4)\n"
        "  native/ipsecd   - 2 byte patches; run: python3 tools/patch_ipsecd.py "
        "<stock ipsecd> native/ipsecd   (PATCH-RECIPES.md 5)\n")
    (out / "patched" / "README.txt").write_text(
        "Place here:\n"
        "  patched/Ims6.apk                     - bytecode patch (PATCH-RECIPES.md 1)\n"
        "  patched/lgdataservice.apk            - add stub class (PATCH-RECIPES.md 2)\n"
        "  patched/QualifiedNetworksService.apk - com.android.qns, 3 edits (PATCH-RECIPES.md 3)\n"
        "  patched/Iwlan.apk                    - com.google.android.iwlan, UNMODIFIED, just\n"
        "                                        obtained from an AOSP/Android 16 build (3)\n"
        "The privapp allowlist is NOT staged: tools/privapp-permissions-v60-aosp-iwlan.xml is\n"
        "shipped and the build script adds MODIFY_PHONE_STATE to it.\n")

    print(f"wrote {out/'paths.json'}")
    print("next: reproduce the patched APKs (docs/PATCH-RECIPES.md), then run the build scripts.")


if __name__ == "__main__":
    main()
