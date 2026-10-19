"""
Test that mkfontscale does not crash on crafted PCF font files whose
property string blocks lack NUL terminators.

Before the fix in ident.c, pcfIdentify() would call strcmp()/strdup()
on unterminated strings, reading past the allocated heap buffer.  Under
ASan this is a heap-buffer-overflow; without ASan the most likely result
is a crash (SIGSEGV/SIGBUS/SIGABRT).

After the fix, mkfontscale must silently reject the malformed fonts
and exit successfully.
"""

import gzip
import struct
from dataclasses import dataclass
from pathlib import Path

import pytest

PCF_VERSION = 0x70636601  # little-endian "pcf\x01"
PCF_PROPERTIES = 1 << 0

@dataclass
class PcfProperty:
    name_offset: int
    is_string: bool
    value_offset: int

@dataclass
class Pcf:
    strings_block: bytes
    properties: list[PcfProperty]

    def write_to(self, path: Path):
        with gzip.open(path, "wb") as f:
            f.write(self._build_pcf())


    def _build_pcf(self) -> bytes:
        nprops = len(self.properties)

        # Property table payload
        prop_data = bytearray()
        prop_data += struct.pack("<I", 0)       # format: LSB, no flags
        prop_data += struct.pack("<I", nprops)

        for prop in self.properties:
            prop_data += struct.pack("<I", prop.name_offset)
            prop_data += struct.pack("<B", 1 if prop.is_string else 0)
            prop_data += struct.pack("<I", prop.value_offset)

        # Pad to 4-byte alignment after property entries
        if nprops & 3:
            prop_data += b"\x00" * (4 - (nprops & 3))

        prop_data += struct.pack("<I", len(self.strings_block))
        prop_data += self.strings_block

        prop_offset = 8 + 16  # after header (8) + one TOC entry (16)

        # File header
        out = bytearray()
        out += struct.pack("<I", PCF_VERSION)
        out += struct.pack("<I", 1)               # table count
        out += struct.pack("<I", PCF_PROPERTIES)  # TOC: type
        out += struct.pack("<I", 0)               # TOC: format
        out += struct.pack("<I", len(prop_data))  # TOC: size
        out += struct.pack("<I", prop_offset)     # TOC: offset
        out += prop_data

        return bytes(out)


# ---- malformed PCF parameters ---------------------------------------------
# Each tuple: (test id, strings_block, props list, description)
MALFORMED_PCFS = [
    pytest.param(
        b"FONT\x00ABCDEFG",           # 12 bytes; no NUL after offset 5
        [PcfProperty(0, True, 5)],
        id="unterminated-value",
    ),
    pytest.param(
        b"FONTX",                      # 5 bytes; no NUL anywhere
        [PcfProperty(0, True, 4)],
        id="unterminated-name",
    ),
    pytest.param(
        b"FONT" + b"V" * 8,           # 12 bytes; zero NUL bytes
        [PcfProperty(0, True, 4)],
        id="no-nuls-at-all",
    ),
    pytest.param(
        b"FONT\x00",                  # name OK, but value offset == string_size
        [PcfProperty(0, True, 5)],
        id="value-offset-at-boundary",
    ),
    pytest.param(
        b"\x00" * 3 + b"FONT\x00AB",  # name offset near start, value unterminated
        [PcfProperty(3, True, 8)],
        id="name-near-start-value-unterminated",
    ),
]


@pytest.mark.parametrize("strings_block,props", MALFORMED_PCFS)
def test_no_crash_on_malformed_pcf(tmp_path, mkfontscale, strings_block, props):
    """mkfontscale -b must not crash on a PCF with unterminated strings."""
    pcf_path = tmp_path / "malformed.pcf"

    Pcf(strings_block, props).write_to(pcf_path)

    mkfontscale.run(tmp_path)


def test_unterminated_value_no_oob_in_output(tmp_path, mkfontscale):
    """Verify that an unterminated value string does not leak OOB heap data
    into fonts.dir.

    This test catches the vulnerability even without ASan: the unfixed code
    reads past the string block and writes OOB bytes into the output file,
    while the fixed code NUL-terminates the buffer so only in-bounds bytes
    can appear.

    The PCF has FONT name "FONT\\0" at offset 0 and value "VAL" (no NUL)
    at offset 5.  The string block is exactly 8 bytes: ``FONT\\0VAL``.
    After the fix (calloc with +1), the value is bounded to "VAL".
    Before the fix (malloc with no trailing NUL), strdup reads past
    the 8-byte allocation and includes whatever follows on the heap.

    We detect this by checking that the value in fonts.dir, if present,
    is exactly "val" (lowercased by mkfontscale) and nothing more."""
    in_bounds_value = b"VAL"
    strings = b"FONT\x00" + in_bounds_value  # no trailing NUL
    props = [PcfProperty(0, True, 5)]

    pcf_path = tmp_path / "oob.pcf"
    Pcf(strings, props).write_to(pcf_path)

    mkfontscale.run(tmp_path)

    fonts_dir = tmp_path / "fonts.dir"
    if fonts_dir.exists():
        raw = fonts_dir.read_bytes()
        content = raw.decode("ascii", errors="replace")
        for line in content.splitlines()[1:]:  # skip count line
            if line.startswith("oob.pcf "):
                xlfd = line.split(" ", 1)[1]
                assert xlfd == in_bounds_value.decode("ascii").lower(), (
                    f"fonts.dir contains out-of-bounds data: {xlfd!r} "
                    f"(expected at most {in_bounds_value!r})"
                )


def test_valid_pcf_does_not_crash(tmp_path, mkfontscale):
    """Sanity check: a well-formed (but minimal) PCF with NUL-terminated
    property strings must not crash mkfontscale.

    Note: the generated PCF only has a PROPERTIES table, so pcfIdentify()
    extracts the XLFD name but FreeType rejects the font as incomplete.
    The font won't appear in fonts.scale — that's fine; we're only
    testing that the string handling in pcfIdentify() is correct."""
    xlfd = "-misc-test-medium-r-normal--0-0-0-0-p-0-iso8859-1"
    strings = b"FONT\x00" + xlfd.encode("ascii") + b"\x00"
    props = [PcfProperty(0, True, 5)]  # name -> "FONT", value -> xlfd

    pcf_path = tmp_path / "good.pcf"
    Pcf(strings, props).write_to(pcf_path)

    mkfontscale.run(tmp_path)
