#!/usr/bin/env python3
"""
Bluetooth hardware setup for Kindle.

Ensures the BT kernel module is loaded and any process holding the HCI
device is evicted before opening the transport.

Auto-detects kernel version and module paths. Override via config.ini:

    [bluetooth]
    module_patterns = wmt_cdev_bt.ko, bt_drv.ko
    settle_time = 0.5

"""

import glob
import os
import re
import signal
import subprocess
import time

from kindle_detect import detect_codename, detect_kindle
from logging_utils import log

# Broadcom warm-handoff knobs
BT_DEV_WAKE_PATH = '/proc/bluetooth/sleep/btwake'
BT_SLEEP_PROTO_PATH = '/proc/bluetooth/sleep/proto'
BT_ENABLE_PATH = '/proc/bluetooth/btenable'
BTENABLE_LIPC = ['lipc-set-prop', 'com.lab126.btfd', 'BTenable', '1:1']
BSA_WARMUP_TIMEOUT = 12.0   # seconds to wait for bsa_server after BTenable
BSA_FIRMWARE_SETTLE = 2.0   # let bsa finish the .hcd download before we take over

# Known BT kernel module patterns across Kindle versions
DEFAULT_MODULE_PATTERNS = [
    'wmt_cdev_bt.ko',   # MediaTek (PW4/5, Kindle 10/11, Scribe)
    'bt_drv.ko',         # Older Freescale/NXP Kindles
]

BUNDLED_MODULES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'modules'
)
VERSION_TXT = '/etc/version.txt'


