#!/usr/bin/env python3
"""Neutralise ipsecd's dependency on the LG `vendor.lge.hardware.property` HAL.

LineageOS carries only the *interface* stub for `vendor.lge.hardware.property@2.0`,
not an implementation, so `IProperty::getService()` returns null inside ipsecd and
the daemon segfaults at IPSEC_CONNECTED. ipsecd has exactly two functions that go
through that HAL - a property getter and a property setter. This patches:

  * the SETTER  -> `ret` at its first instruction (writes become no-ops)
  * the GETTER  -> tail-call libcutils `property_get(key, value, NULL)` instead
                   of the HAL. Both `property_get` and `property_set` are already
                   imported by ipsecd (PLT entries exist), so no relocation work.

The two functions are found by their only cross-reference: a `bl` to the
`IProperty::getService@plt` thunk. From each `bl`, we walk back to the function
prologue (`paciasp`, 3f 23 03 d5) and overwrite it.

Getter vs setter: the getter reads a value back, so its body calls
`hidl_string::c_str()` and then copies the result out (and LG's own code already
has a `property_get` fallback path inside it). The setter's body has neither.

Verified against the LineageOS-era ipsecd shipped with the LG V60 (`file` reports
"for Android 33"); md5 52393430f8e5c3cac9fe04e1680117fe. For that exact binary the
patch is two literal edits (also listed in docs/PATCH-RECIPES.md 5):

    file 0x13854 : 3f 23 03 d5                          -> c0 03 5f d6
    file 0x13a00 : 3f 23 03 d5 ff 83 03 d1 fd 7b 0a a9  -> e0 03 02 aa e2 03 1f aa <b property_get@plt>

Usage:  python3 patch_ipsecd.py in/ipsecd out/ipsecd
"""
from __future__ import annotations

import struct
import sys

RET = bytes.fromhex("c0035fd6")           # ret
PACIASP = bytes.fromhex("3f2303d5")       # paciasp  (function prologue marker)
GET_HEAD = bytes.fromhex("e0030 2aa".replace(" ", "")) + bytes.fromhex("e2031faa")
#                         mov x0, x2      ;         mov x2, xzr


def le32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def sections(elf: bytes):
    # minimal ELF64 section table walk -> {name: (addr, off, size)}
    e_shoff = le32(elf, 0x28) | (le32(elf, 0x2C) << 32)
    e_shentsize = struct.unpack_from("<H", elf, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", elf, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", elf, 0x3E)[0]
    def sh(i):
        b = e_shoff + i * e_shentsize
        name = le32(elf, b)
        addr = le32(elf, b + 0x10) | (le32(elf, b + 0x14) << 32)
        off = le32(elf, b + 0x18) | (le32(elf, b + 0x1C) << 32)
        size = le32(elf, b + 0x20) | (le32(elf, b + 0x24) << 32)
        return name, addr, off, size
    _, _, stroff, _ = sh(e_shstrndx)
    out = {}
    for i in range(e_shnum):
        nmoff, addr, off, size = sh(i)
        end = elf.index(b"\0", stroff + nmoff)
        out[elf[stroff + nmoff:end].decode()] = (addr, off, size)
    return out


def plt_targets(elf: bytes, sec):
    """map symbol name -> plt stub vaddr, via .rela.plt + .plt layout."""
    dynsym_off = sec[".dynsym"][1]
    dynstr_off = sec[".dynstr"][1]
    rela_off, rela_size = sec[".rela.plt"][1], sec[".rela.plt"][2]
    plt_addr, _, plt_size = sec[".plt"]
    names = []
    for i in range(rela_size // 24):
        r_info = le32(elf, rela_off + i * 24 + 8) | (le32(elf, rela_off + i * 24 + 12) << 32)
        symidx = r_info >> 32
        st_name = le32(elf, dynsym_off + symidx * 24)
        end = elf.index(b"\0", dynstr_off + st_name)
        names.append(elf[dynstr_off + st_name:end].decode())
    # .plt = 0x20 header + one fixed-size stub per rela entry, in order.
    # Stub size is 0x10 normally, 0x18 with BTI landing pads - derive it.
    stub = (plt_size - 0x20) // max(1, len(names))
    return {n: plt_addr + 0x20 + i * stub for i, n in enumerate(names)}


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    data = bytearray(open(sys.argv[1], "rb").read())
    if data[:4] != b"\x7fELF":
        sys.exit("not an ELF")
    sec = sections(data)
    text_addr, text_off, text_size = sec[".text"]
    plt = plt_targets(data, sec)

    getsvc = next((v for k, v in plt.items() if "IProperty" in k and "getService" in k), None)
    pget = plt.get("property_get")
    if getsvc is None or pget is None:
        sys.exit("expected IProperty::getService@plt and property_get@plt imports; "
                 "this does not look like the V60 ipsecd")

    def v2o(va):  # vaddr -> file offset (within .text)
        return text_off + (va - text_addr)

    # find every `bl getsvc` in .text
    calls = []
    for o in range(text_off, text_off + text_size, 4):
        w = le32(data, o)
        if (w >> 26) == 0x25:  # BL
            imm = w & 0x03FFFFFF
            if imm & 0x02000000:
                imm -= 0x04000000
            tgt = text_addr + (o - text_off) + imm * 4
            if tgt == getsvc:
                calls.append(o)
    if len(calls) != 2:
        sys.exit(f"expected 2 xrefs to IProperty::getService, found {len(calls)}")

    def prologue_before(o):
        p = o
        while p > text_off:
            p -= 4
            if data[p:p + 4] == PACIASP:
                return p
        sys.exit("no paciasp prologue found before a getService call")

    va_by_name = {n: v for n, v in plt.items()}
    getter_marks = {va for n, va in va_by_name.items()
                    if n == "property_get" or "hidl_string5c_strEv" in n}

    def is_getter(fn_off):
        for o in range(fn_off, min(fn_off + 0x800, text_off + text_size), 4):
            if o != fn_off and data[o:o + 4] == PACIASP:
                break
            w = le32(data, o)
            if (w >> 26) == 0x25:
                imm = w & 0x03FFFFFF
                if imm & 0x02000000:
                    imm -= 0x04000000
                if text_addr + (o - text_off) + imm * 4 in getter_marks:
                    return True
        return False

    fns = {prologue_before(c) for c in calls}
    if len(fns) != 2:
        sys.exit("both getService calls resolved to the same function")
    getter = next((f for f in fns if is_getter(f)), None)
    if getter is None:
        sys.exit("could not tell getter from setter; patch by hand per PATCH-RECIPES.md 5")
    setter = (fns - {getter}).pop()

    # setter -> ret
    data[setter:setter + 4] = RET
    # getter -> mov x0,x2 ; mov x2,xzr ; b property_get@plt
    b_from = text_addr + (getter + 8 - text_off)
    off = (pget - b_from) >> 2
    b_ins = struct.pack("<I", 0x14000000 | (off & 0x03FFFFFF))
    data[getter:getter + 12] = GET_HEAD + b_ins

    open(sys.argv[2], "wb").write(data)
    print(f"setter @0x{setter + text_addr - text_off:x}  -> ret")
    print(f"getter @0x{getter + text_addr - text_off:x}  -> tail-call property_get@plt (0x{pget:x})")


if __name__ == "__main__":
    main()
