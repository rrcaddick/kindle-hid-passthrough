#!/usr/bin/env python3
"""Turn a stripped digitizer's swipes and taps into ordinary key presses.

Some BLE "page turner" remotes — rings, clickers, selfie buttons — have no
keyboard at all. Their only input collection is a Digitizer, and a button
press makes the firmware replay a canned finger swipe: a short run of
absolute X/Y samples with Tip Switch held, then a release. On a phone that
lands on the touchscreen and turns the page. On a Kindle it lands nowhere,
because strip_digitizer_collections() has to drop the Digitizer before the
descriptor reaches uhid (see uhid_handler for why), so the reports arrive
with a report ID the kernel has never been told about and hid-core discards
them. The remote pairs, connects, gets an eventX node, and does nothing.

This module closes that gap. It parses the *original* descriptor to learn
where Tip Switch, X and Y sit in the digitizer's reports, watches the strokes
as they arrive, classifies each one as a swipe direction or a tap, and emits
a synthetic key report instead. The synthetic keys are declared by a small
keyboard collection appended to the descriptor we hand uhid, so the keys come
out of the same eventX node as everything else — no uinput needed, and
kindle-button-mapper and the KOReader plug-in see a plain keyboard.

Deliberately generic: nothing here keys off a vendor, product or address.
Any device whose digitizer collection carries Tip Switch plus absolute X/Y
is handled, and devices without one are left alone.
"""

import logging
from typing import Dict, List, Optional, Tuple

__all__ = [
    'DigitizerSpec',
    'GestureTranslator',
    'build_keypad_collection',
    'find_digitizer_spec',
    'leads_with_digitizer',
    'parse_input_fields',
    'pick_report_id',
    'DEFAULT_GESTURE_KEYS',
    'GESTURES',
]

logger = logging.getLogger(__name__)

# HID usage pages and usages we care about.
PAGE_GENERIC_DESKTOP = 0x01
PAGE_KEYBOARD = 0x07
PAGE_DIGITIZER = 0x0D
USAGE_X = 0x30
USAGE_Y = 0x31
USAGE_TIP_SWITCH = 0x42

# Keyboard usage IDs for the keys we can synthesize (HID Usage Table 10).
KEY_USAGES = {
    'right': 0x4F,
    'left': 0x50,
    'down': 0x51,
    'up': 0x52,
    'enter': 0x28,
    'pageup': 0x4B,
    'pagedown': 0x4E,
    'space': 0x2C,
    'home': 0x4A,
    'end': 0x4D,
    'escape': 0x29,
}

# The gestures we can recognise, in the bit order they get in the synthetic
# report. A stroke is classified into exactly one of these.
GESTURES = ('swipe_right', 'swipe_left', 'swipe_down', 'swipe_up', 'tap')

# Arrow keys and Enter: the layout that needs no configuration to be useful,
# since KOReader and kindle-button-mapper both bind them out of the box.
DEFAULT_GESTURE_KEYS = {
    'swipe_right': 'right',
    'swipe_left': 'left',
    'swipe_down': 'down',
    'swipe_up': 'up',
    'tap': 'enter',
}

# A stroke counts as a swipe once it travels this fraction of the axis's
# logical range. Canned swipes move 30% of the range or more, a tap moves
# nothing, so a tenth separates them with room to spare while still being
# small enough that a real fingertip drag registers.
SWIPE_FRACTION = 0.10


class Field:
    """One variable input field: where it sits in a report and what it means."""

    __slots__ = ('page', 'usage', 'bit_offset', 'bit_size',
                 'logical_min', 'logical_max', 'in_digitizer')

    def __init__(self, page, usage, bit_offset, bit_size,
                 logical_min, logical_max, in_digitizer):
        self.page = page
        self.usage = usage
        self.bit_offset = bit_offset
        self.bit_size = bit_size
        self.logical_min = logical_min
        self.logical_max = logical_max
        self.in_digitizer = in_digitizer

    def extract(self, payload: bytes) -> int:
        """Pull this field's value out of a report payload (report ID removed).

        HID packs fields little-endian from bit 0 of the first payload byte,
        and fields are free to straddle byte boundaries — this device's X and
        Y are 12 bits each — so shift the whole payload as one integer rather
        than trying to index bytes.
        """
        raw = int.from_bytes(payload, 'little')
        value = (raw >> self.bit_offset) & ((1 << self.bit_size) - 1)
        # Sign-extend only when the field is genuinely signed.
        if self.logical_min < 0 and value & (1 << (self.bit_size - 1)):
            value -= (1 << self.bit_size)
        return value

    def __repr__(self):
        return (f"Field(page=0x{self.page:02x}, usage=0x{self.usage:02x}, "
                f"bits={self.bit_offset}+{self.bit_size}, "
                f"range={self.logical_min}..{self.logical_max})")


