#!/usr/bin/env python3
"""Classic Bluetooth HID handler mixin for HIDHost."""

import asyncio
from typing import List

from bumble.core import BT_BR_EDR_TRANSPORT, BT_HUMAN_INTERFACE_DEVICE_SERVICE, InvalidStateError, TimeoutError as BumbleTimeoutError
from bumble.hci import (
    Address,
    HCI_Write_Scan_Enable_Command,
    Role,
)
from bumble.hid import HID_CONTROL_PSM, HID_INTERRUPT_PSM, Message
from bumble.hid import Host as BumbleHIDHost
from bumble.sdp import Client as SDPClient

from config import Protocol, config, normalize_addr, clean_device_name
from logging_utils import log

FALLBACK_HID_DESCRIPTOR = bytes([
    0x05, 0x01, 0x09, 0x05, 0xa1, 0x01, 0x85, 0x01,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35,
    0x16, 0x00, 0x00, 0x26, 0xff, 0xff, 0x75, 0x10, 0x95, 0x04, 0x81, 0x02,
    0x05, 0x02, 0x09, 0xc5, 0x09, 0xc4,
    0x16, 0x00, 0x00, 0x26, 0xff, 0x03, 0x75, 0x10, 0x95, 0x02, 0x81, 0x02,
    0x05, 0x01, 0x09, 0x39, 0x15, 0x01, 0x25, 0x08,
    0x35, 0x00, 0x46, 0x3b, 0x01, 0x65, 0x14, 0x75, 0x08, 0x95, 0x01, 0x81, 0x42,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x10,
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x10, 0x81, 0x02,
    0xc0,
])


