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
"""Docker HCI PTY, Unix control proxy, hciconfig, and BlueZ bridge."""

import logging
import os
import pty
import select
import socket
import stat
import threading
import tty
from typing import Dict, Optional, Tuple

from cirque.virtual_bt.matter_ble_bridge import (
    ATT_CID,
    ATT_HANDLE_C1_VALUE,
    ATT_HANDLE_C2_VALUE,
    ATT_OP_HANDLE_VALUE_CFM,
    ATT_OP_HANDLE_VALUE_IND,
    ATT_OP_WRITE_REQ,
    ATT_OP_WRITE_RSP,
    MATTER_C1_UUID_STR,
    MATTER_C2_UUID_STR,
    MATTER_SERVICE_UUID_SHORT,
    MATTER_SERVICE_UUID_STR,
    VirtualBluezAdapterBridge,
    build_matter_ble_adv_payload,
    parse_matter_ble_service_data,
)

logger = logging.getLogger('VirtualBtDockerBridge')

__all__ = [
    'ATT_CID',
    'ATT_HANDLE_C1_VALUE',
    'ATT_HANDLE_C2_VALUE',
    'ATT_OP_HANDLE_VALUE_CFM',
    'ATT_OP_HANDLE_VALUE_IND',
    'ATT_OP_WRITE_REQ',
    'ATT_OP_WRITE_RSP',
    'DockerVirtualBtManager',
    'MATTER_C1_UUID_STR',
    'MATTER_C2_UUID_STR',
    'MATTER_SERVICE_UUID_SHORT',
    'MATTER_SERVICE_UUID_STR',
    'VirtualBluezAdapterBridge',
    'VirtualPtyHciBridge',
    'build_matter_ble_adv_payload',
    'parse_matter_ble_service_data',
]

_ORG_BLUEZ_DBUS_CONF_XML = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy context="default">
    <allow own="org.bluez"/>
    <allow send_destination="org.bluez"/>
    <allow send_interface="org.bluez.Adapter1"/>
    <allow send_interface="org.bluez.GattManager1"/>
    <allow send_interface="org.bluez.LEAdvertisingManager1"/>
    <allow send_interface="org.bluez.Device1"/>
    <allow send_interface="org.bluez.GattService1"/>
    <allow send_interface="org.bluez.GattCharacteristic1"/>
    <allow send_interface="org.bluez.GattDescriptor1"/>
    <allow send_interface="org.bluez.GattProfile1"/>
    <allow send_interface="org.bluez.LEAdvertisement1"/>
    <allow send_interface="org.freedesktop.DBus.ObjectManager"/>
    <allow send_interface="org.freedesktop.DBus.Properties"/>
    <allow send_interface="org.freedesktop.DBus.Introspectable"/>
  </policy>
</busconfig>
"""

_HCICONFIG_SHIM_TEMPLATE = """#!/usr/bin/env python3
import json, os, socket, sys
HOST = os.environ.get('VIRTUAL_BT_HOST', '{host}')
PORT = int(os.environ.get('VIRTUAL_BT_CONTROL_PORT', '{control_port}'))
UNIX_SOCKS = ('/dev/virtual_bt/control.sock', '{control_sock_path}')

def rpc(cmd, **params):
  payload = (json.dumps({{'cmd': cmd, **params}}) + '\\n').encode('utf-8')
  for usock in UNIX_SOCKS:
    if os.path.exists(usock):
      try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
          s.settimeout(3.0)
          s.connect(usock)
          s.sendall(payload)
          return json.loads(s.makefile('r', encoding='utf-8').readline())
      except OSError:
        pass
  with socket.create_connection((HOST, PORT), timeout=3.0) as s:
    s.sendall(payload)
    return json.loads(s.makefile('r', encoding='utf-8').readline())

def format_ctrl(c):
  cid, bd = c.get('controller_id', 'hci0'), c.get('bd_addr', '00:00:00:00:00:00')
  rx, tx = c.get('rx_packets', 0), c.get('tx_packets', 0)
  arx, atx = c.get('acl_rx_packets', 0), c.get('acl_tx_packets', 0)
  return (
      f'{{cid}}:\\tType: Primary  Bus: Virtual\\n'
      f'\\tBD Address: {{bd}}  ACL MTU: 1021:8  SCO MTU: 64:1\\n'
      f'\\tUP RUNNING PSCAN ISCAN\\n'
      f'\\tRX bytes:{{rx * 16}} acl:{{arx}} sco:0 events:{{tx}} errors:0\\n'
      f'\\tTX bytes:{{tx * 16}} acl:{{atx}} sco:0 commands:{{rx}} errors:0\\n'
  )

def main():
  args = [a for a in sys.argv[1:] if not a.startswith('-')]
  ctrls = rpc('list_controllers').get('controllers', [])
  if not args:
    for c in ctrls:
      print(format_ctrl(c))
    return 0
  for c in ctrls:
    if c.get('controller_id') == args[0]:
      print(format_ctrl(c))
      return 0
  sys.stderr.write(f"Can't get device info: No such device ({{args[0]}})\\n")
  return 1

if __name__ == '__main__':
  sys.exit(main())
