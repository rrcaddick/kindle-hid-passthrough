#!/usr/bin/env python3
"""BLE HID handler mixin for HIDHost."""

import asyncio

from bumble.core import AdvertisingData, BT_LE_TRANSPORT, InvalidStateError
from bumble.device import Device, Peer
from bumble.gatt import (
    GATT_DEVICE_NAME_CHARACTERISTIC,
    GATT_GENERIC_ACCESS_SERVICE,
    GATT_HID_CONTROL_POINT_CHARACTERISTIC,
    GATT_HUMAN_INTERFACE_DEVICE_SERVICE,
    GATT_PROTOCOL_MODE_CHARACTERISTIC,
    GATT_REPORT_CHARACTERISTIC,
    GATT_REPORT_MAP_CHARACTERISTIC,
    GATT_REPORT_REFERENCE_DESCRIPTOR,
)
from bumble.l2cap import (
    L2CAP_CONNECTION_PARAMETERS_ACCEPTED_RESULT,
    L2CAP_Connection_Parameter_Update_Response,
)
from bumble.hci import (
    Address,
    HCI_LE_Add_Device_To_Filter_Accept_List_Command,
    HCI_LE_Clear_Filter_Accept_List_Command,
    HCI_LE_Create_Connection_Cancel_Command,
    HCI_LE_Create_Connection_Command,
    OwnAddressType,
)

from config import Protocol, config, normalize_addr, clean_device_name
from logging_utils import log

HID_REPORT_TYPE_INPUT = 1

# Link timing we ask for, in the units the HCI commands take: 1.25ms for the
# intervals, 10ms for the supervision timeout.
#
# These are also what _ble_initiate() puts in HCI_LE_Create_Connection_Command,
# and they are deliberately tight: a remote's first button press has to wait
# for the next radio anchor point, so a slow interval is felt directly as lag.
CONN_INTERVAL_MIN = 12       # 15ms
CONN_INTERVAL_MAX = 24       # 30ms
CONN_MAX_LATENCY = 0         # never let the peripheral skip anchor points
CONN_SUPERVISION_TIMEOUT = 72   # 720ms
# The spec requires timeout > (1 + latency) * interval_max * 2, and a peripheral
# that asks for a longer one usually has a reason, so keep the larger of the
# two rather than forcing ours and provoking dropouts.
CONN_SUPERVISION_MAX = 3200  # 32s, the spec ceiling




