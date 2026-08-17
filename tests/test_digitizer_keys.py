#!/usr/bin/env python3
"""Tests for the digitizer -> key quirk.

The descriptor and every report below were captured off a real device: a
"CLOUT" BLE ring whose only input collection is a Digitizer. Its five
controls each replay a canned swipe or tap, so the strokes here are exactly
what the firmware sends, byte for byte, read out of the kernel's HID debugfs
before hid-core had a chance to drop them.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'kindle_hid_passthrough'))

from digitizer_keys import (DEFAULT_GESTURE_KEYS, GestureTranslator,
                            KEY_USAGES, build_keypad_collection,
                            find_digitizer_spec, leads_with_digitizer,
                            parse_input_fields, pick_report_id)
from uhid_handler import descriptor_is_pointer, strip_digitizer_collections

# Digitizer (report 2: tip switch + 12-bit X/Y) then Consumer Control
# (report 3). 102 bytes; stripping the digitizer leaves the last 32.
CLOUT_DESCRIPTOR = bytes.fromhex(
    '050d0901a10185020922a102094215002501750195018102093281029506810305'
    '0116000026e803750c55006500093036000046e8039501810226e80346e8030931'
    '8102c0c0050c0901a1018503150025017501950b09ea09e90aae0181029501750d'
    '8103c0'
)

# One stroke per physical control, as captured.
STROKES = {
    'swipe_right': [
        '02079ad214', '0207b8d214', '0207d6d214', '0207f4d214', '020712d314',
        '020730d314', '02074ed314', '02076cd314', '02078ad314', '0207a8d314',
        '0207c6d314', '0200c6d314',
    ],
    'swipe_down': [
        '0207f4d114', '0207f4b116', '0207f49118', '0207f4711a', '0207f4511c',
        '0207f4311e', '0207f41120', '0207f4f121', '0207f4d123', '0207f4b125',
        '0207f49127', '0200f49127',
    ],
    'swipe_left': [
        '02074dd114', '02072fd114', '020711d114', '0207f3d014', '0207d5d014',
        '0207b7d014', '020799d014', '02077bd014', '02075dd014', '02073fd014',
        '020721d014', '020021d014',
    ],
    'swipe_up': [
        '0207f4a129', '0207f48125', '0207f46121', '0207f4411d', '0207f42119',
        '0207f40115', '0207f4e110', '0207f4c10c', '0207f4a108', '0207f48104',
        '0207f46100', '0200f46100',
    ],
    'tap': ['0207f4411f', '0200f4411f'],
}

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: expected {want!r}, got {got!r}")
        print(f"  FAIL {label}: expected {want!r}, got {got!r}")
    else:
        print(f"  ok   {label}")


def test_parse_fields():
    print("parse_input_fields")
    fields = parse_input_fields(CLOUT_DESCRIPTOR)
    check("report IDs", sorted(fields), [2, 3])

    tip, in_range, x, y = fields[2]
    check("tip switch", (tip.page, tip.usage, tip.bit_offset, tip.bit_size),
          (0x0D, 0x42, 0, 1))
    check("in range", (in_range.usage, in_range.bit_offset), (0x32, 1))
    check("X", (x.page, x.usage, x.bit_offset, x.bit_size), (0x01, 0x30, 8, 12))
    check("Y", (y.page, y.usage, y.bit_offset, y.bit_size), (0x01, 0x31, 20, 12))
    check("X range", (x.logical_min, x.logical_max), (0, 1000))
    check("digitizer tagging", [f.in_digitizer for f in fields[2]],
          [True] * 4)

    # The consumer collection names three usages for eleven report bits, so
    # the last one repeats - the same expansion the kernel does.
    consumer = fields[3]
    check("consumer field count", len(consumer), 11)
    check("consumer usages", [f.usage for f in consumer[:4]],
          [0xEA, 0xE9, 0x1AE, 0x1AE])
    check("consumer not digitizer", any(f.in_digitizer for f in consumer), False)


def test_find_spec():
    print("find_digitizer_spec")
    spec = find_digitizer_spec(CLOUT_DESCRIPTOR)
    check("found", spec is not None, True)
    check("report id", spec.report_id, 2)
    check("spans", (spec.x_span, spec.y_span), (1000, 1000))

    # Decoding is against the payload, report ID already removed.
    check("decode swipe_right start",
          spec.decode(bytes.fromhex('02079ad214')[1:]), (True, 666, 333))
    check("decode tap", spec.decode(bytes.fromhex('0207f4411f')[1:]),
          (True, 500, 500))
    check("decode release", spec.decode(bytes.fromhex('0200c6d314')[1:]),
          (False, 966, 333))

    # A descriptor with no digitizer must be left alone.
    check("no digitizer -> None",
          find_digitizer_spec(strip_digitizer_collections(CLOUT_DESCRIPTOR)),
          None)


def test_report_id_choice():
    print("pick_report_id")
    check("avoids declared IDs", pick_report_id(CLOUT_DESCRIPTOR), 1)
    # The ring exposes GATT input reports 1..6 while declaring only 2 and 3;
    # an undeclared one must not collide with our synthetic keyboard.
    check("avoids reserved GATT IDs",
          pick_report_id(CLOUT_DESCRIPTOR, reserved=[1, 2, 3, 4, 5, 6]), 7)


def test_keypad_collection():
    print("build_keypad_collection")
    usages = [KEY_USAGES[DEFAULT_GESTURE_KEYS[g]]
              for g in ('swipe_right', 'swipe_left', 'swipe_down',
                        'swipe_up', 'tap')]
    coll = build_keypad_collection(7, usages)
    check("declares report 7", bytes([0x85, 0x07]) in coll, True)
    check("is a keyboard", coll.startswith(bytes([0x05, 0x01, 0x09, 0x06])), True)
    check("ends the collection", coll[-1], 0xC0)
    check("survives digitizer stripping",
          strip_digitizer_collections(coll), coll)
    # Appending it to the stripped descriptor must keep both collections.
    combined = strip_digitizer_collections(CLOUT_DESCRIPTOR) + coll
    check("combined parses", sorted(parse_input_fields(combined)), [3, 7])


def replay(translator, stroke):
    out = []
    for hexs in stroke:
        out.extend(translator.translate(bytes.fromhex(hexs)))
    return out


def test_gestures():
    print("GestureTranslator")
    spec = find_digitizer_spec(CLOUT_DESCRIPTOR)
    bits = {'swipe_right': 0x01, 'swipe_left': 0x02, 'swipe_down': 0x04,
            'swipe_up': 0x08, 'tap': 0x10}

    for gesture, stroke in STROKES.items():
        translator = GestureTranslator(spec, report_id=7)
        reports = replay(translator, stroke)
        check(f"{gesture} emits press+release", len(reports), 2)
        if len(reports) == 2:
            check(f"{gesture} press", reports[0].hex(),
                  bytes([7, bits[gesture]]).hex())
            check(f"{gesture} release", reports[1].hex(), bytes([7, 0]).hex())

    # Every control must map to a different key, or the ring is useless.
    translator = GestureTranslator(spec, report_id=7)
    seen = set()
    for stroke in STROKES.values():
        reports = replay(translator, stroke)
        seen.add(reports[0][1] if reports else None)
    check("five distinct keys", len(seen), 5)


def test_swipe_fires_before_release():
    """A page turn that waits for the release feels broken, so it must not."""
    print("early fire")
    spec = find_digitizer_spec(CLOUT_DESCRIPTOR)
    translator = GestureTranslator(spec, report_id=7)
    stroke = STROKES['swipe_right']
    fired_at = None
    for n, hexs in enumerate(stroke):
        if translator.translate(bytes.fromhex(hexs)):
            fired_at = n
            break
    check("fires mid-stroke", fired_at is not None and fired_at < len(stroke) - 1,
          True)
    # And the rest of the stroke must stay quiet - one click, one key.
    rest = []
    for hexs in stroke[fired_at + 1:]:
        rest.extend(translator.translate(bytes.fromhex(hexs)))
    check("no repeats for the rest of the stroke", rest, [])


def test_back_to_back_strokes():
    """State must reset, or the second press of a button does nothing."""
    print("repeated strokes")
    spec = find_digitizer_spec(CLOUT_DESCRIPTOR)
    translator = GestureTranslator(spec, report_id=7)
    for round_n in range(3):
        for gesture, stroke in STROKES.items():
            reports = replay(translator, stroke)
            check(f"round {round_n} {gesture}", len(reports), 2)


def test_custom_keys():
    print("configurable keys")
    spec = find_digitizer_spec(CLOUT_DESCRIPTOR)
    translator = GestureTranslator(spec, report_id=9, gesture_keys={
        'swipe_right': 'pagedown', 'swipe_left': 'pageup', 'tap': 'space',
        'swipe_up': '', 'swipe_down': '',
    })
    check("only mapped gestures", translator.gestures,
          ['swipe_right', 'swipe_left', 'tap'])
    check("usages", translator.usages,
          [KEY_USAGES['pagedown'], KEY_USAGES['pageup'], KEY_USAGES['space']])
    reports = replay(translator, STROKES['swipe_right'])
    check("pagedown press", reports[0].hex(), bytes([9, 0x01]).hex())
    # An unmapped gesture must emit nothing rather than a wrong key.
    check("unmapped swipe_up silent", replay(translator, STROKES['swipe_up']), [])


def test_ignores_noise():
    print("noise handling")
    spec = find_digitizer_spec(CLOUT_DESCRIPTOR)
    translator = GestureTranslator(spec, report_id=7)
    # A release with no preceding press must not invent a tap.
    check("orphan release", replay(translator, ['0200f4411f']), [])
    # A truncated report must not raise.
    check("short report", translator.translate(b'\x02\x07'), [])


# A keyboard that happens to append the same digitizer collection. Devices
# like this keep working after stripping, so the quirk must leave them alone.
KEYBOARD_PLUS_DIGITIZER = (
    bytes.fromhex(
        '05010906a101'    # Usage Page (Generic Desktop), Keyboard, Collection
        '8501'            # Report ID (1)
        '050719e029e7'    # Usage Page (Keyboard), Usage Min E0, Usage Max E7
        '15002501'        # Logical 0..1
        '75019508'        # 8 fields of 1 bit
        '8102'            # Input (Data,Var,Abs) - the modifier byte
        '95017508'        # 1 field of 8 bits
        '8103'            # Input (Const) - reserved byte
        'c0'              # End Collection
    )
    + CLOUT_DESCRIPTOR[:70]   # the ring's digitizer collection, appended
)


def test_leads_with_digitizer():
    print("leads_with_digitizer")
    check("ring leads with digitizer", leads_with_digitizer(CLOUT_DESCRIPTOR), True)
    check("keyboard combo does not",
          leads_with_digitizer(KEYBOARD_PLUS_DIGITIZER), False)
    check("stripped descriptor does not",
          leads_with_digitizer(strip_digitizer_collections(CLOUT_DESCRIPTOR)), False)
    # The combo device still has a translatable digitizer; 'auto' declines it
    # on ordering alone, which is the whole point of the gate.
    check("combo still has a spec",
          find_digitizer_spec(KEYBOARD_PLUS_DIGITIZER) is not None, True)


def test_end_to_end():
    """Assemble the descriptor exactly as _create_uhid_device does."""
    print("end to end")
    gatt_report_ids = [1, 2, 3, 4, 5, 6]      # what the ring exposes over GATT

    stripped = strip_digitizer_collections(CLOUT_DESCRIPTOR)
    check("stripping leaves the consumer collection", len(stripped), 32)

    spec = find_digitizer_spec(CLOUT_DESCRIPTOR)
    report_id = pick_report_id(stripped, reserved=gatt_report_ids)
    translator = GestureTranslator(spec, report_id, DEFAULT_GESTURE_KEYS)
    final = stripped + translator.descriptor_addition

    # What the kernel will now see.
    fields = parse_input_fields(final)
    check("kernel sees consumer + keypad", sorted(fields), [3, report_id])
    check("keypad has five keys", len(fields[report_id]), 5)
    check("keypad usages",
          [f.usage for f in fields[report_id]],
          [KEY_USAGES['right'], KEY_USAGES['left'], KEY_USAGES['down'],
           KEY_USAGES['up'], KEY_USAGES['enter']])
    check("keypad is on the keyboard page",
          {f.page for f in fields[report_id]}, {0x07})
    check("no digitizer survives",
          any(f.in_digitizer for fs in fields.values() for f in fs), False)
    check("not mistaken for a pointer", descriptor_is_pointer(final), False)
    check("descriptor fits uhid", len(final) <= 4096, True)

    # Now drive it the way _forward_report does, and collect what uhid gets.
    def forward(data):
        if translator.owns(data[0]):
            return translator.translate(data)
        return [data]

    sent = []
    for gesture in ('swipe_right', 'swipe_left', 'swipe_up', 'swipe_down', 'tap'):
        for hexs in STROKES[gesture]:
            sent.extend(forward(bytes.fromhex(hexs)))
    check("five presses and five releases", len(sent), 10)
    check("press order", [s[1] for s in sent[::2]], [0x01, 0x02, 0x08, 0x04, 0x10])
    check("every press is released", [s[1] for s in sent[1::2]], [0] * 5)
    check("all on the synthetic report", {s[0] for s in sent}, {report_id})

    # A consumer report from the ring must still pass through untouched.
    consumer = bytes([3, 0x02, 0x00, 0x00])
    check("consumer report forwarded as-is", forward(consumer), [consumer])


for fn in (test_parse_fields, test_find_spec, test_report_id_choice,
           test_keypad_collection, test_gestures,
           test_swipe_fires_before_release, test_back_to_back_strokes,
           test_custom_keys, test_ignores_noise, test_leads_with_digitizer,
           test_end_to_end):
    fn()

print()
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("All digitizer_keys tests passed.")
