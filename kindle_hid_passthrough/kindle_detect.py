#!/usr/bin/env python3
"""
Kindle model detection and hardware defaults.

Identifies the Kindle model from the device serial number and provides
hardware-specific defaults (BT device path, kernel module, transport).

Only BT-capable Kindles (Oasis 1 / 2016 onwards) are included.

Device codes and model data sourced from NiLuJe's KindleTool:
  https://github.com/NiLuJe/KindleTool
Generation/platform mapping from KindleModding:
  https://github.com/KindleModding/kindlemodding.github.io

Serial decoding uses Crockford's base32 (charset: 0-9 A-H J-N P V-X,
no I/O/Y/Z). Old serials (prefix B) encode device code as 2-char hex
at positions 2-3. New serials (prefix G) encode as 3-char base32 at
positions 3-5.
"""

from dataclasses import dataclass
from typing import Optional

from logging_utils import log

USID_PATH = '/proc/usid'

_B32_CHARS = '0123456789ABCDEFGHJKLMNPQRSTUVWX'
_B32_LOOKUP = {c: i for i, c in enumerate(_B32_CHARS)}


@dataclass
class KindleDefaults:
    """Hardware defaults for a Kindle model."""
    device_path: str                    # BT device (e.g. /dev/stpbt, /dev/ttymxc2)
    kernel_module: Optional[str]        # Primary kernel module filename
    model_name: str                     # Human-readable generation name
    transport_scheme: str = 'file'      # Bumble transport scheme ('file' or 'serial')
    baud_rate: Optional[int] = None     # Serial baud rate (for serial transport)
    flow_control: Optional[str] = None  # Serial flow control flag ('rtscts' or 'dsrdtr')


# --- Hardware profiles ---

_MTK_HW = dict(
    device_path='/dev/stpbt',
    kernel_module='wmt_cdev_bt.ko',
    transport_scheme='file',
)

# Broadcom BCM4343 over UART. Amazon's BSA daemon (bsa_server) owns the UART and
# does the timing-critical firmware download / clock bring-up (which wedges the
# kernel if we attempt it cold ourselves). We let bsa warm the chip, then take
# the running UART from it (warm handoff) and talk standard HCI at its 2M baud.
_BRCM_HW = dict(
    device_path='/dev/ttymxc2',
    kernel_module=None,
    transport_scheme='serial',
    baud_rate=2000000,
    flow_control='rtscts',
)


# --- Generations with device codes ---
# Each entry: (name, hw_profile, [device_codes], codename)
# Device codes are integers derived from KindleTool model_tuples.
# codename drives bundled uhid.ko lookup; None for MTK (kernel has /dev/uhid).

_GENERATIONS = [
    # NXP i.MX + Broadcom BCM4343 (8th-10th gen, UART HCI)
    ('Kindle Basic 2',  _BRCM_HW, [0x1BC, 0x269, 0x26A], 'heisenberg'),  # KT3, Heisenberg (2016)
    ('Kindle Oasis',    _BRCM_HW, [0x20C, 0x20D, 0x219, 0x21A, 0x21B, 0x21C], 'duet'),  # KOA, Duet (2016)
    ('Kindle Oasis 2',  _BRCM_HW, [0x295, 0x296, 0x297, 0x298, 0x2E1, 0x2E2, 0x2E6, 0x2E7, 0x2E8, 0x341, 0x342, 0x343, 0x344, 0x347, 0x34A], 'zelda'),  # KOA2, Zelda (2017)
    ('Kindle PW4',      _BRCM_HW, [0x2F7, 0x361, 0x362, 0x363, 0x364, 0x365, 0x366, 0x367, 0x372, 0x373, 0x374, 0x375, 0x376, 0x402, 0x403, 0x4D8, 0x4D9, 0x4DA, 0x4DB, 0x4DC, 0x4DD, 0x2F4], 'rex'),  # PW4, Rex (2018)
    ('Kindle Basic 3',  _BRCM_HW, [0x414, 0x3CF, 0x3D0, 0x3D1, 0x3D2, 0x3AB], 'rex'),  # KT4, Rex (2019)
    ('Kindle Oasis 3',  _BRCM_HW, [0x434, 0x3D8, 0x3D7, 0x3D6, 0x3D5, 0x3D4], 'zelda'),  # KOA3, Zelda (2019)

    # MediaTek CONSYS platforms (11th gen+)
    ('Kindle PW5',       _MTK_HW, [0x690, 0x700, 0x6FF, 0x7AD, 0x829, 0x82A, 0x971, 0x972, 0x9B3], None),  # PW5, Bellatrix (2021)
    ('Kindle Basic 4',   _MTK_HW, [0x84D, 0x8BB, 0x86A, 0x958, 0x957, 0x7F1, 0x84C], None),  # KT5, Bellatrix (2022)
    ('Kindle Scribe',    _MTK_HW, [0x8F2, 0x974, 0x8C3, 0x847, 0x975, 0x874, 0x875, 0x8E0], None),  # KS, Bellatrix3 (2022)
    ('Kindle Basic 5',   _MTK_HW, [0xE85, 0xE86, 0xE84, 0xE83, 0x2909, 0xE82, 0xE75], None),  # KT6, Bellatrix (2024)
    ('Kindle PW6',       _MTK_HW, [0xC89, 0xC86, 0xC7F, 0xC7E, 0xE2A, 0xE25, 0xE23, 0xE28, 0xE45, 0xE5A], None),  # PW6, Bellatrix4 (2024)
    ('Kindle Scribe 2',  _MTK_HW, [0xFA0, 0xFA1, 0xFE5, 0xF9D, 0xFE4, 0xFE3, 0x102E, 0x102D], None),  # KS2, Bellatrix3 (2024)
    ('Kindle Colorsoft',  _MTK_HW, [0xE29, 0xE24, 0xE2B, 0xE26, 0xE22, 0xC9F, 0xE27, 0xE5B, 0xE46, 0x10A6, 0x10A5, 0x11D7], None),  # CS, Bellatrix4 (2024)
    ('Kindle Scribe 3',  _MTK_HW, [0x12F0, 0x12EE, 0x12F4, 0x11E8, 0x11EA, 0x10A4], None),  # KS3, Platpa6 (2025)
    ('Kindle Scribe CS', _MTK_HW, [0x13BF, 0x12EF, 0x12F1, 0x11E9, 0x11EB, 0x10D7], None),  # KSC, Platcs8 (2025)
]

