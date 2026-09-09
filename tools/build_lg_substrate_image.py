#!/usr/bin/env python3
"""Build the LG IMS/ePDG `system_ext` substrate image.

Grafts, onto a stock LineageOS system_ext.img, the LG pieces that `init` and
`secilc` consume before Magisk loads (so they cannot be a Magisk module):

  * SELinux policy   : the LG-only delta of system_ext_sepolicy.cil and
                       system_ext_{file,service,property}_contexts, *appended* to
                       whatever the target ROM already ships (not overwritten), so
                       a LineageOS derivative keeps its own system_ext types/rules.
                       The delta is `donor_policy - tools/sepolicy-baseline/*`
                       (stock LineageOS); plus a small `allow ipsecd` CIL block.
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
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# stock LineageOS system_ext policy the LG delta is measured against; see
# tools/sepolicy-baseline/README.md.  Must match the LineageOS version the donor
# image was cut from.
BASELINE = Path(__file__).resolve().parent / "sepolicy-baseline"

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
# policy files merged by appending the LG delta (donor - baseline) onto the
# target's own copy; see merge_policy().
POL = [
    "/etc/selinux/system_ext_sepolicy.cil",
    "/etc/selinux/system_ext_file_contexts",
    "/etc/selinux/system_ext_service_contexts",
    "/etc/selinux/system_ext_property_contexts",
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


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def merge_policy(out: Path, donor: Path, path: str, td: Path) -> None:
    """Append the LG-only delta (donor policy minus tools/sepolicy-baseline) onto
    the target's own copy of a system_ext policy file, rather than overwriting it.
    Keeps a LineageOS derivative's own system_ext types/rules/labels intact."""
    name = path.rsplit("/", 1)[1]
    base_f = BASELINE / name
    if not base_f.is_file():
        raise SystemExit(f"missing baseline: {base_f}  (see tools/sepolicy-baseline/README.md)")
    is_cil = name.endswith(".cil")
    marker = f"; --- lg-substrate: {name} delta (append) ---"
    k = path.replace("/", "_")
    tf, dfp = td / f"t{k}", td / f"d{k}"
    dfs(out, f"dump -p {path} {tf}")
    dfs(donor, f"dump -p {path} {dfp}")
    if not dfp.exists() or dfp.stat().st_size == 0:
        raise SystemExit(f"donor missing {path}")

    if not tf.exists() or tf.stat().st_size == 0:
        # target ships no such file at all -> graft the donor's whole file
        graft(out, donor, path, "0100644", td)
        return

    tgt = tf.read_text(errors="replace").splitlines()
    if any(marker in ln for ln in tgt):
        print(f"  {name}: already merged")
        return
    base_keys = {_norm(ln) for ln in base_f.read_text().splitlines() if _norm(ln)}
    tgt_keys = {_norm(ln) for ln in tgt if _norm(ln)}
    tgt_types = set()
    if is_cil:
        for ln in tgt:
            m = re.match(r"\((?:type|typealias) (\S+)\)", ln.strip())
            if m:
                tgt_types.add(m.group(1))

    delta: list[str] = []
    for ln in dfp.read_text(errors="replace").splitlines():
        s = ln.strip()
        key = _norm(ln)
        if not key or s.startswith(";"):            # blank / comment / ;;* line-marker
            continue
        if key in base_keys or key in tgt_keys:     # already in stock LOS or the target
            continue
        if is_cil:
            m = re.match(r"\((?:type|typealias) (\S+)\)", s)
            if m and m.group(1) in tgt_types:       # type already declared in target
                continue
        delta.append(ln.rstrip())

    if not delta:
        print(f"  {name}: nothing to append")
        return

    x = td / f"x{k}"
    dfs(out, f"ea_get -f {x} {path} security.selinux")     # preserve target's own label
    body = tf.read_bytes()
    if not body.endswith(b"\n"):
        body += b"\n"
    body += ("\n" + marker + "\n" + "\n".join(delta) + "\n").encode()
    nf = td / f"n{k}"
    nf.write_bytes(body)
    dfs(out, f"rm {path}", w=True)
    dfs(out, f"write {nf} {path}", w=True)
    for f in ("mode 0100644", "uid 0", "gid 0"):
        dfs(out, f"set_inode_field {path} {f}", w=True)
    if x.exists() and x.stat().st_size:
        dfs(out, f"ea_set -f {x} {path} security.selinux", w=True)
    v = td / f"v{k}"
    dfs(out, f"dump -p {path} {v}")
    if v.read_bytes() != body:
        raise SystemExit(f"merge mismatch: {path}")
    print(f"  {name}: +{len(delta)} LG statements")


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
        for path in POL:
            print("merge", path)
            merge_policy(a.out, lg, path, td)
        print("append CIL: ipsecd interface-plumbing grants")
        append_cil(a.out, td)

    r = subprocess.run(["e2fsck", "-fn", str(a.out)], capture_output=True, text=True)
    print((r.stdout.strip().splitlines() or ["(e2fsck clean)"])[-1])
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