def _signed(value: int, size: int) -> int:
    """Interpret an item's payload as signed, the way logical min/max are."""
    if size and value & (1 << (size * 8 - 1)):
        return value - (1 << (size * 8))
    return value


def parse_input_fields(descriptor: bytes) -> Dict[int, List[Field]]:
    """Map report ID -> variable input fields declared for it.

    A single pass over the descriptor's items, tracking the global item state
    (usage page, logical bounds, report size/count/ID) and the local usage
    list the way the HID spec lays out. Constant (padding) fields still
    advance the bit cursor but are not returned, and array fields are skipped
    as well since neither can carry a coordinate.

    Report ID 0 is the bucket for descriptors that declare no report ID at
    all, matching how such a device's reports arrive with no ID byte.
    """
    fields: Dict[int, List[Field]] = {}
    bit_cursor: Dict[int, int] = {}

    usage_page = 0
    logical_min = logical_max = 0
    report_size = report_count = 0
    report_id = 0
    usages: List[int] = []
    usage_min = usage_max = None
    depth = 0
    digitizer_depth = None      # depth at which the current digitizer began
    globals_stack: List[tuple] = []

    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        size = prefix & 0x03
        if size == 3:
            size = 4
        item_type = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F

        if i + 1 + size > len(descriptor):
            break
        raw = descriptor[i + 1:i + 1 + size]
        val = int.from_bytes(raw, 'little') if size else 0

        if item_type == 1:                      # Global
            if tag == 0:
                usage_page = val
            elif tag == 1:
                logical_min = _signed(val, size)
            elif tag == 2:
                # Logical Maximum is signed, but a descriptor that means
                # "0..255" often writes 0xFF in one byte. Treat it as
                # unsigned whenever the minimum is not negative.
                logical_max = val if logical_min >= 0 else _signed(val, size)
            elif tag == 7:
                report_size = val
            elif tag == 8:
                report_id = val
            elif tag == 9:
                report_count = val
            elif tag == 10:                     # Push
                globals_stack.append((usage_page, logical_min, logical_max,
                                      report_size, report_count, report_id))
            elif tag == 11 and globals_stack:   # Pop
                (usage_page, logical_min, logical_max,
                 report_size, report_count, report_id) = globals_stack.pop()

        elif item_type == 2:                    # Local
            if tag == 0:
                usages.append(val if size == 4 else (usage_page << 16) | val)
            elif tag == 1:
                usage_min = val if size == 4 else (usage_page << 16) | val
            elif tag == 2:
                usage_max = val if size == 4 else (usage_page << 16) | val

        elif item_type == 0:                    # Main
            if tag == 8:                        # Input
                offset = bit_cursor.get(report_id, 0)
                is_constant = bool(val & 0x01)
                is_variable = bool(val & 0x02)
                if is_variable and not is_constant:
                    expanded = _expand_usages(usages, usage_min, usage_max,
                                              report_count)
                    bucket = fields.setdefault(report_id, [])
                    for n in range(report_count):
                        full = expanded[n] if n < len(expanded) else 0
                        bucket.append(Field(
                            page=full >> 16,
                            usage=full & 0xFFFF,
                            bit_offset=offset + n * report_size,
                            bit_size=report_size,
                            logical_min=logical_min,
                            logical_max=logical_max,
                            in_digitizer=digitizer_depth is not None,
                        ))
                bit_cursor[report_id] = offset + report_size * report_count
            elif tag == 9 or tag == 11:         # Output / Feature
                pass                            # different report space
            elif tag == 10:                     # Collection
                if digitizer_depth is None and usage_page == PAGE_DIGITIZER:
                    digitizer_depth = depth
                depth += 1
            elif tag == 12:                     # End Collection
                depth -= 1
                if digitizer_depth is not None and depth <= digitizer_depth:
                    digitizer_depth = None
            usages = []
            usage_min = usage_max = None

        i += 1 + size

    return fields