# Flat lookup: device_code -> (name, hw_profile, codename)
_CODE_LOOKUP = {}
for _name, _hw, _codes, _codename in _GENERATIONS:
    for _code in _codes:
        _CODE_LOOKUP[_code] = (_name, _hw, _codename)


def _decode_device_code(serial: str) -> Optional[int]:
    """Extract integer device code from a Kindle serial number.

    Old serials (start with B): 2-char hex at positions 2-3.
    New serials (start with G): 3-char Crockford base32 at positions 3-5.
    """
    if not serial or len(serial) < 6:
        return None

    serial = serial.upper()

    if serial[0] == 'G':
        code_str = serial[3:6]
        try:
            return ((_B32_LOOKUP[code_str[0]] << 10) |
                    (_B32_LOOKUP[code_str[1]] << 5) |
                    _B32_LOOKUP[code_str[2]])
        except (KeyError, IndexError):
            return None
    else:
        try:
            return int(serial[2:4], 16)
        except ValueError:
            return None


def read_serial() -> Optional[str]:
    """Read the Kindle serial number from /proc/usid."""
    try:
        with open(USID_PATH, 'r') as f:
            return f.read().strip()
    except (OSError, IOError):
        return None


def detect_kindle(serial: str = None) -> Optional[KindleDefaults]:
    """Detect the Kindle model and return hardware defaults.

    Only returns a result for BT-capable models (Oasis 1 / 2016 onwards).

    Args:
        serial: Optional serial number override (for testing).

    Returns:
        KindleDefaults if a BT-capable model is recognized, None otherwise.
    """
    if serial is None:
        serial = read_serial()
    if not serial:
        log.debug(f"Could not read Kindle serial from {USID_PATH}")
        return None

    device_code = _decode_device_code(serial)
    if device_code is None:
        log.warning("Could not decode device code from serial")
        return None

    result = _CODE_LOOKUP.get(device_code)
    if result is None:
        log.info(f"Unknown device code 0x{device_code:X} (pre-BT or unrecognized)")
        return None

    name, hw, _codename = result
    defaults = KindleDefaults(
        device_path=hw['device_path'],
        kernel_module=hw.get('kernel_module'),
        model_name=name,
        transport_scheme=hw.get('transport_scheme', 'file'),
        baud_rate=hw.get('baud_rate'),
        flow_control=hw.get('flow_control'),
    )
    log.info(f"Detected {name} (code 0x{device_code:X})")
    return defaults


def detect_codename(serial: str = None) -> Optional[str]:
    if serial is None:
        serial = read_serial()
    if not serial:
        return None
    code = _decode_device_code(serial)
    if code is None:
        return None
    result = _CODE_LOOKUP.get(code)
    if result is None:
        return None
    return result[2]