class ClassicMixin:
    """Classic Bluetooth methods for HIDHost."""

    async def _run_classic_handler(self):
        """Handle Classic Bluetooth connections."""
        if hasattr(self, '_classic_connection_listener') and self._classic_connection_listener:
            try:
                self.device.remove_listener('connection', self._classic_connection_listener)
            except Exception:
                pass
            self._classic_connection_listener = None

        self.hid_host = BumbleHIDHost(self.device)
        self.hid_host.on(BumbleHIDHost.EVENT_INTERRUPT_DATA, self._on_classic_interrupt_data)
        self.hid_host.on(BumbleHIDHost.EVENT_VIRTUAL_CABLE_UNPLUG, self._on_virtual_cable_unplug)
        log.info(f"[Classic] HID Host ready (PSM 0x{HID_CONTROL_PSM:04X}, 0x{HID_INTERRUPT_PSM:04X})")

        log.info("[Classic] Enabling Page Scan...")
        await self.device.host.send_command(
            HCI_Write_Scan_Enable_Command(scan_enable=0x02),
            check_result=True
        )

        async def on_classic_connection(connection):
            if self._connection_future.done():
                log.info("[Classic] Connection received but another protocol won")
                try:
                    await connection.disconnect()
                except Exception:
                    pass
                return

            if not self.hid_host:
                log.warning("[Classic] Connection received but hid_host not ready, ignoring")
                try:
                    await connection.disconnect()
                except Exception:
                    pass
                return

            addr_str = str(connection.peer_address)
            log.info(f"[Classic] Device connected: {self._format_device(addr_str)}")

            if not self._is_classic_allowed(addr_str):
                log.warning(f"[Classic] Rejecting {addr_str} (not allowed)")
                try:
                    await connection.disconnect()
                except Exception:
                    pass
                return

            if self.connection is not None and self.connection is not connection:
                try:
                    self.connection.remove_listener('disconnection', self._on_disconnection)
                except Exception:
                    pass

            self._disconnection_event.clear()

            conn_lost = asyncio.Event()

            def on_conn_lost(reason):
                conn_lost.set()

            self.connection = connection
            self.current_device_address = addr_str
            self.connected_protocol = Protocol.CLASSIC
            connection.on('disconnection', on_conn_lost)
            connection.on('disconnection', self._on_disconnection)

            self.hid_host.on_device_connection(connection)

            if connection.role != Role.CENTRAL:
                log.info("[Classic] Requesting role switch to central...")
                try:
                    await asyncio.wait_for(connection.switch_role(Role.CENTRAL), timeout=5.0)
                    log.success("[Classic] Role switch complete, now central")
                except Exception as e:
                    log.warning(f"[Classic] Role switch failed: {e!r}")

            if conn_lost.is_set():
                log.warning("[Classic] Connection lost during role switch")
                return

            if not connection.is_encrypted:
                log.info("[Classic] Restoring bonding (authenticate + encrypt)...")
                try:
                    await asyncio.wait_for(connection.authenticate(), timeout=5.0)
                    await asyncio.wait_for(connection.encrypt(enable=True), timeout=5.0)
                    log.success("[Classic] Bonding restored")
                except Exception as e:
                    log.warning(f"[Classic] Bonding restore failed: {e!r}")

            if conn_lost.is_set():
                log.warning("[Classic] Connection lost during authentication")
                return

            if not self.hid_host.l2cap_ctrl_channel:
                log.info("[Classic] Connecting to HID control channel...")
                try:
                    await asyncio.wait_for(self.hid_host.connect_control_channel(), timeout=5.0)
                    log.success("[Classic] HID control channel connected")
                except Exception as e:
                    log.warning(f"[Classic] Control channel: {e!r}")

            if conn_lost.is_set():
                log.warning("[Classic] Connection lost during channel setup")
                return

            if not self.hid_host.l2cap_intr_channel:
                log.info("[Classic] Connecting to HID interrupt channel...")
                try:
                    await asyncio.wait_for(self.hid_host.connect_interrupt_channel(), timeout=5.0)
                    log.success("[Classic] HID interrupt channel connected")
                except Exception as e:
                    log.warning(f"[Classic] Interrupt channel: {e!r}")

            if conn_lost.is_set():
                log.warning("[Classic] Connection lost during channel setup")
                return

            if not self.hid_host.l2cap_intr_channel:
                log.warning("[Classic] HID interrupt channel failed to connect, dropping link")
                try:
                    await connection.disconnect()
                except Exception:
                    pass
                self.connection = None
                self.current_device_address = None
                self.connected_protocol = None
                if not self._connection_future.done():
                    self._connection_future.set_exception(
                        InvalidStateError("[Classic] HID channels not opened by peer")
                    )
                return

            self._classic_set_report_protocol()

            if not self._connection_future.done():
                self._connection_future.set_result(connection)

        def on_connection_event(connection):
            is_classic = (hasattr(connection, 'transport')
                          and connection.transport == BT_BR_EDR_TRANSPORT) \
                          or not hasattr(connection, 'transport')
            if not is_classic:
                return
            prev_task = getattr(self, '_classic_conn_task', None)
            if prev_task is not None and not prev_task.done():
                prev_task.cancel()
            task = asyncio.create_task(on_classic_connection(connection))
            self._classic_conn_task = task
            self._connection_tasks.add(task)
            task.add_done_callback(self._connection_tasks.discard)

        self._classic_connection_listener = on_connection_event
        self.device.on('connection', on_connection_event)

        active_addresses = [d.address for d in self.classic_devices if d.address != '*']
        if active_addresses:
            await self._classic_active_connect_loop(active_addresses)

    def _is_classic_allowed(self, addr_str: str) -> bool:
        """Check if Classic address is allowed."""
        norm_addr = normalize_addr(addr_str)

        for dev in self.classic_devices:
            if dev.address == '*':
                return True
            if dev.address == norm_addr:
                return True

        if norm_addr in self._keystore_addresses:
            return True

        return False

    async def _classic_active_connect_loop(self, addresses: List[str]):
        """Actively try to connect to Classic devices."""
        log.info(f"[Classic] Active: {len(addresses)} device(s)")
        await asyncio.sleep(self.ACTIVE_DELAY)

        attempt = 0
        while not self._connection_future.done():
            attempt += 1
            for addr in addresses:
                if self._connection_future.done():
                    return
                if self.connection is not None:
                    continue

                log.info(f"[Classic] Attempt {attempt}: {self._format_device(addr)}")

                target = Address(addr, Address.PUBLIC_DEVICE_ADDRESS)
                await self._radio_lock.acquire()
                connect_task = asyncio.create_task(
                    self.device.connect(target, transport=BT_BR_EDR_TRANSPORT)
                )
                # The finally below guarantees connect_task is cancelled and
                # awaited on every exit path, including suspend cancellation —
                # otherwise it leaks and asyncio logs an unretrieved exception
                # when bumble eventually raises HCI_PAGE_TIMEOUT.
                try:
                    timed_out = True
                    for _ in range(self.ACTIVE_CONNECT_TIMEOUT):
                        if self._connection_future.done():
                            return
                        done, _ = await asyncio.wait([connect_task], timeout=0.5)
                        if done:
                            timed_out = False
                            break

                    if timed_out:
                        log.info(f"[Classic] {addr} timed out")
                        await asyncio.sleep(3.0)
                        continue

                    await connect_task

                except Exception as e:
                    msg = str(e)
                    if "CONNECTION_ALREADY_EXISTS" in msg:
                        log.info("[Classic] Peer paged us first; yielding to incoming link")
                        await asyncio.sleep(2.0)
                    elif "DISALLOWED" in msg or "PENDING" in msg:
                        log.warning("[Classic] HCI busy, waiting...")
                        await asyncio.sleep(5.0)
                    else:
                        log.info(f"[Classic] Connect failed: {e}")
                        await asyncio.sleep(2.0)

                finally:
                    if not connect_task.done():
                        connect_task.cancel()
                    try:
                        await connect_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    self._radio_lock.release()

            if not self._connection_future.done():
                await asyncio.sleep(self.ACTIVE_RETRY_INTERVAL)

    def _classic_set_report_protocol(self):
        """Send HIDP SET_PROTOCOL(Report) on the control channel."""
        if not self.hid_host or not self.hid_host.l2cap_ctrl_channel:
            return
        try:
            self.hid_host.set_protocol(Message.ProtocolMode.REPORT_PROTOCOL)
            log.info("[Classic] Sent SET_PROTOCOL (Report)")
        except Exception as e:
            log.warning(f"[Classic] SET_PROTOCOL failed: {e}")

    def _finalize_classic_hid(self):
        """Apply fallback descriptor if needed and create UHID."""
        if not self.report_map:
            self.report_map = FALLBACK_HID_DESCRIPTOR
            log.warning("[Classic] Using fallback descriptor")
        self._create_uhid_device()

    async def _handle_classic_connection(self):
        """Finalize Classic connection setup."""
        if not self.hid_host.l2cap_intr_channel:
            raise InvalidStateError("HID interrupt channel not connected")

        if not self._load_cached_descriptor():
            await self._query_classic_sdp()

        self._finalize_classic_hid()

    def _parse_hid_descriptor_list(self, data_element):
        """Parse HID Descriptor List from SDP."""
        try:
            if hasattr(data_element, 'value'):
                data_element = data_element.value

            if isinstance(data_element, (list, tuple)):
                for descriptor in data_element:
                    if hasattr(descriptor, 'value'):
                        descriptor = descriptor.value

                    if isinstance(descriptor, (list, tuple)) and len(descriptor) >= 2:
                        desc_type = descriptor[0]
                        desc_data = descriptor[1]

                        if hasattr(desc_type, 'value'):
                            desc_type = desc_type.value

                        if desc_type == 0x22:  # Report Descriptor
                            if hasattr(desc_data, 'value'):
                                desc_data = desc_data.value

                            if isinstance(desc_data, bytes):
                                self.report_map = desc_data
                            elif isinstance(desc_data, (list, tuple)):
                                self.report_map = bytes(desc_data)

                            log.success(f"[Classic] Got descriptor: {len(self.report_map)} bytes")
                            return
        except Exception as e:
            log.warning(f"[Classic] Failed to parse descriptor: {e}")

    def _on_classic_interrupt_data(self, pdu: bytes):
        """Handle Classic HID report."""
        if len(pdu) < 1:
            return
        if (pdu[0] >> 4) != Message.MessageType.DATA or \
                (pdu[0] & 0x0F) != Message.ReportType.INPUT_REPORT:
            log.debug(f"[Classic] Ignoring non-input interrupt PDU: 0x{pdu[0]:02X}")
            return
        self._forward_report(pdu[1:])

    def _on_virtual_cable_unplug(self):
        """Handle virtual cable unplug."""
        log.warning("[Classic] Virtual cable unplugged")
        self._virtual_cable_unplug_address = self.current_device_address
        self._disconnection_event.set()

    async def _pair_classic(self, address: str) -> bool:
        """Pair with a Classic Bluetooth device."""
        log.info(f"[Classic] Pairing with {address}...")

        try:
            target_address = Address(address, Address.PUBLIC_DEVICE_ADDRESS)
            self.connection = await self.device.connect(
                target_address,
                transport=BT_BR_EDR_TRANSPORT,
                timeout=config.connect_timeout,
            )
            log.success(f"[Classic] Connected to {address}")
        except (asyncio.TimeoutError, BumbleTimeoutError):
            log.error(f"[Classic] Connection timeout after {config.connect_timeout}s")
            return False
        except Exception as e:
            log.error(f"[Classic] Connection failed: {e}")
            return False

        self.current_device_address = address
        self.connected_protocol = Protocol.CLASSIC

        link_key_received = asyncio.Event()

        def on_device_link_key(_bd_addr, link_key, key_type):
            log.success(f"[Classic] Link key received: type={key_type}")
            link_key_received.set()

        self.device.host.on('link_key', on_device_link_key)

        try:
            log.info("[Classic] Authenticating...")
            try:
                await asyncio.wait_for(self.connection.authenticate(), timeout=30.0)
                log.success("[Classic] Authentication complete")
            except Exception as e:
                log.warning(f"[Classic] Authentication: {e!r}")

            log.info("[Classic] Waiting for link key...")
            try:
                await asyncio.wait_for(link_key_received.wait(), timeout=5.0)
                log.success("[Classic] Link key saved")
            except asyncio.TimeoutError:
                log.warning("[Classic] Link key event timeout (may already be saved)")

            if not self.connection.is_encrypted:
                log.info("[Classic] Requesting encryption...")
                try:
                    await asyncio.wait_for(
                        self.connection.encrypt(enable=True),
                        timeout=10.0
                    )
                except Exception as e:
                    log.warning(f"[Classic] Encryption: {e!r}")

            await self._query_classic_sdp(address)

            if self.keystore:
                keys = await self.keystore.get(str(self.connection.peer_address))
                if keys and keys.link_key:
                    log.success("[Classic] Link key verified")
                else:
                    log.warning("[Classic] Link key not found in keystore!")

            self.device.host.remove_listener('link_key', on_device_link_key)
            return True

        except Exception as e:
            log.error(f"[Classic] Pairing failed: {e}")
            self.device.host.remove_listener('link_key', on_device_link_key)
            if self.connection:
                try:
                    await self.connection.disconnect()
                except Exception:
                    pass
                self.connection = None
            return False

    async def _query_classic_sdp(self, address: str = None):
        """Query SDP for HID descriptor and cache it."""
        if not self.connection:
            return

        address = address or self.current_device_address

        log.info("[Classic] Querying SDP...")
        try:
            sdp_client = SDPClient(self.connection)
            await asyncio.wait_for(sdp_client.connect(), timeout=5.0)

            result = await asyncio.wait_for(
                sdp_client.search_attributes(
                    [BT_HUMAN_INTERFACE_DEVICE_SERVICE],
                    [0x0100, 0x0206]
                ),
                timeout=10.0
            )

            if result:
                for record in result:
                    for attr in record:
                        if hasattr(attr, 'id') and attr.id == 0x0206:
                            self._parse_hid_descriptor_list(attr.value)
                        elif hasattr(attr, 'id') and attr.id == 0x0100:
                            try:
                                name = clean_device_name(attr.value.value)
                                if name:
                                    self.device_name = name
                            except Exception:
                                pass

            await sdp_client.disconnect()

            if self.report_map:
                self.device_cache.save(address, {
                    'report_map': self.report_map.hex(),
                    'device_name': self.device_name or 'Unknown'
                })
                log.success(f"[Classic] Cached descriptor ({len(self.report_map)} bytes)")
        except Exception as e:
            log.warning(f"[Classic] SDP query failed: {e}")

    async def _continue_classic_after_pairing(self):
        """Continue Classic connection after pairing."""
        self.hid_host = BumbleHIDHost(self.device)
        self.hid_host.on(BumbleHIDHost.EVENT_INTERRUPT_DATA, self._on_classic_interrupt_data)
        self.hid_host.on(BumbleHIDHost.EVENT_VIRTUAL_CABLE_UNPLUG, self._on_virtual_cable_unplug)
        log.info("[Classic] HID Host created")

        self.hid_host.on_device_connection(self.connection)

        log.info("[Classic] Connecting to HID control channel...")
        try:
            await asyncio.wait_for(self.hid_host.connect_control_channel(), timeout=5.0)
            log.success("[Classic] HID control channel connected")
        except Exception as e:
            log.warning(f"[Classic] Control channel: {e!r}")

        log.info("[Classic] Connecting to HID interrupt channel...")
        try:
            await asyncio.wait_for(self.hid_host.connect_interrupt_channel(), timeout=5.0)
            log.success("[Classic] HID interrupt channel connected")
        except Exception as e:
            log.warning(f"[Classic] Interrupt channel: {e!r}")

        if not self.hid_host.l2cap_intr_channel:
            log.error("[Classic] Failed to connect HID interrupt channel")
            return

        self._classic_set_report_protocol()
        self._finalize_classic_hid()