def _expand_usages(usages, usage_min, usage_max, count) -> List[int]:
    """The per-item usage list for one Input item.

    Explicit usages win. A Usage Minimum/Maximum pair fills the rest, and
    when a descriptor names fewer usages than it has report items the HID
    spec says the last one repeats — which is exactly how this ring declares
    its consumer bits.
    """
    out = list(usages)
    if usage_min is not None and usage_max is not None:
        out.extend(range(usage_min, usage_max + 1))
    if not out:
        return []
    while len(out) < count:
        out.append(out[-1])
    return out[:count]


class DigitizerSpec:
    """Where Tip Switch, X and Y live in one digitizer report."""

    def __init__(self, report_id: int, tip: Field, x: Field, y: Field):
        self.report_id = report_id
        self.tip = tip
        self.x = x
        self.y = y

    @property
    def x_span(self) -> int:
        return max(1, self.x.logical_max - self.x.logical_min)

    @property
    def y_span(self) -> int:
        return max(1, self.y.logical_max - self.y.logical_min)

    def decode(self, payload: bytes) -> Tuple[bool, int, int]:
        return bool(self.tip.extract(payload)), \
            self.x.extract(payload), self.y.extract(payload)

    def __repr__(self):
        return (f"DigitizerSpec(report_id={self.report_id}, "
                f"x={self.x}, y={self.y})")


def find_digitizer_spec(descriptor: bytes) -> Optional[DigitizerSpec]:
    """The first digitizer report that carries Tip Switch and absolute X/Y.

    Returns None for descriptors with no digitizer, and for digitizers too
    exotic to translate (no tip switch, or no coordinates) — callers treat
    that as "nothing to do" and leave the device on the normal path.
    """
    for report_id, bucket in sorted(parse_input_fields(descriptor).items()):
        digitizer = [f for f in bucket if f.in_digitizer]
        if not digitizer:
            continue
        tip = _first(digitizer, PAGE_DIGITIZER, USAGE_TIP_SWITCH)
        x = _first(digitizer, PAGE_GENERIC_DESKTOP, USAGE_X)
        y = _first(digitizer, PAGE_GENERIC_DESKTOP, USAGE_Y)
        if tip and x and y and x.bit_size > 1 and y.bit_size > 1:
            return DigitizerSpec(report_id, tip, x, y)
    return None


def _first(fields, page, usage) -> Optional[Field]:
    for f in fields:
        if f.page == page and f.usage == usage:
            return f
    return None


def leads_with_digitizer(descriptor: bytes) -> bool:
    """True if the descriptor's first top-level collection is a Digitizer.

    The same reasoning descriptor_is_pointer() uses: a device leads with its
    primary function. A remote whose whole purpose is faking touches opens
    with a Digitizer, so stripping it leaves nothing; a keyboard or gamepad
    that merely appends a digitizer opens with something else and keeps
    working on its own. Only the former wants its strokes turned into keys.
    """
    i = 0
    usage_page = None
    while i < len(descriptor):
        prefix = descriptor[i]
        size = prefix & 0x03
        if size == 3:
            size = 4
        item_type = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        if i + 1 + size > len(descriptor):
            break
        val = int.from_bytes(descriptor[i + 1:i + 1 + size], 'little') if size else 0

        if item_type == 1 and tag == 0:          # Global: Usage Page
            usage_page = val
        elif item_type == 0 and tag == 10:       # Main: Collection
            return usage_page == PAGE_DIGITIZER
        i += 1 + size
    return False


def pick_report_id(descriptor: bytes, reserved=()) -> Optional[int]:
    """A report ID free for our synthetic keyboard.

    Avoids every ID the descriptor declares and every ID the transport says
    the device can send, so a stray report from the remote can never be
    mistaken for one of our key presses.
    """
    used = set(parse_input_fields(descriptor))
    used.update(int(r) for r in reserved)
    used.discard(0)
    for candidate in range(1, 256):
        if candidate not in used:
            return candidate
    return None


