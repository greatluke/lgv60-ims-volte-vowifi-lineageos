#!/usr/bin/env python3
"""Static boot checks for a built lg_substrate.img, run before it's published.

CI can't test-boot a substrate, but the ways a system_ext graft bootloops are
mostly checkable offline. Each check mirrors the code that consumes the file
at boot:

  1. *_contexts syntax (libselinux / init parsers). Only '#' is a comment in
     these files; any other non-blank line is an entry. A malformed entry in
     system_ext_file_contexts makes the whole file-contexts handle fail to load,
     init labels nothing, servicemanager (critical) crash-loops, and init
     reboots to the bootloader. Lines already present in the stock image are
     not judged (they're the ROM's, and it boots).
  2. property_contexts trie conflicts. init builds one trie from plat +
     system_ext + vendor + product + odm; any name defined twice fails it, and
     init LOG(FATAL)s "Failed to load serialized property info file".
  3. The policy hash is removed, so init really recompiles (otherwise it boots
     the stock precompiled policy and the LG CIL is silently ignored).
  4. The exact secilc compile init runs (system/core/init/selinux.cpp
     OpenSplitPolicy), stock vs. substrate. Skipped when the other partitions'
     policy (tools/ci/extract_policy_inputs.sh) or a secilc that can compile the
     stock policy isn't available: that's a toolchain gap, not a substrate bug.

Usage:
  validate_substrate.py --stock system_ext.img --substrate out/lg_substrate.img \\
      [--policy-dir policy/] [--secilc path/to/secilc]
Exit status 1 on any failed check.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SEL = "/etc/selinux"
CONTEXTS = ("system_ext_file_contexts", "system_ext_service_contexts", "system_ext_property_contexts")
SEPOLICY_VERSION = "30"          # system/core/init/Android.bp -DSEPOLICY_VERSION
DEFAULT_GENFS_VERSION = "202404" # system/sepolicy/compat/libgenfslabelsversion
CTX = re.compile(r"^(<<none>>|[^:\s]+:[^:\s]+:[^:\s]+:[^\s]+)$")
FILE_TYPES = {"--", "-b", "-c", "-d", "-p", "-l", "-s"}
PROP_TYPES = re.compile(r"^(string|bool|int|uint|double|size|enum)$")

failures: list[str] = []
warnings: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"FAIL  {msg}")


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f"warn  {msg}")


def dump(img: Path, path: str, dest: Path) -> bool:
    subprocess.run(["debugfs", "-R", f"dump -p {path} {dest}", "--", str(img)],
                   capture_output=True, text=True)
    return dest.exists() and dest.stat().st_size > 0


def exists(img: Path, path: str) -> bool:
    r = subprocess.run(["debugfs", "-R", f"stat {path}", "--", str(img)],
                       capture_output=True, text=True)
    return "File not found" not in (r.stdout + r.stderr)


def entries(text: str) -> list[tuple[int, str]]:
    out = []
    for n, ln in enumerate(text.splitlines(), 1):
        s = ln.strip()
        if s and not s.startswith("#"):
            out.append((n, s))
    return out


def bad_context_line(name: str, s: str) -> str | None:
    t = s.split()
    if s.startswith(";"):
        return "';' is not a comment in contexts files (only '#')"
    if name.endswith("file_contexts"):
        if len(t) not in (2, 3):
            return f"expected 'regex [type] context', got {len(t)} fields"
        if len(t) == 3 and t[1] not in FILE_TYPES:
            return f"invalid file type {t[1]!r}"
        if not CTX.match(t[-1]):
            return f"invalid context {t[-1]!r}"
    elif name.endswith("service_contexts"):
        if len(t) != 2:
            return f"expected 'name context', got {len(t)} fields"
        if not CTX.match(t[1]):
            return f"invalid context {t[1]!r}"
    elif name.endswith("property_contexts"):
        if len(t) < 2:
            return "missing context"
        if not CTX.match(t[1]):
            return f"invalid context {t[1]!r}"
        if len(t) >= 3 and t[2] not in ("exact", "prefix"):
            return f"third field must be 'exact' or 'prefix', got {t[2]!r}"
        if len(t) >= 4 and not PROP_TYPES.match(t[3]):
            return f"unknown property type {t[3]!r}"
    return None


def check_contexts(stock: Path, sub: Path) -> None:
    for name in CONTEXTS:
        a, b = stock / name, sub / name
        if not b.exists():
            continue
        stock_lines = {s for _, s in entries(a.read_text(errors="replace"))} if a.exists() else set()
        n_bad = 0
        for n, s in entries(b.read_text(errors="replace")):
            if s in stock_lines:
                continue
            why = bad_context_line(name, s)
            if why:
                n_bad += 1
                fail(f"{name}:{n}: {why}: {s[:90]}")
        if not n_bad:
            print(f"ok    {name}")


def property_entries(path: Path) -> list[tuple[str, bool]]:
    out = []
    for _, s in entries(path.read_text(errors="replace")):
        t = s.split()
        if len(t) >= 2 and CTX.match(t[1]) and (len(t) < 3 or t[2] in ("exact", "prefix")):
            out.append((t[0], len(t) >= 3 and t[2] == "exact"))
    return out


def check_properties(sub: Path, pol: Path | None) -> None:
    files = [("system_ext", sub / "system_ext_property_contexts")]
    if pol:
        files += [("plat", pol / "system/plat_property_contexts"),
                  ("vendor", pol / "vendor/vendor_property_contexts"),
                  ("product", pol / "product/product_property_contexts"),
                  ("odm", pol / "odm/odm_property_contexts")]
    seen: dict[tuple[str, bool], str] = {}
    dup = 0
    for part, f in files:
        if not f.exists():
            continue
        for key in property_entries(f):
            if key in seen:
                dup += 1
                fail(f"property '{key[0]}' ({'exact' if key[1] else 'prefix'}) defined in both "
                     f"{seen[key]} and {part}: init's property trie build fails")
            else:
                seen[key] = part
    if not dup:
        print(f"ok    property trie ({'all partitions' if pol else 'system_ext only'})")


def secilc_args(pol: Path, se: Path, out: Path) -> list[str] | None:
    sysp, ven, prod, odm = pol / "system", pol / "vendor", pol / "product", pol / "odm"
    need = [sysp / "plat_sepolicy.cil", ven / "plat_sepolicy_vers.txt",
            ven / "plat_pub_versioned.cil", ven / "vendor_sepolicy.cil"]
    if not all(p.exists() for p in need):
        return None
    vers = (ven / "plat_sepolicy_vers.txt").read_text().splitlines()[0].strip()
    gv = ven / "genfs_labels_version.txt"
    genfs = gv.read_text().strip() if gv.exists() else DEFAULT_GENFS_VERSION
    a = [str(sysp / "plat_sepolicy.cil"), "-m", "-M", "true", "-G", "-N", "-c", SEPOLICY_VERSION,
         str(sysp / f"mapping/{vers}.cil"), "-o", str(out), "-f", "/dev/null"]
    opt = [sysp / f"mapping/{vers}.compat.cil",
           se / "system_ext_sepolicy.cil", se / f"mapping/{vers}.cil", se / f"mapping/{vers}.compat.cil",
           prod / "product_sepolicy.cil", prod / f"mapping/{vers}.cil",
           ven / "plat_pub_versioned.cil", ven / "vendor_sepolicy.cil",
           odm / "odm_sepolicy.cil", sysp / f"plat_sepolicy_genfs_{genfs}.cil"]
    return a + [str(p) for p in opt if p.exists()]


def check_compile(stock: Path, sub: Path, pol: Path | None, secilc: str | None, td: Path) -> None:
    if not pol or not secilc:
        warn("secilc compile skipped (no --policy-dir / --secilc)")
        return
    res = {}
    for label, se in (("stock", stock), ("substrate", sub)):
        args = secilc_args(pol, se, td / f"{label}.bin")
        if args is None:
            warn("secilc compile skipped (plat/vendor policy not in the ROM zip)")
            return
        r = subprocess.run([secilc] + args, capture_output=True, text=True)
        res[label] = r
    if res["stock"].returncode != 0:
        warn("secilc can't compile even the STOCK policy (toolchain too old for this ROM?); "
             "compile check skipped:\n      " + res["stock"].stderr.strip().replace("\n", "\n      "))
        return
    r = res["substrate"]
    if r.returncode != 0:
        fail("boot-time sepolicy compile fails with the LG graft:\n      "
             + r.stderr.strip().replace("\n", "\n      "))
    else:
        print("ok    boot-time secilc compile (stock and substrate)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stock", type=Path, required=True)
    ap.add_argument("--substrate", type=Path, required=True)
    ap.add_argument("--policy-dir", type=Path)
    ap.add_argument("--secilc")
    a = ap.parse_args()
    pol = a.policy_dir if a.policy_dir and a.policy_dir.is_dir() else None

    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        stock, sub = td / "stock", td / "sub"
        for d, img in ((stock, a.stock), (sub, a.substrate)):
            (d / "mapping").mkdir(parents=True)
            for f in CONTEXTS + ("system_ext_sepolicy.cil",):
                dump(img, f"{SEL}/{f}", d / f)
            vf = pol / "vendor/plat_sepolicy_vers.txt" if pol else None
            if vf and vf.exists():
                v = vf.read_text().splitlines()[0].strip()
                for m in (f"{v}.cil", f"{v}.compat.cil"):
                    dump(img, f"{SEL}/mapping/{m}", d / "mapping" / m)

        check_contexts(stock, sub)
        check_properties(sub, pol)
        if exists(a.substrate, f"{SEL}/system_ext_sepolicy_and_mapping.sha256"):
            fail("system_ext_sepolicy_and_mapping.sha256 still present: init would boot the stock "
                 "precompiled policy and ignore the LG CIL")
        else:
            print("ok    policy hash removed (forces the boot-time compile)")
        check_compile(stock, sub, pol, a.secilc, td)

    print(f"\n{len(failures)} failed, {len(warnings)} warnings")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
