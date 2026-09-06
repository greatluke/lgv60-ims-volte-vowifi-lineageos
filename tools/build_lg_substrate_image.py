#!/usr/bin/env python3
"""Build the LG IMS/ePDG `system_ext` substrate image.

Grafts, onto a stock LineageOS system_ext.img, the LG pieces that `init` and
`secilc` consume before Magisk loads (so they cannot be a Magisk module):

  * SELinux policy   : system_ext_sepolicy.cil (+ a small `allow ipsecd` append),
                       system_ext_{file,service,property}_contexts
  * init services    : init.lge.iwlan.rc, init.lge.ims.rc, lge_ims_phone_provider.rc
  * native daemons   : lge_ims_phone_provider, imsipsecclient, imsipsecstarter,
                       ipsecd (stock; the module overlays the HAL-patched one),
                       starter, charon, ipsec
  * libs             : libcharon, libstrongswan, libsimaka, LGDataFeature,
                       libLgeProductProperties, vendor.lge.hardware.property@2.0,
                       libpatchcodeid
  * /etc/ipsec/      : strongswan.conf, ipsec.conf, updown_script, CA set

Inputs come from staging/paths.json written by extract_blobs.py.
Removing system_ext_sepolicy_and_mapping.sha256 forces secilc to recompile the
merged CIL at boot (otherwise the loader validates the stock precompiled policy
and the LG CIL is silently ignored).

Usage:  python3 build_lg_substrate_image.py --staging staging/ --out out/lg_substrate.img
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

BIN = [
    ("/bin/charon", "0100755"), ("/bin/imsipsecclient", "0100755"),
    ("/bin/imsipsecstarter", "0100755"), ("/bin/ipsec", "0100755"),
    ("/bin/ipsecd", "0100755"), ("/bin/lge_ims_phone_provider", "0100755"),
    ("/bin/starter", "0100755"),
    ("/lib64/LGDataFeature.so", "0100644"),
    ("/lib64/libLgeProductProperties.so", "0100644"),
    ("/lib64/libcharon.so", "0100644"), ("/lib64/libpatchcodeid.so", "0100644"),
    ("/lib64/libsimaka.so", "0100644"), ("/lib64/libstrongswan.so", "0100644"),
    ("/lib64/vendor.lge.hardware.property@2.0.so", "0100644"),
    ("/etc/init/init.lge.iwlan.rc", "0100644"),
    ("/etc/init/init.lge.ims.rc", "0100644"),
    ("/etc/init/lge_ims_phone_provider.rc", "0100644"),
    ("/etc/ipsec/ipsec.conf", "0100644"), ("/etc/ipsec/strongswan.conf", "0100644"),
    ("/etc/ipsec/updown_script", "0100755"),
]
IPSEC_D = "/etc/ipsec/ipsec.d"
POL = [
    ("/etc/selinux/system_ext_sepolicy.cil", "0100644"),
    ("/etc/selinux/system_ext_file_contexts", "0100644"),
    ("/etc/selinux/system_ext_service_contexts", "0100644"),
    ("/etc/selinux/system_ext_property_contexts", "0100644"),
]

# charon runs in the ipsecd domain when ipsecd auto-spawns it; these interface-
# plumbing grants are not in the LG closure and are plain `(allow ...)` (verified
# to compile).  The MLS-attribute grant that DOES break secilc is applied by the
# module via `magiskpolicy --live` instead.
CIL_APPEND = """
; --- lg-substrate: ipsecd interface-plumbing grants ---
(allow ipsecd lge_ims_wo_prop (file (read getattr map open)))
(allow ipsecd lge_ims_data_prop (file (read getattr map open)))
(allow ipsecd vendor_default_prop (file (read getattr map open)))
(allow ipsecd vendor_lge_misc_prop (file (read getattr map open)))
(allow ipsecd system_prop (property_service (set)))
(allow ipsecd hwservicemanager_prop (file (read getattr map open)))
(allow ipsecd hwservicemanager (binder (call transfer)))
(allow ipsecd servicemanager (binder (call transfer)))
(allow ipsecd default_android_hwservice (hwservice_manager (find)))
(allow ipsecd ipsecd_socket (unix_stream_socket (connectto)))
"""


def dfs(img: Path, cmd: str, w: bool = False) -> str:
    a = ["debugfs"] + (["-w"] if w else []) + ["-R", cmd, "--", str(img)]
    r = subprocess.run(a, capture_output=True, text=True)
    return r.stdout + r.stderr


def graft(out: Path, donor: Path, path: str, mode: str, td: Path) -> None:
    k = path.replace("/", "_")
    c, x = td / f"c{k}", td / f"x{k}"
    dfs(donor, f"dump -p {path} {c}")
    if (not c.exists() or c.stat().st_size == 0) and ("/bin/" in path or "/lib64/" in path):
        raise SystemExit(f"donor missing {path}")
    dfs(donor, f"ea_get -f {x} {path} security.selinux")
    dfs(out, f"rm {path}", w=True)
    dfs(out, f"write {c} {path}", w=True)
    for f in (f"mode {mode}", "uid 0", "gid 0"):
        dfs(out, f"set_inode_field {path} {f}", w=True)
    if x.exists() and x.stat().st_size:
        dfs(out, f"ea_set -f {x} {path} security.selinux", w=True)
    v = td / f"v{k}"
    dfs(out, f"dump -p {path} {v}")
    if v.read_bytes() != c.read_bytes():
        raise SystemExit(f"content mismatch: {path}")


def mkdir_like(out: Path, donor: Path, path: str, td: Path) -> None:
    if "File not found" not in dfs(out, f"stat {path}"):
        return
    dfs(out, f"mkdir {path}", w=True)
    for f in ("mode 040755", "uid 0", "gid 0"):
        dfs(out, f"set_inode_field {path} {f}", w=True)
    x = td / ("d" + path.replace("/", "_"))
    dfs(donor, f"ea_get -f {x} {path} security.selinux")
    if x.exists() and x.stat().st_size:
        dfs(out, f"ea_set -f {x} {path} security.selinux", w=True)


def list_dir(img: Path, path: str) -> list[str]:
    out = dfs(img, f"ls -p {path}")
    names = []
    for rec in out.replace("\n", "").split("/"):
        rec = rec.strip()
        # -p format: /ino/mode/uid/gid/name/size/  -> names are the 5th field of each 7-tuple
    # simpler: parse `ls -l`
    for ln in dfs(img, f"ls -l {path}").splitlines():
        p = ln.split()
        if len(p) >= 9 and p[-1] not in (".", ".."):
            names.append(p[-1])
    return names


def append_cil(out: Path, td: Path) -> None:
    p = "/etc/selinux/system_ext_sepolicy.cil"
    before, after, x = td / "cb", td / "ca", td / "cx"
    dfs(out, f"dump -p {p} {before}")
    data = before.read_bytes()
    if b"lg-substrate: ipsecd interface-plumbing" in data:
        return
    dfs(out, f"ea_get -f {x} {p} security.selinux")
    after.write_bytes(data + CIL_APPEND.encode())
    dfs(out, f"rm {p}", w=True)
    dfs(out, f"write {after} {p}", w=True)
    for f in ("mode 0100644", "uid 0", "gid 0"):
        dfs(out, f"set_inode_field {p} {f}", w=True)
    if x.stat().st_size:
        dfs(out, f"ea_set -f {x} {p} security.selinux", w=True)
    chk = td / "cc"
    dfs(out, f"dump -p {p} {chk}")
    if chk.read_bytes() != after.read_bytes():
        raise SystemExit("CIL append mismatch")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging", type=Path, default=Path("staging"))
    ap.add_argument("--out", type=Path, default=Path("out/lg_substrate.img"))
    a = ap.parse_args()

    paths = json.loads((a.staging / "paths.json").read_text())
    lg, los = Path(paths["lg_system_ext"]), Path(paths["los_system_ext"])
    for p in (lg, los):
        if not p.is_file():
            raise SystemExit(f"missing input: {p}  (re-run extract_blobs.py)")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    print(f"base: {los}")
    shutil.copy(los, a.out)
    subprocess.run(["e2fsck", "-fy", str(a.out)], capture_output=True)

    dfs(a.out, "rm /etc/selinux/system_ext_sepolicy_and_mapping.sha256", w=True)
    print("removed …_and_mapping.sha256 (forces secilc recompile)")

    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        mkdir_like(a.out, lg, "/etc/ipsec", td)
        mkdir_like(a.out, lg, IPSEC_D, td)
        for path, mode in BIN:
            print("graft", path)
            graft(a.out, lg, path, mode, td)
        for cert in list_dir(lg, IPSEC_D):
            print("graft", f"{IPSEC_D}/{cert}")
            graft(a.out, lg, f"{IPSEC_D}/{cert}", "0100644", td)
        for path, mode in POL:
            print("graft", path)
            graft(a.out, lg, path, mode, td)
        print("append CIL: ipsecd interface-plumbing grants")
        append_cil(a.out, td)

    r = subprocess.run(["e2fsck", "-fn", str(a.out)], capture_output=True, text=True)
    print((r.stdout.strip().splitlines() or ["(e2fsck clean)"])[-1])
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
