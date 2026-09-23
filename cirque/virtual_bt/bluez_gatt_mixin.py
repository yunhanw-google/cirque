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
"""Peripheral GATT socket acquisition and Device1/GattCharacteristic1 mixin."""

import logging
import os
import threading
import time
from typing import Dict, Optional

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

  def _on_peripheral_discovered(
      self, controller_id: str, addr: str, _info: Dict[str, object]
  ) -> None:
    """Emits InterfacesAdded and RSSI changes when a device is discovered."""
    dev_path = self._export_device_gatt_tree(controller_id, addr)
    if not self._running:
      return
    try:
      from gi.repository import GLib  # pylint: disable=g-import-not-at-top

      objs = self._build_managed_objects_dict()
      for path in (
          dev_path,
          f'{dev_path}/service00',
          f'{dev_path}/service00/char00',
          f'{dev_path}/service00/char01',
      ):
        if path in objs:
          self._emit_all(
              '/',
              'org.freedesktop.DBus.ObjectManager',
              'InterfacesAdded',
              GLib.Variant('(oa{sa{sv}})', (path, objs[path])),
          )
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

  def _on_peripheral_c1_write(self, controller_id: str, data: bytes) -> None:
    """Forwards an H4 TCP C1 ATT Write payload into peripheral socketpair."""
    deadline = time.time() + 1.5
    while time.time() < deadline:
      fd = self._peripheral_c1_fds.get(controller_id)
      if fd is not None:
        try:
          os.write(fd, data)
        except OSError:
          pass
        return
      time.sleep(0.02)

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
    except Exception:  # pylint: disable=broad-exception-caught
      pass

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
          bridge.send_c2_indication(handle, data)
          time.sleep(0.005)
          # Write 1-byte ATT confirmation (`0x01`) back to `c2_fd` so
          # BlueZ/Matter `BluezSendIndication` knows the indication arrived.
          os.write(c2_fd, b'\x01')
      except (BlockingIOError, InterruptedError):
        time.sleep(0.005)
      except OSError:
        break

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
        if p_bridge.bd_addr.upper() == peer_addr.upper():
          p_bridge.discovered_peripherals[bridge.bd_addr] = {
              'bd_addr': bridge.bd_addr,
              'rssi': -45,
              'discriminator': 0,
          }
          p_bridge.connected_handles[0x0040] = bridge.bd_addr
          self._acquire_peripheral_gatt_sockets(p_cid, bridge.bd_addr)
      self._export_device_gatt_tree(cid, peer_addr)
      objs = self._build_managed_objects_dict()
      for path in (
          object_path,
          f'{object_path}/service00',
          f'{object_path}/service00/char00',
          f'{object_path}/service00/char01',
      ):
        if path in objs:
          self._emit_all(
              '/',
              'org.freedesktop.DBus.ObjectManager',
              'InterfacesAdded',
              GLib.Variant('(oa{sa{sv}})', (path, objs[path])),
          )
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
    for fd_map in (self._peripheral_c1_fds, self._peripheral_c2_fds):
      fd = fd_map.pop(p_cid, None)
      if fd is not None:
        try:
          os.close(fd)
        except OSError:
          pass

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
    bridge.connected_handles.clear()
    for p_cid, p_bridge in list(self.docker_manager.adapter_bridges.items()):
      if p_bridge.bd_addr.upper() != peer_addr.upper():
        continue
      p_bridge.connected_handles.clear()
      self._close_peripheral_fds(p_cid)
      periph_dev = f'/org/bluez/{p_cid}/dev_{bridge.bd_addr.replace(":", "_")}'
      self._emit_all(
          periph_dev,
          'org.freedesktop.DBus.Properties',
          'PropertiesChanged',
          GLib.Variant(
              '(sa{sv}as)',
              (
                  'org.bluez.Device1',
                  {'Connected': GLib.Variant('b', False)},
                  [],
              ),
          ),
      )
    self._emit_all(
        object_path,
        'org.freedesktop.DBus.Properties',
        'PropertiesChanged',
        GLib.Variant(
            '(sa{sv}as)',
            ('org.bluez.Device1', {'Connected': GLib.Variant('b', False)}, []),
        ),
    )
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

        def _start_notify_worker():
          time.sleep(0.05)
          for p_cid, p_bridge in list(
              self.docker_manager.adapter_bridges.items()
          ):
            if p_bridge.bd_addr.upper() == peer_addr.upper():
              self._acquire_peripheral_c2_notify(p_cid, bridge.bd_addr)

        threading.Thread(target=_start_notify_worker, daemon=True).start()
      return
    invocation.return_value(None)
