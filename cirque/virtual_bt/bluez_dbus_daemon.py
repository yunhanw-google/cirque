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
"""BlueZ org.bluez D-Bus service daemon backed by TCP VirtualBluetoothServer."""

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Dict, Optional, Tuple

from cirque.virtual_bt.bluez_gatt_helpers import (
    BLUEZ_INTROSPECT_XML,
    build_dbus_bus_conf_xml,
    handle_bluez_adapter_method,
    query_advertisement_discriminator,
)
from cirque.virtual_bt.bluez_gatt_mixin import BluezGattPeripheralMixin
from cirque.virtual_bt.docker_hci_bridge import (
    DockerVirtualBtManager,
    VirtualBluezAdapterBridge,
)

logger = logging.getLogger('VirtualBtBluezDbus')


class BluezDbusVirtualService(BluezGattPeripheralMixin):
  """Exposes /org/bluez/hci* on D-Bus backed by H4-over-TCP bridges."""

  def __init__(self, docker_manager: DockerVirtualBtManager):
    self.docker_manager = docker_manager
    self.dbus_dir = docker_manager.dbus_dir
    self.socket_path = os.path.join(self.dbus_dir, 'system_bus_socket')
    self.bus_address = f'unix:path={self.socket_path}'
    self._dbus_proc: Optional[subprocess.Popen] = None
    self._running = False
    self._thread: Optional[threading.Thread] = None
    self._loop = None
    self._conn = None
    self._conns: Dict[str, object] = {}
    self._owner_id: int = 0
    self._owner_ids: list[int] = []
    self._reg_ids: list[int] = []
    self._exported_paths: set[str] = set()
    self._exported_paths_by_conn: Dict[int, set[str]] = {}
    self._lock = threading.RLock()
    self._registered_apps: Dict[str, Tuple[str, str]] = {}
    self._registered_app_conns: Dict[str, object] = {}
    self._registered_advs: Dict[str, Tuple[str, str]] = {}
    self._peripheral_c1_fds: Dict[str, int] = {}
    self._peripheral_c2_fds: Dict[str, int] = {}

  def _ensure_dedicated_dbus_daemon(self) -> bool:
    """Starts a permissive dbus-daemon in /tmp/cirque_virtual_bt/dbus."""
    os.makedirs(self.dbus_dir, exist_ok=True)
    if os.path.exists(self.socket_path):
      try:
        os.unlink(self.socket_path)
      except OSError:
        pass
    dbus_bin = shutil.which('dbus-daemon')
    if not dbus_bin:
      return False
    conf_path = os.path.join(self.dbus_dir, 'bus.conf')
    with open(conf_path, 'w', encoding='utf-8') as f:
      f.write(build_dbus_bus_conf_xml(self.socket_path))
    self._dbus_proc = subprocess.Popen(
        [dbus_bin, f'--config-file={conf_path}', '--nofork', '--nopidfile'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 3.0
    while time.time() < deadline:
      if os.path.exists(self.socket_path):
        try:
          os.chmod(self.socket_path, 0o666)
        except OSError:
          pass
        return True
      time.sleep(0.02)
    return False

  def start(self) -> bool:
    """Starts the dedicated D-Bus daemon and registers org.bluez."""
    if self._running:
      return True
    has_dedicated = self._ensure_dedicated_dbus_daemon()
    if not has_dedicated and not os.path.exists(
        '/var/run/dbus/system_bus_socket'
    ):
      logger.info('No D-Bus socket available; skipping org.bluez registration')
      return False
    try:
      from gi.repository import Gio, GLib  # pylint: disable=g-import-not-at-top
    except ImportError:
      logger.info('PyGObject Gio/GLib not installed; skipping org.bluez bind')
      return False

    try:
      if has_dedicated:
        self._conn = Gio.DBusConnection.new_for_address_sync(
            self.bus_address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
      else:
        self._conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

      self._conns['default'] = self._conn
      self._register_root_on_conn(self._conn)
      self._loop = GLib.MainLoop()
      self._running = True
      self._thread = threading.Thread(
          target=self._loop.run, name='virtual-bt-bluez-dbus', daemon=True
      )
      self._thread.start()
      return True
    except Exception as exc:  # pylint: disable=broad-exception-caught
      logger.warning('Could not register org.bluez on D-Bus: %s', exc)
      return False

  def _all_connections(self) -> list:
    with self._lock:
      conns = [c for c in self._conns.values() if c is not None]
      if not conns and self._conn is not None:
        conns = [self._conn]
      return conns

  def _emit_all(
      self, obj_path: str, iface_name: str, sig_name: str, params
  ) -> None:
    for conn in self._all_connections():
      try:
        conn.emit_signal(None, obj_path, iface_name, sig_name, params)
      except Exception:  # pylint: disable=broad-exception-caught
        pass

  def _register_root_on_conn(self, conn) -> None:
    from gi.repository import Gio  # pylint: disable=g-import-not-at-top

    exported = self._exported_paths_by_conn.setdefault(id(conn), set())
    if '/' in exported:
      return
    node_info = Gio.DBusNodeInfo.new_for_xml(BLUEZ_INTROSPECT_XML)
    self._reg_ids.append(
        conn.register_object(
            '/',
            node_info.lookup_interface('org.freedesktop.DBus.ObjectManager'),
            self._handle_method_call,
            self._handle_get_property,
            None,
        )
    )
    owner_id = Gio.bus_own_name_on_connection(
        conn,
        'org.bluez',
        Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
        None,
        None,
    )
    if not self._owner_id:
      self._owner_id = owner_id
    self._owner_ids.append(owner_id)
    exported.add('/')

  def _register_adapter_on_conn(self, conn, controller_id: str) -> None:
    from gi.repository import Gio  # pylint: disable=g-import-not-at-top

    exported = self._exported_paths_by_conn.setdefault(id(conn), set())
    obj_path = f'/org/bluez/{controller_id}'
    if obj_path in exported:
      return
    node_info = Gio.DBusNodeInfo.new_for_xml(BLUEZ_INTROSPECT_XML)
    for iface_name in (
        'org.bluez.Adapter1',
        'org.bluez.GattManager1',
        'org.bluez.LEAdvertisingManager1',
    ):
      self._reg_ids.append(
          conn.register_object(
              obj_path,
              node_info.lookup_interface(iface_name),
              self._handle_method_call,
              self._handle_get_property,
              self._handle_set_property,
          )
      )
    exported.add(obj_path)
    self._exported_paths.add(obj_path)

  def _register_device_on_conn(self, conn, dev_path: str) -> None:
    from gi.repository import Gio  # pylint: disable=g-import-not-at-top

    exported = self._exported_paths_by_conn.setdefault(id(conn), set())
    if dev_path in exported:
      return
    node_info = Gio.DBusNodeInfo.new_for_xml(BLUEZ_INTROSPECT_XML)
    svc_path = f'{dev_path}/service00'
    for path, iface in (
        (dev_path, 'org.bluez.Device1'),
        (svc_path, 'org.bluez.GattService1'),
        (f'{svc_path}/char00', 'org.bluez.GattCharacteristic1'),
        (f'{svc_path}/char01', 'org.bluez.GattCharacteristic1'),
    ):
      self._reg_ids.append(
          conn.register_object(
              path,
              node_info.lookup_interface(iface),
              self._handle_method_call,
              self._handle_get_property,
              self._handle_set_property,
          )
      )
    exported.add(dev_path)
    self._exported_paths.add(dev_path)

  def _wait_for_socket_file(self, socket_path: str) -> bool:
    """Waits up to 8s for a container D-Bus socket to appear."""
    deadline = time.time() + 8.0
    while time.time() < deadline:
      if os.path.exists(socket_path):
        try:
          os.chmod(socket_path, 0o666)
        except OSError:
          pass
        return True
      time.sleep(0.05)
    return os.path.exists(socket_path)

  def attach_container_bus(self, controller_id: str, socket_path: str) -> bool:
    """Attaches org.bluez onto a container's /run/dbus/system_bus_socket."""
    if not self._running or not self._wait_for_socket_file(socket_path):
      logger.warning(
          'Container D-Bus socket unavailable for %s at %s',
          controller_id,
          socket_path,
      )
      return False
    try:
      from gi.repository import Gio  # pylint: disable=g-import-not-at-top

      conn = Gio.DBusConnection.new_for_address_sync(
          f'unix:path={socket_path}',
          Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
          | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
          None,
          None,
      )
      with self._lock:
        self._conns[controller_id] = conn
        self._register_root_on_conn(conn)
        for cid in list(self.docker_manager.adapter_bridges.keys()):
          self._register_adapter_on_conn(conn, cid)
        for path in [p for p in self._exported_paths if '/dev_' in p]:
          self._register_device_on_conn(conn, path)
      return True
    except Exception as exc:  # pylint: disable=broad-exception-caught
      logger.warning('Attach container bus failed (%s): %s', controller_id, exc)
      return False

  def export_adapter(self, controller_id: str) -> None:
    """Exports /org/bluez/hciX interfaces and attaches H4 event callbacks."""
    bridge = self.docker_manager.adapter_bridges.get(controller_id)
    if bridge is not None:
      bridge.on_discovery_callback = (
          lambda addr, info, cid=controller_id: self._on_peripheral_discovered(
              cid, addr, info
          )
      )
      bridge.on_write_callback = (
          lambda data, cid=controller_id: self._on_peripheral_c1_write(
              cid, data
          )
      )
      bridge.on_indication_callback = (
          lambda data, cid=controller_id: self._on_central_c2_indication(
              cid, data
          )
      )
    if not self._running:
      return
    with self._lock:
      for conn in self._all_connections():
        try:
          self._register_adapter_on_conn(conn, controller_id)
        except Exception as exc:  # pylint: disable=broad-exception-caught
          logger.debug('Adapter export skipped for %s: %s', controller_id, exc)

  def _export_device_gatt_tree(self, controller_id: str, peer_addr: str) -> str:
    """Exports Device1, GattService1, and C1/C2 GattCharacteristic1 objects."""
    dev_path = f'/org/bluez/{controller_id}/dev_{peer_addr.replace(":", "_")}'
    if not self._running:
      return dev_path
    with self._lock:
      for conn in self._all_connections():
        try:
          self._register_device_on_conn(conn, dev_path)
        except Exception as exc:  # pylint: disable=broad-exception-caught
          logger.debug('Device export failed for %s: %s', dev_path, exc)
    return dev_path

  def _handle_adv_manager_method(
      self,
      cid: str,
      bridge: Optional[VirtualBluezAdapterBridge],
      *adv_args,
  ) -> None:
    """Handles org.bluez.LEAdvertisingManager1 D-Bus method calls."""
    connection, sender, method_name, parameters, invocation = adv_args
    if method_name == 'RegisterAdvertisement' and bridge is not None:
      adv_path = parameters.unpack()[0]
      self._registered_advs[cid] = (sender, adv_path)

      def _register_adv_worker():
        disc = query_advertisement_discriminator(connection, sender, adv_path)
        bridge.start_matter_advertising(discriminator=disc)
        invocation.return_value(None)

      threading.Thread(target=_register_adv_worker, daemon=True).start()
      return
    if method_name == 'UnregisterAdvertisement' and bridge is not None:
      bridge.stop_matter_advertising()
    invocation.return_value(None)

  def _handle_method_call(self, connection, sender, *cb_args):
    """Dispatches incoming Gio.DBusInterfaceMethodCall invocations."""
    object_path, interface_name, method_name, parameters, invocation = cb_args
    from gi.repository import GLib  # pylint: disable=g-import-not-at-top

    try:
      if (
          interface_name == 'org.freedesktop.DBus.ObjectManager'
          and method_name == 'GetManagedObjects'
      ):
        objs = self._build_managed_objects_dict()
        invocation.return_value(GLib.Variant('(a{oa{sa{sv}}})', (objs,)))
        return
      cid = (
          object_path.split('/')[3]
          if object_path.startswith('/org/bluez/hci')
          else 'hci0'
      )
      bridge = self.docker_manager.adapter_bridges.get(cid)
      if interface_name == 'org.bluez.Adapter1':
        handle_bluez_adapter_method(bridge, method_name, parameters, invocation)
      elif interface_name == 'org.bluez.GattManager1':
        if method_name == 'RegisterApplication':
          self._registered_apps[cid] = (sender, parameters.unpack()[0])
          self._registered_app_conns[cid] = connection
        invocation.return_value(None)
      elif interface_name == 'org.bluez.LEAdvertisingManager1':
        self._handle_adv_manager_method(
            cid, bridge, connection, sender, method_name, parameters, invocation
        )
      elif interface_name == 'org.bluez.Device1':
        if method_name == 'Connect' and bridge is not None:
          threading.Thread(
              target=self._connect_device_worker,
              args=(cid, bridge, object_path, invocation),
              daemon=True,
          ).start()
        elif method_name == 'Disconnect' and bridge is not None:
          self._disconnect_device(bridge, object_path, invocation)
        else:
          invocation.return_value(None)
      elif interface_name == 'org.bluez.GattCharacteristic1':
        self._handle_gatt_char_method(
            bridge, method_name, parameters, invocation
        )
      else:
        invocation.return_value(None)
    except Exception as exc:  # pylint: disable=broad-exception-caught
      invocation.return_dbus_error('org.bluez.Error.Failed', str(exc))

  def _handle_get_property(self, _connection, _sender, *prop_args):
    object_path, interface_name, property_name = prop_args[:3]
    objs = self._build_managed_objects_dict()
    return objs.get(object_path, {}).get(interface_name, {}).get(property_name)

  def _handle_set_property(self, _connection, _sender, *_prop_args):
    return True

  def stop(self) -> None:
    self._running = False
    for cid in list(self._peripheral_c1_fds.keys()) + list(
        self._peripheral_c2_fds.keys()
    ):
      self._close_peripheral_fds(cid)
    if self._loop is not None:
      try:
        self._loop.quit()
      except Exception:  # pylint: disable=broad-exception-caught
        pass
      self._loop = None
    if self._dbus_proc is not None:
      try:
        self._dbus_proc.terminate()
        self._dbus_proc.wait(timeout=2.0)
      except Exception:  # pylint: disable=broad-exception-caught
        pass
      self._dbus_proc = None
