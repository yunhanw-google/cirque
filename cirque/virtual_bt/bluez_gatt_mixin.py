# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Userspace BlueZ-compatible D-Bus GATT mixin (not bluetoothd).

Provides peripheral SEQPACKET socket bridge and Device1/GattCharacteristic1
handlers for userspace BlueZ emulation.
"""

import logging
import os
import struct
import threading
import time
from typing import Any, Dict, Optional

from cirque.virtual_bt.bluez_gatt_helpers import (
    acquire_gatt_characteristic_fd,
    build_managed_objects_dict,
)
from cirque.virtual_bt.docker_hci_bridge import VirtualBluezAdapterBridge

logger = logging.getLogger('VirtualBtBluezDbus')


class BluezGattPeripheralMixin:
  """Mixin providing GATT peripheral SEQPACKET bridge and Device1 handlers."""

  def _build_managed_objects_dict(self) -> dict:
    """Builds the GetManagedObjects dictionary for all virtual adapters."""
    with self.docker_manager._lock:  # pylint: disable=protected-access
      return build_managed_objects_dict(self.docker_manager.adapter_bridges)

  def _emit_gatt_tree_added(self, base_path: str, objs: Dict[str, Any]) -> None:
    """Emits InterfacesAdded for a device and its GATT service/char paths."""
    from gi.repository import GLib  # pylint: disable=g-import-not-at-top

    for path in (
        base_path,
        f'{base_path}/service00',
        f'{base_path}/service00/char00',
        f'{base_path}/service00/char01',
    ):
      if path in objs:
        self._emit_all(
            '/',
            'org.freedesktop.DBus.ObjectManager',
            'InterfacesAdded',
            GLib.Variant('(oa{sa{sv}})', (path, objs[path])),
        )

  def _on_peripheral_discovered(
      self, controller_id: str, addr: str, _info: Dict[str, object]
  ) -> None:
    """Emits InterfacesAdded and RSSI changes when a device is discovered."""
    dev_path = self._export_device_gatt_tree(controller_id, addr)
    if not self._running:
      return
    is_first_discovery = False
    with self._lock:
      discovered = getattr(self, '_discovered_dev_paths', None)
      if discovered is not None and dev_path not in discovered:
        discovered.add(dev_path)
        is_first_discovery = True

    try:
      from gi.repository import GLib  # pylint: disable=g-import-not-at-top

      objs = self._build_managed_objects_dict()
      if is_first_discovery:
        self._emit_gatt_tree_added(dev_path, objs)
      if dev_path in objs and 'org.bluez.Device1' in objs[dev_path]:
        dev_props = objs[dev_path]['org.bluez.Device1']
        changed = {
            'RSSI': dev_props['RSSI'],
            'ServiceData': dev_props['ServiceData'],
        }
        self._emit_all(
            dev_path,
            'org.freedesktop.DBus.Properties',
            'PropertiesChanged',
            GLib.Variant('(sa{sv}as)', ('org.bluez.Device1', changed, [])),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
      logger.debug('InterfacesAdded emit failed: %s', exc)

  @staticmethod
  def _enable_peripheral_c2_cccd(p_bridge) -> None:
    """Enables indications (0x0002) on the peripheral's C2 CCCD handle."""
    db = p_bridge.att_server.database
    c2_info = db.find_characteristic_by_uuid(
        '18ee2ef5-263d-4559-959f-4f9c429f9d12'
    )
    if c2_info is None:
      return
    cccd_h = db.find_cccd_for_char(c2_info[1])
    if cccd_h is not None:
      p_bridge.att_server.cccd_states[cccd_h] = 0x0002

  def _notify_peripheral_c2_subscribers(
      self, bridge, peer_addr: str
  ) -> None:
    """Worker that enables C2 indications and acquires notify sockets."""
    time.sleep(0.05)
    for p_cid, p_bridge in list(self.docker_manager.adapter_bridges.items()):
      if p_bridge.bd_addr.upper() != peer_addr.upper():
        continue
      self._enable_peripheral_c2_cccd(p_bridge)
      if p_cid in self._get_c1_written_set():
        self._acquire_peripheral_c2_notify(p_cid, bridge.bd_addr)

  def _get_c1_written_set(self) -> set:
    """Returns the per-instance set of controllers with an active C1 write."""
    written = getattr(self, '_c1_written_controllers', None)
    if written is None:
      written = set()
      self._c1_written_controllers = written
    return written

  def _resolve_peripheral_peer_addr(self, controller_id: str) -> str:
    """Resolves the Central peer BD_ADDR connected to controller_id."""
    bridge = self.docker_manager.adapter_bridges.get(controller_id)
    peer_addr = 'AA:BB:CC:DD:EE:01'
    if bridge and bridge.connected_handles:
      peer_addr = next(iter(bridge.connected_handles.values()))
    else:
      for other_cid, other_b in self.docker_manager.adapter_bridges.items():
        if other_cid != controller_id:
          peer_addr = other_b.bd_addr
          break
    return peer_addr

  def _on_peripheral_c1_write(self, controller_id: str, data: bytes) -> None:
    """Forwards an H4 TCP C1 ATT Write payload into peripheral socketpair."""
    peer_addr = self._resolve_peripheral_peer_addr(controller_id)
    if controller_id not in self._peripheral_c1_fds:
      self._acquire_peripheral_gatt_sockets(controller_id, peer_addr)
    deadline = time.time() + 1.5
    while time.time() < deadline:
      fd = self._peripheral_c1_fds.get(controller_id)
      if fd is not None:
        try:
          os.write(fd, data)
        except OSError as exc:
          logger.debug('write to c1_fd failed: %s', exc)
        self._get_c1_written_set().add(controller_id)
        bridge = self.docker_manager.adapter_bridges.get(controller_id)
        cccd_ready = bool(
            bridge is None
            or any(v != 0 for v in bridge.att_server.cccd_states.values())
        )
        if cccd_ready:

          def _deferred_c2_notify():
            time.sleep(0.05)
            self._acquire_peripheral_c2_notify(controller_id, peer_addr)

          threading.Thread(target=_deferred_c2_notify, daemon=True).start()
        return
      time.sleep(0.02)

  def _on_peripheral_cccd_write(
      self, controller_id: str, attr_handle: int, cccd_val: int
  ) -> None:
    """Triggers AcquireNotify after Central enables C2 CCCD indications."""
    del attr_handle
    if cccd_val == 0 or controller_id not in self._get_c1_written_set():
      return
    peer_addr = self._resolve_peripheral_peer_addr(controller_id)

    def _deferred_c2_notify():
      time.sleep(0.05)
      self._acquire_peripheral_c2_notify(controller_id, peer_addr)

    threading.Thread(target=_deferred_c2_notify, daemon=True).start()

  def _on_central_c2_indication(self, controller_id: str, data: bytes) -> None:
    """Emits PropertiesChanged('Value') on char01 when C2 indication arrives."""
    bridge = self.docker_manager.adapter_bridges.get(controller_id)
    if not self._running or bridge is None or not bridge.connected_handles:
      return
    peer_addr = next(iter(bridge.connected_handles.values()))
    c2_path = (
        f'/org/bluez/{controller_id}/dev_{peer_addr.replace(":", "_")}'
        '/service00/char01'
    )
    try:
      from gi.repository import GLib  # pylint: disable=g-import-not-at-top

      self._emit_all(
          c2_path,
          'org.freedesktop.DBus.Properties',
          'PropertiesChanged',
          GLib.Variant(
              '(sa{sv}as)',
              (
                  'org.bluez.GattCharacteristic1',
                  {'Value': GLib.Variant('ay', list(data))},
                  [],
              ),
          ),
      )
    except Exception as exc:  # pylint: disable=broad-exception-caught
      logger.debug('c2 indication emit notice: %s', exc)

  def _on_peripheral_c2_confirm(self, controller_id: str) -> None:
    """Delivers real 1-byte ATT indication confirmation to peripheral c2_fd."""
    fd = self._peripheral_c2_fds.get(controller_id)
    if fd is not None:
      try:
        os.write(fd, b'\x01')
      except OSError as exc:
        logger.debug('write to c2_fd confirm failed: %s', exc)

  def _acquire_peripheral_gatt_sockets(
      self, periph_cid: str, central_bd_addr: str
  ) -> None:
    """Acquires C1 write SEQPACKET FD from peripheral app.

    Matter's Linux BlueZ peripheral (`BluezEndpoint.cpp`) exports
    `org.bluez.GattCharacteristic1.AcquireWrite` on `/service/c1`, returning a
    Unix `SOCK_SEQPACKET` file descriptor via D-Bus `GUnixFDList`. Writing raw
    BTP v4 frames into `c1_fd` delivers them directly into Matter's
    `BLEManagerImpl` event loop with sub-millisecond latency.
    """
    app_entry = self._registered_apps.get(periph_cid)
    app_conn = self._registered_app_conns.get(periph_cid, self._conn)
    if not self._running or not app_entry or app_conn is None:
      return
    sender, app_path = app_entry
    periph_dev_path = self._export_device_gatt_tree(periph_cid, central_bd_addr)
    try:
      from gi.repository import GLib  # pylint: disable=g-import-not-at-top

      objs = self._build_managed_objects_dict()
      if periph_dev_path in objs:
        self._emit_all(
            '/',
            'org.freedesktop.DBus.ObjectManager',
            'InterfacesAdded',
            GLib.Variant(
                '(oa{sa{sv}})', (periph_dev_path, objs[periph_dev_path])
            ),
        )
        self._emit_all(
            periph_dev_path,
            'org.freedesktop.DBus.Properties',
            'PropertiesChanged',
            GLib.Variant(
                '(sa{sv}as)',
                (
                    'org.bluez.Device1',
                    {
                        'Connected': GLib.Variant('b', True),
                        'ServicesResolved': GLib.Variant('b', True),
                    },
                    [],
                ),
            ),
        )
      if periph_cid not in self._peripheral_c1_fds:
        c1_fd = acquire_gatt_characteristic_fd(
            app_conn,
            sender,
            f'{app_path}/service/c1',
            'AcquireWrite',
            periph_dev_path,
        )
        if c1_fd is not None:
          self._peripheral_c1_fds[periph_cid] = c1_fd
    except Exception as exc:  # pylint: disable=broad-exception-caught
      logger.debug('AcquireWrite skipped: %s', exc)

  def _acquire_peripheral_c2_notify(
      self, periph_cid: str, central_bd_addr: str
  ) -> None:
    """Acquires C2 notify SEQPACKET FD from peripheral app upon StartNotify."""
    app_entry = self._registered_apps.get(periph_cid)
    app_conn = self._registered_app_conns.get(periph_cid, self._conn)
    if (
        not self._running
        or periph_cid in self._peripheral_c2_fds
        or not app_entry
        or app_conn is None
    ):
      return
    sender, app_path = app_entry
    periph_dev_path = (
        f'/org/bluez/{periph_cid}/dev_{central_bd_addr.replace(":", "_")}'
    )
    c2_fd = acquire_gatt_characteristic_fd(
        app_conn,
        sender,
        f'{app_path}/service/c2',
        'AcquireNotify',
        periph_dev_path,
    )
    if c2_fd is not None:
      self._peripheral_c2_fds[periph_cid] = c2_fd
      threading.Thread(
          target=self._pump_peripheral_c2_fd,
          args=(periph_cid, c2_fd),
          daemon=True,
      ).start()

  def _pump_peripheral_c2_fd(self, periph_cid: str, c2_fd: int) -> None:
    """Reads indications from peripheral C2 fd and sends over H4 TCP ACL."""
    bridge = self.docker_manager.adapter_bridges.get(periph_cid)
    while self._running and bridge is not None:
      try:
        data = os.read(c2_fd, 512)
        if not data:
          break
        if bridge.connected_handles:
          handle = next(iter(bridge.connected_handles.keys()))
          if (
              bridge.h4_client
              and bridge.h4_client._connections
              and handle not in bridge.h4_client._connections
          ):
            handle = next(iter(bridge.h4_client._connections.keys()))
          bridge.send_c2_indication(handle, data)
      except (BlockingIOError, InterruptedError):
        time.sleep(0.005)
      except OSError:
        break

  @staticmethod
  def _resolve_peripheral_conn_handle(p_bridge, central_bd_addr: str) -> int:
    """Resolves the active connection handle on `p_bridge` for central."""
    if not p_bridge.h4_client:
      return 0x0040
    target_addr = central_bd_addr.upper()
    with p_bridge.h4_client._cv:  # pylint: disable=protected-access
      conns = (
          p_bridge.h4_client._connections  # pylint: disable=protected-access
      )
      for ch, cdata in conns.items():
        if str(cdata.get('peer_bd_addr', '')).upper() == target_addr:
          return ch
      if conns:
        return next(iter(conns.keys()))
    return 0x0040

  def _connect_device_worker(
      self,
      cid: str,
      bridge: VirtualBluezAdapterBridge,
      object_path: str,
      invocation,
  ) -> None:
    """Establishes an LE connection and emits BlueZ connected signals."""
    from gi.repository import GLib  # pylint: disable=g-import-not-at-top

    parts = object_path.split('/')
    dev_part = parts[4] if len(parts) > 4 else parts[-1]
    peer_addr = dev_part.replace('dev_', '').replace('_', ':')
    try:
      bridge.connect_to_peripheral(peer_addr)
      for p_cid, p_bridge in list(self.docker_manager.adapter_bridges.items()):
        if p_bridge.bd_addr.upper() != peer_addr.upper():
          continue
        p_bridge.discovered_peripherals[bridge.bd_addr] = {
            'bd_addr': bridge.bd_addr,
            'rssi': -45,
            'discriminator': 0,
        }
        p_handle = self._resolve_peripheral_conn_handle(
            p_bridge, bridge.bd_addr
        )
        p_bridge.connected_handles[p_handle] = bridge.bd_addr
        self._acquire_peripheral_gatt_sockets(p_cid, bridge.bd_addr)
      self._export_device_gatt_tree(cid, peer_addr)
      objs = self._build_managed_objects_dict()
      self._emit_gatt_tree_added(object_path, objs)
      self._emit_all(
          object_path,
          'org.freedesktop.DBus.Properties',
          'PropertiesChanged',
          GLib.Variant(
              '(sa{sv}as)',
              (
                  'org.bluez.Device1',
                  {
                      'Connected': GLib.Variant('b', True),
                      'ServicesResolved': GLib.Variant('b', True),
                  },
                  [],
              ),
          ),
      )
      invocation.return_value(None)
    except Exception as exc:  # pylint: disable=broad-exception-caught
      invocation.return_dbus_error('org.bluez.Error.Failed', str(exc))

  def _close_peripheral_fds(self, p_cid: str) -> None:
    """Closes and removes any open C1/C2 FDs for p_cid."""
    self._get_c1_written_set().discard(p_cid)
    for fd_map in (self._peripheral_c1_fds, self._peripheral_c2_fds):
      fd = fd_map.pop(p_cid, None)
      if fd is not None:
        try:
          os.close(fd)
        except OSError as exc:
          logger.debug("Failed closing peripheral FD %d: %s", fd, exc)

  @staticmethod
  def _disconnect_bridge_handles(bridge: VirtualBluezAdapterBridge) -> None:
    """Sends HCI Disconnect (0x0406) for all active handles on a bridge."""
    if bridge.h4_client is not None:
      for handle in list(bridge.connected_handles.keys()):
        try:
          bridge.h4_client.send_command(
              0x0406, struct.pack('<HB', handle, 0x13), timeout=1.0
          )
        except Exception as exc:
          logger.debug(
              'Failed sending HCI Disconnect for handle 0x%04X: %s',
              handle,
              exc,
          )
    bridge.connected_handles.clear()

  def _disconnect_device(
      self,
      bridge: VirtualBluezAdapterBridge,
      object_path: str,
      invocation,
  ) -> None:
    """Disconnects an LE peripheral and closes acquired GATT FDs."""
    from gi.repository import GLib  # pylint: disable=g-import-not-at-top

    parts = object_path.split('/')
    dev_part = parts[4] if len(parts) > 4 else parts[-1]
    peer_addr = dev_part.replace('dev_', '').replace('_', ':')
    self._disconnect_bridge_handles(bridge)
    disconnected_props = GLib.Variant(
        '(sa{sv}as)',
        ('org.bluez.Device1', {'Connected': GLib.Variant('b', False)}, []),
    )
    for p_cid, p_bridge in list(self.docker_manager.adapter_bridges.items()):
      if p_bridge.bd_addr.upper() != peer_addr.upper():
        continue
      self._disconnect_bridge_handles(p_bridge)
      self._close_peripheral_fds(p_cid)
      periph_dev = f'/org/bluez/{p_cid}/dev_{bridge.bd_addr.replace(":", "_")}'
      self._emit_all(
          periph_dev,
          'org.freedesktop.DBus.Properties',
          'PropertiesChanged',
          disconnected_props,
      )
    self._emit_all(
        object_path,
        'org.freedesktop.DBus.Properties',
        'PropertiesChanged',
        disconnected_props,
    )
    if invocation is not None:
      invocation.return_value(None)

  def _handle_gatt_char_method(
      self,
      bridge: Optional[VirtualBluezAdapterBridge],
      method_name: str,
      parameters,
      invocation,
  ) -> None:
    """Handles org.bluez.GattCharacteristic1 D-Bus method calls."""
    from gi.repository import GLib  # pylint: disable=g-import-not-at-top

    if method_name == 'WriteValue' and bridge is not None:
      val_bytes = bytes(parameters.unpack()[0])
      if bridge.connected_handles:
        handle = next(iter(bridge.connected_handles.keys()))
        bridge.write_c1_request(handle, val_bytes)
      invocation.return_value(None)
      return
    if method_name == 'ReadValue':
      invocation.return_value(GLib.Variant('(ay)', ([],)))
      return
    if method_name == 'StartNotify' and bridge is not None:
      invocation.return_value(None)
      if bridge.connected_handles:
        peer_addr = next(iter(bridge.connected_handles.values()))
        threading.Thread(
            target=self._notify_peripheral_c2_subscribers,
            args=(bridge, peer_addr),
            daemon=True,
        ).start()
      return
    invocation.return_value(None)
