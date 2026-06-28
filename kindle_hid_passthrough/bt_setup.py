#!/usr/bin/env python3
"""Bluetooth hardware setup — select and drive the per-Kindle chip backend."""

import os
import re

from bt_brcm import BrcmChip
from bt_chip import run
from bt_mtk import MtkChip
from kindle_detect import detect_codename, detect_kindle
from logging_utils import log

BUNDLED_MODULES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'modules'
)
VERSION_TXT = '/etc/version.txt'

_chip = None


def chip():
    """The BT chip backend for this Kindle (memoized; one chip per device)."""
    global _chip
    if _chip is None:
        _chip = make_chip(detect_kindle())
    return _chip


def make_chip(kindle):
    """Pick the chip backend for a detected Kindle."""
    if kindle and kindle.transport_scheme == 'serial':
        return BrcmChip(kindle)
    return MtkChip(kindle)


def _read_firmware_build():
    try:
        with open(VERSION_TXT) as f:
            line = f.readline()
    except OSError:
        return None
    m = re.search(r'-(\d+)\s*$', line.strip())
    return m.group(1) if m else None


def _ensure_uhid():
    """Load bundled uhid.ko on Kindles whose stock kernel lacks CONFIG_UHID."""
    if os.path.exists('/dev/uhid'):
        return True
    codename = detect_codename()
    if not codename:
        return False
    build = _read_firmware_build()
    if not build:
        log.error(f"could not read build from {VERSION_TXT}")
        return False
    kernel = os.uname().release
    expected = f"uhid-{kernel}-{build}-{codename}.ko"
    ko = os.path.join(BUNDLED_MODULES_DIR, expected)
    if not os.path.exists(ko):
        log.error(f"no bundled uhid.ko matching {expected}")
        log.error("please file an issue with /etc/version.txt output")
        return False
    log.info(f"loading {expected}")
    if not run(['/sbin/insmod', ko]):
        log.error("insmod failed")
        return False
    return os.path.exists('/dev/uhid')


def prepare_bt():
    """Load uhid if needed, then prepare the chip. True if BT is ready."""
    _ensure_uhid()
    log.info("Preparing Bluetooth hardware...")
    return chip().prepare()