"""


class VirtualPtyHciBridge:
  """Bridges a POSIX PTY device (/tmp/cirque_virtual_bt/hciX) to TCP H4."""

  def __init__(
      self,
      controller_id: str,
      host: str,
      hci_tcp_port: int,
      runtime_dir: str = '/tmp/cirque_virtual_bt',
  ):
    self.controller_id = controller_id
    self.host = host
    self.hci_tcp_port = hci_tcp_port
    self.runtime_dir = runtime_dir
    self.master_fd: Optional[int] = None
    self.slave_fd: Optional[int] = None
    self.slave_name: str = ''
    self.symlink_path: str = os.path.join(runtime_dir, controller_id)
    self._running = False
    self._thread: Optional[threading.Thread] = None
    self._sock: Optional[socket.socket] = None

  def start(self) -> str:
    """Starts the PTY-to-TCP H4 relay thread and returns the device symlink."""
    os.makedirs(self.runtime_dir, exist_ok=True)
    self.master_fd, self.slave_fd = pty.openpty()
    tty.setraw(self.slave_fd)
    os.set_blocking(self.master_fd, False)
    self.slave_name = os.ttyname(self.slave_fd)
    try:
      os.chmod(self.slave_name, 0o666)
    except OSError:
      pass
    if os.path.lexists(self.symlink_path):
      try:
        os.unlink(self.symlink_path)
      except OSError:
        pass
    os.symlink(self.slave_name, self.symlink_path)
    self._sock = socket.create_connection(
        (self.host, self.hci_tcp_port), timeout=5.0
    )
    self._sock.settimeout(None)
    self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    self._running = True
    self._thread = threading.Thread(
        target=self._pump_loop, name=f'pty-h4-{self.controller_id}', daemon=True
    )
    self._thread.start()
    return self.symlink_path

  def _relay_single_fd(
      self, fd: int, master_fd: int, sock: socket.socket
  ) -> bool:
    """Relays one ready file descriptor; returns False when stream closes."""
    if fd == master_fd:
      try:
        data = os.read(master_fd, 4096)
        if not data:
          return False
        sock.sendall(data)
        return True
      except OSError:
        return False
    try:
      data = sock.recv(4096)
      if not data:
        self._running = False
        return False
      try:
        os.write(master_fd, data)
      except BlockingIOError:
        pass
      return True
    except OSError:
      self._running = False
      return False

  def _pump_loop(self) -> None:
    master_fd, sock = self.master_fd, self._sock
    if master_fd is None or sock is None:
      return
    try:
      sock_fd = sock.fileno()
    except OSError:
      return
    while self._running:
      try:
        rlist, _, _ = select.select([master_fd, sock_fd], [], [], 0.1)
      except (ValueError, OSError):
        break
      for fd in rlist:
        if not self._running or not self._relay_single_fd(fd, master_fd, sock):
          break

  def stop(self) -> None:
    self._running = False
    if self._sock is not None:
      try:
        self._sock.close()
      except OSError:
        pass
      self._sock = None
    for fd in (self.master_fd, self.slave_fd):
      if fd is not None:
        try:
          os.close(fd)
        except OSError:
          pass
    self.master_fd = None
    self.slave_fd = None
    if os.path.lexists(self.symlink_path):
      try:
        os.unlink(self.symlink_path)
      except OSError:
        pass
    if self._thread is not None and self._thread.is_alive():
      self._thread.join(timeout=1.5)


class DockerVirtualBtManager:
  """Manages Docker-visible HCI PTYs, Unix control socket, and hciconfig."""

  def __init__(
      self,
      host: str,
      control_port: int,
      hci_port: int,
      *extra_args,
      **kwargs,
  ):
    phy_port = int(
        extra_args[0] if len(extra_args) >= 1 else kwargs.get('phy_port', 0)
    )
    default_rt_dir = os.environ.get(
        'CIRQUE_VIRTUAL_BT_RUNTIME_DIR', '/tmp/cirque_virtual_bt'
    )
    runtime_dir = str(
        extra_args[1]
        if len(extra_args) >= 2
        else kwargs.get('runtime_dir', default_rt_dir)
    )
    self.host = host
    self.control_port = control_port
    self.hci_port = hci_port
    self.phy_port = phy_port
    self.runtime_dir = runtime_dir
    self.bin_dir = os.path.join(runtime_dir, 'bin')
    self.dbus_dir = os.path.join(runtime_dir, 'dbus')
    self.containers_dir = os.path.join(runtime_dir, 'containers')
    self.org_bluez_conf_path = os.path.join(runtime_dir, 'org.bluez.conf')
    self.control_sock_path = os.path.join(runtime_dir, 'control.sock')
    self.hciconfig_path = os.path.join(self.bin_dir, 'hciconfig')
    self.pty_bridges: Dict[str, VirtualPtyHciBridge] = {}
    self.adapter_bridges: Dict[str, VirtualBluezAdapterBridge] = {}
    self._lock = threading.RLock()
    self._unix_running = False
    self._unix_server: Optional[socket.socket] = None
    self._unix_thread: Optional[threading.Thread] = None
    os.makedirs(self.dbus_dir, exist_ok=True)
    os.makedirs(self.containers_dir, exist_ok=True)
    self._generate_org_bluez_dbus_conf()
    self._start_unix_control_proxy()
    self._generate_hciconfig_shim()

  def _generate_org_bluez_dbus_conf(self) -> str:
    """Writes /tmp/cirque_virtual_bt/org.bluez.conf for container D-Bus."""
    os.makedirs(self.runtime_dir, exist_ok=True)
    with open(self.org_bluez_conf_path, 'w', encoding='utf-8') as f:
      f.write(_ORG_BLUEZ_DBUS_CONF_XML)
    try:
      os.chmod(self.org_bluez_conf_path, 0o644)
    except OSError:
      pass
    return self.org_bluez_conf_path

  def get_container_dbus_dir(self, controller_id: str) -> str:
    """Returns a dedicated /run/dbus host directory for a single container."""
    c_dbus_dir = os.path.join(self.containers_dir, controller_id, 'dbus')
    os.makedirs(c_dbus_dir, exist_ok=True)
    try:
      os.chmod(c_dbus_dir, 0o777)
    except OSError:
      pass
    for stale_name in ('pid', 'system_bus_socket'):
      stale_path = os.path.join(c_dbus_dir, stale_name)
      if os.path.lexists(stale_path):
        try:
          os.unlink(stale_path)
        except OSError:
          pass
    return c_dbus_dir

  def _start_unix_control_proxy(self) -> None:
    """Exposes a Unix domain socket proxy to the TCP Control Port."""
    os.makedirs(self.runtime_dir, exist_ok=True)
    if os.path.exists(self.control_sock_path):
      try:
        os.unlink(self.control_sock_path)
      except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(self.control_sock_path)
    try:
      os.chmod(self.control_sock_path, 0o666)
    except OSError:
      pass
    srv.listen(16)
    self._unix_server = srv
    self._unix_running = True
    self._unix_thread = threading.Thread(
        target=self._unix_accept_loop, name='virtual-bt-unix-proxy', daemon=True
    )
    self._unix_thread.start()

  def _unix_accept_loop(self) -> None:
    srv = self._unix_server
    if srv is None:
      return
    while self._unix_running:
      try:
        client_sock, _ = srv.accept()
      except OSError:
        break
      threading.Thread(
          target=self._handle_unix_client, args=(client_sock,), daemon=True
      ).start()

  def _handle_unix_client(self, client_sock: socket.socket) -> None:
    with client_sock:
      try:
        data = client_sock.recv(4096)
        if not data:
          return
        with socket.create_connection(
            (self.host, self.control_port), timeout=3.0
        ) as tcp_sock:
          tcp_sock.sendall(data)
          resp = tcp_sock.recv(16384)
        if resp:
          client_sock.sendall(resp)
      except OSError:
        pass

  def _generate_hciconfig_shim(self) -> None:
    """Generates a drop-in hciconfig binary script backed by Unix/TCP socket."""
    os.makedirs(self.bin_dir, exist_ok=True)
    script = _HCICONFIG_SHIM_TEMPLATE.format(
        host=self.host,
        control_port=self.control_port,
        control_sock_path=self.control_sock_path,
    )
    with open(self.hciconfig_path, 'w', encoding='utf-8') as f:
      f.write(script)
    os.chmod(
        self.hciconfig_path,
        stat.S_IRWXU
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH,
    )

  def register_controller(
      self, controller_id: str, bd_addr: str, dedicated_hci_port: int
  ) -> Tuple[str, VirtualBluezAdapterBridge]:
    """Starts PTY device + VirtualBluezAdapterBridge for a controller."""
    with self._lock:
      self._generate_hciconfig_shim()
      pty_bridge = VirtualPtyHciBridge(
          controller_id=controller_id,
          host=self.host,
          hci_tcp_port=dedicated_hci_port,
          runtime_dir=self.runtime_dir,
      )
      pty_path = pty_bridge.start()
      self.pty_bridges[controller_id] = pty_bridge

      adapter_bridge = VirtualBluezAdapterBridge(
          controller_id=controller_id,
          bd_addr=bd_addr,
          host=self.host,
          dedicated_hci_port=dedicated_hci_port,
      )
      adapter_bridge.start()
      self.adapter_bridges[controller_id] = adapter_bridge
      return pty_path, adapter_bridge

  def unregister_controller(self, controller_id: str) -> None:
    with self._lock:
      adapter = self.adapter_bridges.pop(controller_id, None)
      if adapter is not None:
        adapter.stop()
      pty_bridge = self.pty_bridges.pop(controller_id, None)
      if pty_bridge is not None:
        pty_bridge.stop()

  def stop_all(self) -> None:
    self._unix_running = False
    if self._unix_server is not None:
      try:
        self._unix_server.close()
      except OSError:
        pass
      self._unix_server = None
    if os.path.exists(self.control_sock_path):
      try:
        os.unlink(self.control_sock_path)
      except OSError:
        pass
    with self._lock:
      for cid in list(self.adapter_bridges.keys()):
        self.unregister_controller(cid)