class BLEMixin:
    """BLE methods for HIDHost."""

    BLE_INIT_WINDOW = 18.0
    BLE_SCAN_WINDOW = 8.0

    def _pin_connection_parameters(self):
        """Keep the link fast even when the peripheral asks to slow it down.

        We open the connection at a 15-30ms interval with no peripheral
        latency, but a remote is free to ask for something more power-saving
        the moment it is connected, and Bumble's central-role handler grants
        whatever is asked without looking at it. A remote that negotiates its
        way to a slow interval then makes every press wait for the next radio
        anchor point, which is felt as the first button press after a pause
        taking a second or more while a burst of presses stays instant.

        So keep answering "accepted" — refusing tends to make remotes retry or
        drop the link — but hand the controller our timing rather than theirs.
        The peripheral's supervision timeout is honoured when it is longer than
        ours, since that only makes the link more forgiving, and shortening it
        under a peripheral that expects slack risks needless disconnections.
        """
        manager = getattr(self.device, 'l2cap_channel_manager', None)
        handler = getattr(manager, 'on_l2cap_connection_parameter_update_request', None)
        if handler is None:
            log.warning("Cannot pin BLE connection parameters on this Bumble build")
            return

        def clamped(connection, cid, request):
            asked = (request.interval_min, request.interval_max,
                     request.latency, request.timeout)

            # A remote that wanted latency will not stop asking just because
            # it was told yes — it can see the link did not change, and a
            # CLOUT ring re-asks about twice a second, indefinitely. Answer
            # every time, but only touch the link once per connection: each
            # grant is a real LL connection update, and renegotiating twice a
            # second costs exactly the responsiveness we came for.
            #
            # Whether we have already done it is remembered on the connection
            # rather than read back off the link. Reading the link cannot
            # distinguish "we clamped it" from "it was born this way" — the
            # connection is created at these parameters, so a state check is
            # true on the very first request and the clamp never applies at
            # all. That bug shipped once; hence the explicit flag, which also
            # dies with the connection and so cannot outlive a handle.
            if getattr(connection, '_khp_params_pinned', False):
                manager.send_control_frame(
                    connection, cid,
                    L2CAP_Connection_Parameter_Update_Response(
                        identifier=request.identifier,
                        result=L2CAP_CONNECTION_PARAMETERS_ACCEPTED_RESULT))
                log.debug(f"[BLE] Repeat parameter request {asked} acknowledged, "
                          f"link left alone")
                return

            request.interval_min = CONN_INTERVAL_MIN
            request.interval_max = CONN_INTERVAL_MAX
            request.latency = CONN_MAX_LATENCY
            request.timeout = min(max(request.timeout, CONN_SUPERVISION_TIMEOUT),
                                  CONN_SUPERVISION_MAX)
            log.info(
                f"[BLE] Peripheral asked for interval {asked[0]}-{asked[1]}, "
                f"latency {asked[2]}, timeout {asked[3]}; granting "
                f"{request.interval_min}-{request.interval_max}, "
                f"latency {request.latency}, timeout {request.timeout}")
            connection._khp_params_pinned = True
            return handler(connection, cid, request)

        manager.on_l2cap_connection_parameter_update_request = clamped

    def _watch_connection_parameters(self, connection):
        """Log the timing actually in force, so the link can be verified."""
        def on_update():
            p = connection.parameters
            log.info(f"[BLE] Link timing now: interval {p.connection_interval:.2f}ms, "
                     f"latency {p.peripheral_latency}, "
                     f"timeout {p.supervision_timeout:.0f}ms")
        try:
            connection.on(connection.EVENT_CONNECTION_PARAMETERS_UPDATE, on_update)
        except Exception as e:
            log.debug(f"Cannot watch connection parameters: {e}")

    async def _run_ble_handler(self):
        """Handle BLE connections."""
        has_known = any(dev.address != '*' for dev in self.ble_devices)
        has_wildcard = any(dev.address == '*' for dev in self.ble_devices)

        if has_known:
            await self._run_ble_accept_list_handler()
        elif has_wildcard:
            await self._run_ble_scan_handler(set())

    def _ble_missing_addresses(self):
        """Configured BLE addresses without a live session."""
        return [normalize_addr(d.address) for d in self.ble_devices
                if d.address != '*' and normalize_addr(d.address) not in self.sessions]

    async def _seed_accept_list(self, missing):
        """Load the filter accept list with every address we may connect to."""
        await self.device.send_command(
            HCI_LE_Clear_Filter_Accept_List_Command(), check_result=True)

        known = set(missing) | {a for a in self._keystore_addresses
                                if a not in self.sessions}

        for addr_str in sorted(known):
            target = Address(addr_str)
            known_type = self._keystore_address_types.get(addr_str)
            if known_type is not None:
                entry_types = [known_type & 1]
            else:
                entry_types = [Address.PUBLIC_DEVICE_ADDRESS, Address.RANDOM_DEVICE_ADDRESS]
            for entry_type in entry_types:
                await self.device.send_command(
                    HCI_LE_Add_Device_To_Filter_Accept_List_Command(
                        address_type=entry_type,
                        address=target,
                    ), check_result=True)
        return known

    async def _run_ble_accept_list_handler(self):
        """Connect to configured BLE devices using the filter accept list."""
        log.info("[BLE] Accept-list handler running")

        while True:
            try:
                missing = self._ble_missing_addresses()
                if not missing:
                    await asyncio.sleep(self.ACTIVE_RETRY_INTERVAL)
                    continue

                known = await self._seed_accept_list(missing)

                matched_dev = None
                match_kind = None
                connection = await self._ble_initiate(self.BLE_INIT_WINDOW)

                if connection is None:
                    match = await self._ble_scan_for_rotated(known, self.BLE_SCAN_WINDOW)
                    if match:
                        target_address, matched_dev, match_kind = match
                        connection = await self._ble_initiate(
                            config.connect_timeout, peer=target_address)

                if connection is None:
                    continue

                self._admit_ble_connection(connection, matched_dev, match_kind)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"[BLE] Connection failed: {e}")
                await asyncio.sleep(2.0)

    def _admit_ble_connection(self, connection, matched_dev=None, match_kind=None):
        """Create a session for a new BLE connection, dropping duplicates."""
        addr = normalize_addr(str(connection.peer_address))
        old = self.sessions.get(addr)
        if old is not None and old.is_alive():
            log.info(f"[BLE] Duplicate connection from {addr}, dropping")
            self._track_task(asyncio.create_task(self._reject_connection(connection)))
            return

        log.info(f"[BLE] Device connected: {self._format_device(addr)}")
        session = self._new_session(addr, Protocol.BLE, connection)
        session.peer = Peer(connection)
        self._register_session(session)
        session.setup_task = self._track_task(asyncio.create_task(
            self._run_session_setup(
                session,
                self._setup_ble_session(session, matched_dev, match_kind, old))))

    async def _setup_ble_session(self, session, matched_dev=None, match_kind=None, old=None):
        """Pair or restore bonding, then bring up HID for one session."""
        if old is not None:
            await self._teardown_session(old)

        await self._ble_restore_or_pair(session)

        if match_kind == 'name' and matched_dev is not None:
            self._save_rotated_address(matched_dev, session.address)
            self._parse_devices()

        self._load_cached_descriptor(session)
        await self._setup_ble_hid(session)
        log.success(f"[BLE] {self._format_device(session.address)} receiving HID reports")

    async def _ble_initiate(self, window: float, peer: Address = None):
        """Legacy create-connection to `peer`, or to the accept list when
        None. Returns the connection, or None on window timeout."""
        pending = asyncio.get_running_loop().create_future()

        def on_connection(connection):
            if connection.transport == BT_LE_TRANSPORT and not pending.done():
                pending.set_result(connection)

        def on_failure(error):
            if getattr(error, 'transport', BT_LE_TRANSPORT) != BT_LE_TRANSPORT:
                return
            if not pending.done():
                pending.set_exception(error)

        def consume_exception(future):
            if not future.cancelled():
                future.exception()
        pending.add_done_callback(consume_exception)

        await self._radio_lock.acquire()
        self.device.on(Device.EVENT_CONNECTION, on_connection)
        self.device.on(Device.EVENT_CONNECTION_FAILURE, on_failure)

        try:
            self.device.connect_own_address_type = OwnAddressType.PUBLIC
            self.device.le_connecting = True

            await self.device.send_command(
                HCI_LE_Create_Connection_Command(
                    le_scan_interval=96,
                    le_scan_window=96,
                    initiator_filter_policy=0 if peer is not None else 1,
                    peer_address_type=peer.address_type if peer is not None else 0,
                    peer_address=peer if peer is not None else Address.ANY,
                    own_address_type=OwnAddressType.PUBLIC,
                    connection_interval_min=CONN_INTERVAL_MIN,
                    connection_interval_max=CONN_INTERVAL_MAX,
                    max_latency=CONN_MAX_LATENCY,
                    supervision_timeout=CONN_SUPERVISION_TIMEOUT,
                    min_ce_length=0,
                    max_ce_length=0,
                ), check_result=True)

            try:
                return await asyncio.wait_for(asyncio.shield(pending), timeout=window)
            except asyncio.TimeoutError:
                return None

        finally:
            if not pending.done():
                try:
                    await self.device.send_command(
                        HCI_LE_Create_Connection_Cancel_Command())
                    await asyncio.wait_for(asyncio.shield(pending), timeout=1.0)
                except Exception:
                    pass
            self.device.le_connecting = False
            self.device.remove_listener(Device.EVENT_CONNECTION, on_connection)
            self.device.remove_listener(Device.EVENT_CONNECTION_FAILURE, on_failure)
            self._radio_lock.release()

    async def _ble_scan_for_rotated(self, known: set, window: float):
        """Scan for bonded devices advertising, including from a rotated
        address. Returns (address, DeviceConfig or None, kind) or None."""
        rotated = asyncio.get_running_loop().create_future()

        def on_advertisement(advertisement):
            if rotated.done():
                return
            match = self._match_rotated_ble_device(advertisement, known)
            if match:
                rotated.set_result((advertisement.address,) + match)

        await self._radio_lock.acquire()
        self.device.on('advertisement', on_advertisement)
        scanning = False
        try:
            await self.device.start_scanning(
                legacy=True,
                own_address_type=OwnAddressType.PUBLIC,
                filter_duplicates=True,
            )
            scanning = True
            try:
                return await asyncio.wait_for(rotated, timeout=window)
            except asyncio.TimeoutError:
                return None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"[BLE] Rotation scan failed: {e}")
            return None
        finally:
            self.device.remove_listener('advertisement', on_advertisement)
            if scanning:
                try:
                    await self.device.stop_scanning(legacy=True)
                except Exception:
                    pass
            self._radio_lock.release()

    def _match_rotated_ble_device(self, advertisement, known: set):
        """Match an advertisement by known address, IRK resolution, or
        device name. Returns (DeviceConfig or None, kind) or None."""
        address = advertisement.address
        addr_norm = normalize_addr(str(address))
        if addr_norm in self.sessions:
            return None
        if addr_norm in known:
            dev = next((d for d in self.ble_devices if d.address == addr_norm), None)
            return (dev, 'known')

        if address.is_resolvable and self.device.address_resolver:
            resolved = self.device.address_resolver.resolve(address)
            if resolved:
                resolved_norm = normalize_addr(str(resolved))
                if resolved_norm in known:
                    dev = next((d for d in self.ble_devices if d.address == resolved_norm), None)
                    log.info(f"[BLE] Resolved {addr_norm} to bonded device "
                             f"{self._format_device(resolved_norm)}")
                    return (dev, 'irk')

        try:
            name = advertisement.data.get(AdvertisingData.COMPLETE_LOCAL_NAME) or \
                advertisement.data.get(AdvertisingData.SHORTENED_LOCAL_NAME)
        except UnicodeDecodeError as e:
            log.debug(f"[BLE] Ignoring malformed advertisement from {addr_norm}: {e}")
            return None
        if isinstance(name, bytes):
            name = clean_device_name(name)
        if name:
            dev = next((d for d in self.ble_devices
                        if d.name == name and d.address not in self.sessions), None)
            if dev:
                log.info(f"[BLE] {name} advertising from new address {addr_norm}")
                return (dev, 'name')

        return None

    def _save_rotated_address(self, dev, new_addr: str):
        """Save the new address and drop the device's stale entries."""
        config.add_device(new_addr, Protocol.BLE, dev.name)
        for old in self.ble_devices:
            if old.name == dev.name and old.address != new_addr:
                config.remove_device(old.address)

    async def _run_ble_scan_handler(self, target_addresses: set):
        """Fallback BLE handler using active scanning for discovery."""
        log.info("[BLE] Scanning for devices...")

        while True:
            found_device = None

            def on_advertisement(advertisement):
                nonlocal found_device
                if found_device is not None:
                    return

                addr = normalize_addr(str(advertisement.address))
                if addr in self.sessions:
                    return
                if not target_addresses or addr in target_addresses:
                    found_device = advertisement
                    log.info(f"[BLE] Found target: {addr}")

            self.device.on('advertisement', on_advertisement)

            try:
                async with self._radio_lock:
                    await self.device.start_scanning()
                    for _ in range(20):
                        if found_device:
                            break
                        await asyncio.sleep(0.5)
                    await self.device.stop_scanning()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"[BLE] Scan error: {e}")
            finally:
                self.device.remove_listener('advertisement', on_advertisement)

            if found_device:
                max_attempts = 2
                for attempt in range(1, max_attempts + 1):
                    try:
                        log.info(f"[BLE] Connecting to {found_device.address} (Attempt {attempt}/{max_attempts})...")
                        async with self._radio_lock:
                            connection = await self.device.connect(
                                found_device.address,
                                own_address_type=OwnAddressType.PUBLIC,
                                timeout=config.connect_timeout,
                            )
                        self._admit_ble_connection(connection)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning(f"[BLE] Connect attempt {attempt} failed: {e}")
                        if attempt < max_attempts:
                            await asyncio.sleep(2.0)

            await asyncio.sleep(3.0)

    async def _ble_restore_or_pair(self, session):
        """Restore BLE bonding or initiate new pairing."""
        connection = session.connection
        if connection.is_encrypted:
            log.info("[BLE] Connection already encrypted")
            return

        if self.device.keystore:
            try:
                keys = await self.device.keystore.get(str(connection.peer_address))
                if keys:
                    log.info("[BLE] Restoring bonding...")
                    await connection.encrypt()
                    log.success("[BLE] Bonding restored")
                    return
            except Exception as e:
                log.warning(f"[BLE] Bonding restore failed: {e}")

        log.info("[BLE] Initiating pairing...")
        await connection.pair()
        log.success("[BLE] Pairing complete")

    async def _setup_ble_hid(self, session):
        """Discover reports, create UHID, subscribe. Common to connect and post-pair."""
        if not session.hid_reports:
            await self._discover_ble_hid_service(session, process_reports=True)
        if not session.report_map:
            raise InvalidStateError("[BLE] No report descriptor available")
        self._create_uhid_device(session)
        await self._subscribe_to_ble_reports(session)
        await self._ble_activate_hid_service(session)

    async def _read_ble_device_name(self, session):
        """Read BLE device name from Generic Access Service."""
        try:
            for service in session.peer.services:
                if service.uuid == GATT_GENERIC_ACCESS_SERVICE:
                    await session.peer.discover_characteristics(service=service)
                    for char in service.characteristics:
                        if char.uuid == GATT_DEVICE_NAME_CHARACTERISTIC:
                            value = await session.peer.read_value(char)
                            session.name = clean_device_name(bytes(value))
                            log.info(f"[BLE] Device name: {session.name}")
                            return
        except Exception as e:
            log.warning(f"[BLE] Could not read device name: {e}")

    async def _process_ble_report_char(self, session, char):
        """Process a BLE Report characteristic."""
        await session.peer.discover_descriptors(characteristic=char)

        report_id = 0
        report_type = HID_REPORT_TYPE_INPUT

        for desc in char.descriptors:
            if desc.type == GATT_REPORT_REFERENCE_DESCRIPTOR:
                try:
                    ref = await session.peer.read_value(desc)
                    if len(ref) >= 2:
                        report_id = ref[0]
                        report_type = ref[1]
                except Exception:
                    pass

        if report_type == HID_REPORT_TYPE_INPUT:
            session.hid_reports.append((report_id, char))
            log.info(f"[BLE] Found input report {report_id}")

    async def _subscribe_to_ble_reports(self, session):
        """Subscribe to BLE HID input report notifications."""
        for report_id, char in session.hid_reports:
            try:
                def make_callback(rid):
                    return lambda value: self._on_ble_hid_report(session, value, rid)

                await session.peer.subscribe(char, make_callback(report_id))
                log.success(f"[BLE] Subscribed to report {report_id}")
            except Exception as e:
                log.warning(f"[BLE] Failed to subscribe to report {report_id}: {e}")

    async def _ble_activate_hid_service(self, session):
        """Write Exit Suspend to HID Control Point and force Report Protocol Mode."""
        peer = session.peer
        if not peer:
            log.warning("[BLE] No peer for HID activation")
            return

        hid_services = [s for s in peer.services if s.uuid == GATT_HUMAN_INTERFACE_DEVICE_SERVICE]
        if not hid_services:
            log.warning("[BLE] No HID service found for activation")
            return

        hid_service = hid_services[0]
        if not hid_service.characteristics:
            log.info("[BLE] Discovering characteristics for HID activation...")
            await peer.discover_characteristics(service=hid_service)

        found_cp = False
        for char in hid_service.characteristics:
            if char.uuid == GATT_HID_CONTROL_POINT_CHARACTERISTIC:
                found_cp = True
                try:
                    await peer.write_value(char, bytes([0x01]), with_response=False)
                    log.info("[BLE] Wrote Exit Suspend to HID Control Point")
                except Exception as e:
                    log.warning(f"[BLE] Failed to write HID Control Point: {e}")

            elif char.uuid == GATT_PROTOCOL_MODE_CHARACTERISTIC:
                try:
                    value = await peer.read_value(char)
                    mode = "Report" if bytes(value) == b'\x01' else "Boot"
                    log.info(f"[BLE] Protocol Mode: {mode}")
                    if bytes(value) != b'\x01':
                        await peer.write_value(char, bytes([0x01]), with_response=False)
                        log.info("[BLE] Forced Report Protocol Mode")
                except Exception as e:
                    log.warning(f"[BLE] Protocol Mode read/write failed: {e}")

        if not found_cp:
            log.info(f"[BLE] No HID Control Point characteristic (found {len(hid_service.characteristics)} chars)")

    def _on_ble_hid_report(self, session, value, report_id):
        """Handle BLE HID report."""
        self._forward_report(session, bytes([report_id]) + bytes(value))

    async def _pair_ble(self, address: str) -> bool:
        """Pair with a BLE device."""
        log.info(f"[BLE] Pairing with {address}...")

        target = Address(address)
        try:
            connection = await self.device.connect(
                target,
                own_address_type=OwnAddressType.PUBLIC,
                timeout=config.connect_timeout,
            )
        except Exception as e:
            log.error(f"[BLE] Connection failed: {e}")
            return False

        session = self._new_session(normalize_addr(address), Protocol.BLE, connection)
        session.peer = Peer(connection)
        self._pairing_session = session
        log.success(f"[BLE] Connected to {address}")

        try:
            log.info("[BLE] Initiating pairing...")
            await connection.pair()
            log.success("[BLE] Pairing complete!")

            if not connection.is_encrypted:
                raise InvalidStateError("[BLE] Link not encrypted after pairing")

            await self._discover_ble_hid_service(session)

            return True
        except Exception as e:
            log.error(f"[BLE] Pairing failed: {e}")
            await session.cleanup()
            self._pairing_session = None
            return False

    async def _discover_ble_hid_service(self, session, process_reports: bool = False):
        """Discover BLE GATT HID service and cache descriptor."""
        peer = session.peer
        await peer.discover_services()

        if not session.name:
            await self._read_ble_device_name(session)

        hid_services = [s for s in peer.services if s.uuid == GATT_HUMAN_INTERFACE_DEVICE_SERVICE]
        if not hid_services:
            if process_reports:
                raise InvalidStateError("[BLE] HID service not found")
            log.warning("[BLE] HID service not found")
            return

        hid_service = hid_services[0]
        log.success("[BLE] Found HID service")

        await peer.discover_characteristics(service=hid_service)

        for char in hid_service.characteristics:
            if char.uuid == GATT_REPORT_MAP_CHARACTERISTIC and not session.report_map:
                try:
                    value = await peer.read_value(char)
                    session.report_map = bytes(value)
                    log.success(f"[BLE] Got descriptor: {len(session.report_map)} bytes")

                    self.device_cache.save(session.address, {
                        'report_map': session.report_map.hex(),
                        'device_name': session.name
                    })
                except Exception as e:
                    log.warning(f"[BLE] Failed to read report map: {e}")

            elif process_reports and char.uuid == GATT_REPORT_CHARACTERISTIC:
                await self._process_ble_report_char(session, char)

    async def _continue_ble_after_pairing(self, session):
        """Continue BLE connection after pairing."""
        if not session.peer:
            session.peer = Peer(session.connection)
        await self._ble_restore_or_pair(session)
        await self._setup_ble_hid(session)
