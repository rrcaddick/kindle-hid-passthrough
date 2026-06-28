#!/usr/bin/env python3
"""BT chip backends: per-Kindle Bluetooth lifecycle, selected by hardware."""

import subprocess

from logging_utils import log


def run(cmd, **kwargs):
    """Run a command silently; return True on success."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10, **kwargs)
        return r.returncode == 0
    except Exception:
        return False


def free_device(device_path):
    """Evict whatever userspace process holds device_path (fuser -k)."""
    try:
        r = subprocess.run(['fuser', '-k', device_path],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            holders = r.stderr.decode(errors='replace').strip()
            log.info(f"Evicted holders of {device_path}: {holders}")
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning(f"fuser unavailable or timed out: {e}")
        return False


class BtChip:
    """Bluetooth lifecycle for one Kindle's chip.

    Defaults are no-ops; subclasses override only the hooks they need.
    """

    def __init__(self, kindle):
        self.kindle = kindle

    def prepare(self) -> bool:
        """Bring the BT device to a state where the transport can open."""
        return True

    def on_transport_open(self):
        """Run right after the transport opens, before the first HCI command."""

    def power_off(self):
        """Turn the radio off (BT-off toggle)."""

    def ensure_powered(self):
        """Re-arm the chip before a (re)connect if it was powered off."""
