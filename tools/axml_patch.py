#!/usr/bin/env python3
"""Minimal, surgical binary AXML patcher.

Strategy: never resize any chunk. Only overwrite string-pool character
data in place (replacement string must have the identical UTF-16 code
unit length) and/or flip a boolean attribute's 4-byte data field.
This guarantees every chunk size / offset table in the file stays valid.
"""
import struct
import sys

RES_STRING_POOL_TYPE = 0x0001
RES_XML_START_ELEMENT_TYPE = 0x0102

def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def s32(b, o): return struct.unpack_from("<i", b, o)[0]

class AXML:
    def __init__(self, data: bytes):
        self.data = bytearray(data)
        self._parse_string_pool()

    def _parse_string_pool(self):
        # file header: type(2) headerSize(2) size(4) -> then first chunk
        # first real chunk after the 8-byte outer Res_chunk_header wrapping
        # the XML file is at offset 8 typically for RES_XML_TYPE(3).
        off = 8
        t = u16(self.data, off)
        assert t == RES_STRING_POOL_TYPE, f"expected string pool at {off}, got {hex(t)}"
        self.sp_off = off
        hdr_size = u16(self.data, off + 2)
        size = u32(self.data, off + 4)
        self.sp_size = size
        string_count = u32(self.data, off + 8)
        style_count = u32(self.data, off + 12)
        flags = u32(self.data, off + 16)
        strings_start = u32(self.data, off + 20)
        styles_start = u32(self.data, off + 24)
        self.is_utf8 = bool(flags & (1 << 8))
        self.string_count = string_count
        self.strings_start_abs = off + strings_start
        # offsets table: string_count * 4 bytes, right after the 28-byte header
        self.offsets_off = off + hdr_size
        self.next_chunk_off = off + size

    def get_string_offsets(self, index):
        rel = u32(self.data, self.offsets_off + index * 4)
        return self.strings_start_abs + rel

    def read_utf16_string(self, index):
        base = self.get_string_offsets(index)
        length = u16(self.data, base)  # code unit count
        chars = self.data[base + 2: base + 2 + length * 2]
        return chars.decode("utf-16-le"), base, length

    def read_utf8_string(self, index):
        base = self.get_string_offsets(index)
        # utf8 pool: one byte "utf16 length" (or two if >0x7f), then
        # one byte "utf8 length" (or two if >0x7f), then bytes, then \0
        # keep it simple: not expected/used for our target manifests.
        raise NotImplementedError("utf8 string pool not needed for this task")

    def find_string_index(self, target: str):
        for i in range(self.string_count):
            if self.is_utf8:
                continue
            s, _, _ = self.read_utf16_string(i)
            if s == target:
                return i
        return -1

    def overwrite_utf16_string_inplace(self, index, new_value: str):
        s, base, length = self.read_utf16_string(index)
        if len(new_value) != length:
            raise ValueError(f"length mismatch: old={length} new={len(new_value)} ({s!r} -> {new_value!r})")
        enc = new_value.encode("utf-16-le")
        self.data[base + 2: base + 2 + len(enc)] = enc
        print(f"  patched string #{index}: {s!r} -> {new_value!r}")

    def iter_start_elements(self):
        """Yield (chunk_off, name_str_idx, attr_array_off, attr_count, attr_size)."""
        off = self.next_chunk_off
        n = len(self.data)
        while off < n:
            ctype = u16(self.data, off)
            hdr_size = u16(self.data, off + 2)
            size = u32(self.data, off + 4)
            if size == 0:
                break
            if ctype == RES_XML_START_ELEMENT_TYPE:
                # node header: lineNumber(4) comment(4) at off+8
                attr_ext = off + 16
                ns = s32(self.data, attr_ext)
                name_idx = u32(self.data, attr_ext + 4)
                attr_start = u16(self.data, attr_ext + 8)
                attr_size = u16(self.data, attr_ext + 10)
                attr_count = u16(self.data, attr_ext + 12)
                arr_off = attr_ext + attr_start
                yield (off, name_idx, arr_off, attr_count, attr_size)
            off += size

    def get_attr(self, arr_off, i, attr_size):
        rec = arr_off + i * attr_size
        ns = s32(self.data, rec)
        name = u32(self.data, rec + 4)
        raw_value = s32(self.data, rec + 8)
        val_size = u16(self.data, rec + 12)
        val_res0 = self.data[rec + 14]
        val_type = self.data[rec + 15]
        val_data = u32(self.data, rec + 16)
        return dict(rec_off=rec, ns=ns, name_idx=name, raw_value=raw_value,
                    val_type=val_type, val_data=val_data, data_off=rec + 16)

    def set_attr_int_data(self, data_off, new_value: int):
        struct.pack_into("<I", self.data, data_off, new_value & 0xFFFFFFFF)
        print(f"  patched attr data @0x{data_off:x} -> {new_value}")

    def _set_outer_size(self, delta: int):
        # outer RES_XML_TYPE wrapper header: type(2) headerSize(2) size(4) at file offset 0
        cur = u32(self.data, 4)
        struct.pack_into("<I", self.data, 4, cur + delta)

    def append_string(self, value: str) -> int:
        """Grow the UTF-16 string pool by one entry, return its new index.
        Only safe for UTF-16 pools (is_utf8=False), and only ever call this
        BEFORE locating/inserting any XML element chunks (it shifts every
        byte after the string pool)."""
        assert not self.is_utf8, "utf8 pool append not implemented"
        old_string_count = self.string_count
        strings_start_abs = self.strings_start_abs
        pool_end = self.next_chunk_off  # == sp_off + sp_size (no style pool)

        new_index = old_string_count
        # relative offset (from strings_start) where the new string will land:
        # current string data occupies [strings_start_abs, pool_end)
        new_rel_offset = pool_end - strings_start_abs

        enc = value.encode("utf-16-le")
        str_bytes = struct.pack("<H", len(value)) + enc + b"\x00\x00"

        # 1) insert new 4-byte offset entry right at strings_start_abs
        #    (end of the current offsets table / start of string data)
        offset_entry = struct.pack("<I", new_rel_offset)
        self.data[strings_start_abs:strings_start_abs] = offset_entry
        added = len(offset_entry)

        # 2) append the new string bytes at the (now-shifted) end of the pool
        insert_at = pool_end + added
        self.data[insert_at:insert_at] = str_bytes
        added += len(str_bytes)

        # 3) 4-byte-align the pool chunk; pad with zeros if needed
        pad = (-added) % 4
        if pad:
            pad_at = insert_at + len(str_bytes)
            self.data[pad_at:pad_at] = b"\x00" * pad
            added += pad

        # 4) fix up the string pool chunk header: size, string_count,
        #    and stringsStart (the offsets table grew by len(offset_entry),
        #    pushing the string data start further into the chunk)
        struct.pack_into("<I", self.data, self.sp_off + 8, old_string_count + 1)
        cur_size = u32(self.data, self.sp_off + 4)
        struct.pack_into("<I", self.data, self.sp_off + 4, cur_size + added)
        cur_strings_start = u32(self.data, self.sp_off + 20)
        struct.pack_into("<I", self.data, self.sp_off + 20, cur_strings_start + len(offset_entry))

        # 5) fix up the outer file wrapper size
        self._set_outer_size(added)

        # 6) re-parse so self.string_count / next_chunk_off / etc. are fresh
        self._parse_string_pool()

        print(f"  appended string #{new_index}: {value!r} (+{added} bytes)")
        return new_index

    def find_start_element(self, tag_name: str):
        """Return (start_off, start_size, end_off, end_size) for the first
        top-level occurrence of <tag_name>...</tag_name> with no children,
        i.e. START chunk immediately followed by its matching END chunk."""
        RES_XML_END_ELEMENT_TYPE = 0x0103
        for (off, name_idx, arr_off, attr_count, attr_size) in self.iter_start_elements():
            s, *_ = self.read_utf16_string(name_idx)
            if s != tag_name:
                continue
            start_size = u32(self.data, off + 4)
            end_off = off + start_size
            end_type = u16(self.data, end_off)
            if end_type != RES_XML_END_ELEMENT_TYPE:
                continue  # has children; not the simple leaf we want
            end_size = u32(self.data, end_off + 4)
            return (off, start_size, end_off, end_size)
        raise ValueError(f"no leaf element <{tag_name}> found")

    def duplicate_uses_permission(self, template_tag_name: str, new_value_str_idx: int):
        """Duplicate a leaf <uses-permission android:name="..."/> element
        (found by template_tag_name, e.g. 'uses-permission'), patch its
        sole attribute's value to new_value_str_idx, and splice the copy
        in immediately after the original. Returns the absolute offset
        where the new element was inserted."""
        start_off, start_size, end_off, end_size = self.find_start_element(template_tag_name)
        total_len = end_off + end_size - start_off
        elem = bytearray(self.data[start_off:end_off + end_size])

        # locate the attribute record inside the copied bytes
        attr_ext = 16  # local offset of Res_xml_attrExt within the START chunk
        attr_start = u16(elem, attr_ext + 8)
        attr_size = u16(elem, attr_ext + 10)
        attr_count = u16(elem, attr_ext + 12)
        assert attr_count == 1, "expected exactly one attribute on template element"
        arr_off = attr_ext + attr_start
        rec = arr_off  # attr index 0
        # Res_xml_attribute: ns(4) name(4) rawValue(4) Res_value{size(2) res0(1) dataType(1) data(4)}
        struct.pack_into("<i", elem, rec + 8, new_value_str_idx)   # rawValue
        struct.pack_into("<I", elem, rec + 16, new_value_str_idx)  # Res_value.data

        insert_at = end_off + end_size
        self.data[insert_at:insert_at] = elem
        self._set_outer_size(len(elem))
        print(f"  inserted duplicated <{template_tag_name}> ({len(elem)} bytes) at 0x{insert_at:x}")
        return insert_at

if __name__ == "__main__":
    print("module loaded ok")
