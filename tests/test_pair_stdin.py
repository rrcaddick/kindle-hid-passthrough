#!/usr/bin/env python3
"""Tests for --pair early-stop on stdin (issue #179). Run: python3 tests/test_pair_stdin.py"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'kindle_hid_passthrough'))


def _stub(name, **attrs):
    """Register a fake module so main.py imports without bumble installed."""
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod


_stub('scanner', Scanner=object)
_stub('host', HIDHost=object)
_stub('daemon', main=None)

import main  # noqa: E402


class Bail(Exception):
    """Ends pair_mode after the first scan."""


class FakeScanner:
    def __init__(self):
        self.stop_event = None

    async def start(self):
        pass

    async def cleanup(self):
        pass

    async def scan(self, duration=10.0, concurrent=True, stop_event=None):
        self.stop_event = stop_event
        await asyncio.sleep(0.05)  # let a registered stdin reader fire
        raise Bail


class FakeStdin:
    """An fd that is readable at EOF, claiming to be a tty or not."""

    def __init__(self, tty):
        self._tty = tty
        self._fd, w = os.pipe()
        os.close(w)

    def fileno(self):
        return self._fd

    def isatty(self):
        return self._tty

    def readline(self):
        return ''

    def close(self):
        os.close(self._fd)


def run_pair(tty):
    """Run one --pair scan against a fake stdin; return its stop_event state."""
    scanner = FakeScanner()
    real_stdin, real_prepare, real_scanner = sys.stdin, main.prepare_bt, main.Scanner
    sys.stdin = FakeStdin(tty)
    main.prepare_bt = lambda: True
    main.Scanner = lambda: scanner
    try:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _drive(main.pair_mode()))
    finally:
        sys.stdin.close()
        sys.stdin, main.prepare_bt, main.Scanner = real_stdin, real_prepare, real_scanner
    return scanner.stop_event


async def _drive(coro):
    try:
        await coro
    except Bail:
        pass


def test_non_tty_stdin_does_not_cut_the_scan_short():
    stop_event = run_pair(tty=False)
    assert not stop_event.is_set(), "pipe/redirect at EOF killed the scan (issue #179)"


def test_tty_stdin_still_stops_early_on_input():
    stop_event = run_pair(tty=True)
    assert stop_event.is_set(), "readable tty no longer stops the scan"


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print(f"ok  {name}")
    print("all pass")