def _run(cmd, **kwargs):
    """Run a command silently, return success."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10, **kwargs)
        return r.returncode == 0
    except Exception:
        return False


def _find_bt_module(patterns=None):
    """Find the BT kernel module path for the running kernel.

    Returns:
        Module path string, or None if not found.
    """
    patterns = patterns or DEFAULT_MODULE_PATTERNS

    try:
        uname = os.uname().release
    except Exception:
        return None

    base = f'/lib/modules/{uname}/extra'

    for pattern in patterns:
        matches = glob.glob(f'{base}/{pattern}')
        if matches:
            return matches[0]
        # Also check subdirectories
        matches = glob.glob(f'{base}/**/{pattern}', recursive=True)
        if matches:
            return matches[0]

    return None


def _is_module_loaded(module_path):
    """Check if a kernel module is already loaded."""
    mod_name = os.path.basename(module_path).replace('.ko', '')
    try:
        with open('/proc/modules', 'r') as f:
            for line in f:
                if line.split()[0] == mod_name:
                    return True
    except Exception:
        pass
    return False


def _free_device(device_path):
    """Kill whatever userspace process is holding the BT device.

    Uses fuser(1), which queries the kernel for open file descriptors
    on the device. This avoids hardcoding Amazon's process names
    (bluetoothd, acsbtfd, btif_rxd, etc.) which differ across
    firmwares. Kernel threads don't appear in fuser output, which is
    correct since they can't be killed from userspace anyway.

    Returns:
        True if fuser ran (regardless of whether anything was killed).
    """
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
    if not _run(['/sbin/insmod', ko]):
        log.error("insmod failed")
        return False
    return os.path.exists('/dev/uhid')


def _kill_holders_via_proc(device_path):
    """Kill processes holding device_path by scanning /proc directly.

    Fallback for when fuser(1) is missing or didn't catch the holder.
    Walks every /proc/<pid>/fd, resolves each symlink, and SIGKILLs any
    process (other than ourselves) with the device open.

    Returns:
        Number of processes signalled.
    """
    my_pid = os.getpid()
    killed = 0
    try:
        pids = [e for e in os.listdir('/proc') if e.isdigit()]
    except OSError:
        return 0

    for pid in pids:
        if int(pid) == my_pid:
            continue
        fd_dir = f'/proc/{pid}/fd'
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # process exited or not readable
        for fd in fds:
            try:
                target = os.readlink(f'{fd_dir}/{fd}')
            except OSError:
                continue
            if target == device_path:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    killed += 1
                    log.info(f"Killed PID {pid} holding {device_path}")
                except OSError as e:
                    log.warning(f"Could not kill PID {pid}: {e}")
                break  # one match per process is enough

    return killed


def _is_device_free(device_path):
    """Check if the BT device can be opened."""
    try:
        fd = os.open(device_path, os.O_RDWR | os.O_NONBLOCK)
        os.close(fd)
        return True
    except OSError:
        return False


def _extract_device_path(transport_spec):
    """Extract the device path from a Bumble file: transport spec string.

    Only used for the MediaTek (file:) path; the Broadcom path uses
    kindle.device_path directly.
    """
    if transport_spec and transport_spec.startswith('file:'):
        return transport_spec[5:]
    return None


def prepare_bt(transport_spec=None, module_patterns=None, settle_time=0.5):
    """Prepare Bluetooth hardware for use.

    For MediaTek Kindles (11th gen+):
      1. Load BT kernel module if needed
      2. Evict processes holding /dev/stpbt (fuser, then a direct /proc
         scan if fuser is unavailable or comes up short)
      3. Verify /dev/stpbt is accessible

    For Broadcom Kindles (8th-10th gen):
      1. Let bsa_server warm the chip (it loads the .hcd firmware)
      2. Take the running UART from bsa (warm handoff)

    Args:
        transport_spec: Transport string (e.g. 'file:/dev/stpbt',
                       'serial:/dev/ttymxc2,2000000,rtscts').
        module_patterns: List of module filename patterns to search for.
        settle_time: Seconds to wait after evicting holders (MediaTek path).

    Returns:
        True if BT device is ready.
    """
    # Load bundled uhid.ko first; no-op on Kindles where /dev/uhid already exists
    _ensure_uhid()

    kindle = detect_kindle()

    device_path = _extract_device_path(transport_spec) or '/dev/stpbt'
    if kindle and not transport_spec:
        device_path = kindle.device_path

    if module_patterns is None and kindle and kindle.kernel_module:
        module_patterns = [kindle.kernel_module]

    log.info("Preparing Bluetooth hardware...")

    if kindle and kindle.transport_scheme == 'serial':
        return _prepare_brcm(kindle)

    return _prepare_standard(device_path, module_patterns, settle_time)


def _pgrep_x(name):
    """Return PIDs whose process name exactly matches `name`."""
    try:
        r = subprocess.run(['pgrep', '-x', name],
                           capture_output=True, text=True, timeout=5)
        return [int(p) for p in r.stdout.split() if p.isdigit()]
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return []


def _wait_for_bsa():
    """Wait for bsa_server to appear (chip warmed). Returns True on success."""
    deadline = time.monotonic() + BSA_WARMUP_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if _pgrep_x('bsa_server'):
            log.info("bsa_server up; BCM chip warmed")
            time.sleep(BSA_FIRMWARE_SETTLE)  # let the .hcd download finish
            return True
    return False


def _warm_up_chip():
    """Bring the chip up via Amazon's BT stack (it loads the firmware)."""
    if _pgrep_x('bsa_server'):
        log.info("bsa_server already running; BCM chip is warm")
        return True

    log.info("Warming BCM chip via btfd (BTenable)...")
    _run(BTENABLE_LIPC)
    if _wait_for_bsa():
        return True

    # unfreeze a btd left frozen by a prior handoff, else initctl restart hangs
    log.warning("bsa_server didn't start; recovering btd")
    for pid in _pgrep_x('btd'):
        try:
            os.kill(pid, signal.SIGCONT)
        except OSError:
            pass
    _run(['initctl', 'restart', 'btd'])
    time.sleep(2.0)
    _run(BTENABLE_LIPC)
    return _wait_for_bsa()