def build_keypad_collection(report_id: int, usages: List[int]) -> bytes:
    """A keyboard collection declaring `usages` as a one-byte bitmap.

    A bitmap (Variable) field rather than the usual keyboard array: we only
    ever assert one key at a time, and a bitmap keeps the report to a single
    byte whose bit order matches GESTURES, so building one is a shift.
    """
    if not usages or len(usages) > 8:
        raise ValueError(f"need 1..8 key usages, got {len(usages)}")

    items = bytearray()
    items += bytes([0x05, PAGE_GENERIC_DESKTOP])    # Usage Page (Generic Desktop)
    items += bytes([0x09, 0x06])                    # Usage (Keyboard)
    items += bytes([0xA1, 0x01])                    # Collection (Application)
    items += bytes([0x85, report_id])               # Report ID
    items += bytes([0x05, PAGE_KEYBOARD])           # Usage Page (Keyboard)
    for usage in usages:
        items += bytes([0x09, usage])               # Usage (key)
    items += bytes([0x15, 0x00])                    # Logical Minimum (0)
    items += bytes([0x25, 0x01])                    # Logical Maximum (1)
    items += bytes([0x75, 0x01])                    # Report Size (1)
    items += bytes([0x95, len(usages)])             # Report Count (n)
    items += bytes([0x81, 0x02])                    # Input (Data,Var,Abs)
    padding = 8 - len(usages)
    if padding:
        items += bytes([0x95, padding])             # Report Count (pad)
        items += bytes([0x81, 0x03])                # Input (Cnst,Var,Abs)
    items += bytes([0xC0])                          # End Collection
    return bytes(items)


class GestureTranslator:
    """Watches one digitizer's strokes and emits synthetic key reports.

    A stroke runs from the first sample with Tip Switch set to the sample
    that clears it. The direction is decided as soon as the stroke has
    travelled far enough, so a swipe fires while the finger is still down
    rather than waiting for the release — the canned swipes take a tenth of a
    second or so, and a page turn that lags behind the click feels broken. A
    stroke that ends without moving is a tap.
    """

    def __init__(self, spec: DigitizerSpec, report_id: int,
                 gesture_keys: Optional[Dict[str, str]] = None):
        self.spec = spec
        self.report_id = report_id
        keys = dict(DEFAULT_GESTURE_KEYS)
        keys.update(gesture_keys or {})
        self.gestures = [g for g in GESTURES if keys.get(g)]
        self.usages = [KEY_USAGES[keys[g]] for g in self.gestures]
        self._bit = {g: 1 << n for n, g in enumerate(self.gestures)}
        self._down = False
        self._start = (0, 0)
        self._fired = False

    @property
    def descriptor_addition(self) -> bytes:
        return build_keypad_collection(self.report_id, self.usages)

    def owns(self, report_id: int) -> bool:
        return report_id == self.spec.report_id

    def translate(self, data: bytes) -> List[bytes]:
        """Feed one raw digitizer report; get back reports to send instead.

        Callers must not forward the original: its report ID is not in the
        descriptor uhid was given, so the kernel would drop it anyway.
        """
        payload = data[1:] if self.spec.report_id else data
        try:
            tip, x, y = self.spec.decode(payload)
        except Exception:
            return []

        if tip and not self._down:
            self._down = True
            self._fired = False
            self._start = (x, y)
            return []

        if tip:
            if self._fired:
                return []
            gesture = self._classify(x, y)
            if gesture:
                self._fired = True
                return self._press(gesture)
            return []

        # Tip released.
        was_down, fired = self._down, self._fired
        self._down = False
        self._fired = False
        if not was_down or fired:
            return []
        gesture = self._classify(x, y) or 'tap'
        return self._press(gesture)

    def _classify(self, x: int, y: int) -> Optional[str]:
        dx = x - self._start[0]
        dy = y - self._start[1]
        x_threshold = self.spec.x_span * SWIPE_FRACTION
        y_threshold = self.spec.y_span * SWIPE_FRACTION
        if abs(dx) >= x_threshold and abs(dx) * self.spec.y_span >= \
                abs(dy) * self.spec.x_span:
            return 'swipe_right' if dx > 0 else 'swipe_left'
        if abs(dy) >= y_threshold:
            return 'swipe_down' if dy > 0 else 'swipe_up'
        return None

    def _press(self, gesture: str) -> List[bytes]:
        """A press report followed by a release, since a gesture is a tap.

        The remote gives us no key-up of its own, so an unpaired press would
        latch the key down forever and auto-repeat.
        """
        bit = self._bit.get(gesture)
        if bit is None:
            return []
        logger.debug(f"gesture {gesture} -> key bit 0x{bit:02x}")
        return [bytes([self.report_id, bit]), bytes([self.report_id, 0])]
