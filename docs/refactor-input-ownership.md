# Refactor, who owns input mapping

Planned split between this project and [kindle-button-mapper-rs](https://github.com/zampierilucas/kindle-button-mapper-rs). Not done yet, this is the target.

## Target

kindle-hid-passthrough does Bluetooth and nothing else. Scan, pair, connect, reconnect, descriptor cache, and a proper Linux evdev node via uhid. Its output is a kernel input device, with no opinion about what the buttons mean.

kindle-button-mapper does the mapping. It reads evdev nodes, whatever created them, and turns presses into actions.

Both stay usable alone. kindle-hid with no mapper still gives the native framework a working keyboard or remote. The mapper with no kindle-hid still maps the built-in buttons.

The KOReader plugin shrinks to a control panel for the daemon, which is scan, pair, connect, start, stop, status and logs. That is the part nothing else can do, because it drives the 8321 API.

## Where we are now

The daemon already honours this. The deviation is entirely in `koreader-plugin/`, which grew a second mapping layer inside KOReader, duplicating work the mapper already does.

| Job | Plugin today | Mapper today |
|---|---|---|
| Adopt the evdev node | `_attachInput` / `checkKeyDevice` | reads it directly, optional exclusive grab |
| Hat axis to direction | almost landed for #152 | `src/mapper.rs:185` |
| Fire a KOReader action | Dispatcher bindings | `src/koreader.rs`, HTTP Inspector on 8080, native reader fallback |
| Edit the mapping on device | Key mappings menu | `illusion/MapperManager` WAF app |

## The interface already exists

Neither project was wired to the other, but they meet anyway.

- The uhid node carries the Bluetooth address as `uniq`, visible as `U: Uniq=E0:F6:B5:BC:1C:7F/P` in `/proc/bus/input/devices`.
- `src/input.rs:51` matches devices on `uniq` for exactly that reason, since it survives renames and reconnects.
- `wait_for_matching_device` watches `/dev/input` with inotify `IN_CREATE`, which is the BT-connect case, a node appearing from nowhere.

So a mapper instance can already target a kindle-hid device by MAC and pick it up the moment it connects.

## Why adoption belongs on the mapper side

The devices KOReader refuses to adopt are the same devices that need mapping.

A real keyboard has keycodes 1..31, so FBInk classifies it INPUT_KEYBOARD and KOReader's stock externalkeyboard plugin opens it with no help from us. What falls through is gamepads, media remotes and page-turners, and they fall through because they speak in buttons and axes rather than keys, which is also why they need a mapping layer. One population, two names for it.

The mapper's own uinput node declares the full keycode range, so KOReader adopts it natively. Anything routed through the mapper needs no adoption code from us at all.

## Work needed

1. Multi-device in the mapper. `matches_device`, `scan_for_device` and `wait_for_matching_device` are all singular, so a pad and a keyboard at once needs either multi-device support or one instance per device.
2. Decide grab semantics, so a device routed through the mapper is not also handled raw by KOReader.
3. Install story for running both, see #122.

## What stays in the plugin

Daemon lifecycle and the BT device UI.

Possibly the keyboard fast path, meaning `fdopen` plus the `event_map_extra.lua` names. The mapper reaches KOReader over the HTTP Inspector, which is a TCP connect per press against a 50ms UI tick. That is fine for page turns and miserable for typing, so a real keyboard should keep going straight into KOReader's evdev path.

## Bridge

PR #156 restores gamepad adoption in the plugin, broken since 3.12.0. Under this plan that code eventually moves out, but it ships now because it unbreaks people today without asking them to install a second package. Do not delete the adoption path until mapper multi-device lands and the install story covers both.