def _handoff_from_bsa(device_path):
    """Freeze btd, SIGKILL bsa, free the UART. The chip stays warm."""
    for pid in _pgrep_x('btd'):
        try:
            os.kill(pid, signal.SIGSTOP)
            log.info(f"Froze btd ({pid}) so it won't respawn bsa_server")
        except OSError as e:
            log.warning(f"Could not SIGSTOP btd {pid}: {e}")

    for pid in _pgrep_x('bsa_server'):
        try:
            os.kill(pid, signal.SIGKILL)
            log.info(f"Killed bsa_server ({pid}); taking the UART")
        except OSError as e:
            log.warning(f"Could not kill bsa_server {pid}: {e}")

    time.sleep(0.5)
    _free_device(device_path)
    time.sleep(0.3)


def wake_brcm_chip():
    """Wake the chip (dev_wake) and disable bluesleep. Call after the UART opens."""
    if not os.path.exists(BT_DEV_WAKE_PATH):
        return
    try:
        with open(BT_DEV_WAKE_PATH, 'w') as f:
            f.write('0')
        log.info("BCM chip woken (dev_wake asserted)")
        time.sleep(0.3)
    except OSError as e:
        log.warning(f"Could not assert dev_wake: {e}")
    try:
        with open(BT_SLEEP_PROTO_PATH, 'w') as f:
            f.write('0')
        log.info("BCM bluesleep disabled (chip stays awake)")
        time.sleep(0.2)
    except OSError as e:
        log.warning(f"Could not disable bluesleep: {e}")


def power_off_brcm_chip():
    """Power the chip down (btenable=0) for BT-off. No-op off Broadcom."""
    if not os.path.exists(BT_ENABLE_PATH):
        return
    try:
        with open(BT_ENABLE_PATH, 'w') as f:
            f.write('0')
        log.info("BCM chip powered off (btenable=0)")
    except OSError as e:
        log.warning(f"Could not power off BCM chip: {e}")


def ensure_brcm_powered():
    """Re-warm the chip if it was powered off (BT-off toggle). No-op otherwise."""
    if not os.path.exists(BT_ENABLE_PATH):
        return
    try:
        if open(BT_ENABLE_PATH).read().strip() != '0':
            return
    except OSError:
        return
    kindle = detect_kindle()
    if kindle and kindle.transport_scheme == 'serial':
        _prepare_brcm(kindle)


def _prepare_brcm(kindle):
    """Prepare Broadcom BCM4343 via warm handoff from bsa_server."""
    device_path = kindle.device_path

    if not os.path.exists(device_path):
        log.error(f"{device_path} does not exist")
        return False

    if not _warm_up_chip():
        log.error("Could not warm the BCM chip via bsa_server")
        return False

    _handoff_from_bsa(device_path)

    log.info(f"{device_path} ready (warm handoff complete; wake deferred to open)")
    return True


def _prepare_standard(device_path, module_patterns, settle_time):
    """Prepare standard (MediaTek) hardware."""
    module_path = _find_bt_module(module_patterns)
    if module_path:
        if _is_module_loaded(module_path):
            log.info(f"BT module already loaded: {os.path.basename(module_path)}")
        else:
            log.info(f"Loading BT module: {module_path}")
            if _run(['/sbin/insmod', module_path]):
                log.info("BT module loaded")
                time.sleep(0.5)
            else:
                log.warning(f"Failed to load {module_path} (may need root)")
    else:
        log.info("No BT kernel module found (may already be built-in)")

    if not os.path.exists(device_path):
        log.warning(f"{device_path} does not exist")
        return False

    if _is_device_free(device_path):
        log.info(f"{device_path} is available")
        return True

    log.info(f"{device_path} is busy, evicting holder...")
    if _free_device(device_path) and settle_time > 0:
        time.sleep(settle_time)

    if _is_device_free(device_path):
        log.info(f"{device_path} is now available")
        return True

    log.warning(f"{device_path} still busy, scanning /proc for holders...")
    if _kill_holders_via_proc(device_path) and settle_time > 0:
        time.sleep(settle_time)

    if _is_device_free(device_path):
        log.info(f"{device_path} is now available")
        return True

    log.warning(f"{device_path} still busy, waiting 2s...")
    time.sleep(2.0)

    if _is_device_free(device_path):
        log.info(f"{device_path} is now available")
        return True

    log.warning(f"{device_path} still busy after cleanup")
    return False
