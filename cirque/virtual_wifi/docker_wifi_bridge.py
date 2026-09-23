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
"""Docker Virtual Wi-Fi wlan0 TAP/veth + L2 Switch & D-Bus socket bridge.

Coordinates container-level virtual Wi-Fi plumbing without requiring host
kernel modules (`mac80211_hwsim`):
  1. Generates the `fi.w1.wpa_supplicant1.conf` D-Bus system bus security policy
     so the host-side `WpaSupplicantDbusService` can own `fi.w1.wpa_supplicant1`
     inside each container's isolated `/run/dbus/system_bus_socket`.
  2. Exposes Unix domain socket proxies (`control.sock`, `data.sock`) mounted
     at `/dev/virtual_wifi` inside each container.
  3. Provisions a real `wlan0` veth/TAP pair inside the container and spawns
     `vwifi_l2_agent.py` to bridge `wlan0` frames to
     `/dev/virtual_wifi/data.sock`.
"""

import logging
import os
import threading
from typing import Dict, Optional, Tuple

from cirque.common.docker_transport import (
    UnixToTcpProxy,
    prepare_container_dbus_dir,
    write_executable_script,
)
from cirque.virtual_wifi.server import VirtualWiFiServer

logger = logging.getLogger('VirtualWiFiDockerBridge')

_WPA_DBUS_CONF_XML = (
    '<!DOCTYPE busconfig PUBLIC\n'
    ' "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"\n'
    ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n'
    '<busconfig>\n'
    '  <policy context="default">\n'
    '    <allow own="fi.w1.wpa_supplicant1"/>\n'
    '    <allow send_destination="fi.w1.wpa_supplicant1"/>\n'
    '    <allow send_interface="fi.w1.wpa_supplicant1"/>\n'
    '    <allow send_interface="fi.w1.wpa_supplicant1.Interface"/>\n'
    '    <allow send_interface="fi.w1.wpa_supplicant1.BSS"/>\n'
    '    <allow send_interface="fi.w1.wpa_supplicant1.Network"/>\n'
    '    <allow send_interface="org.freedesktop.DBus.Properties"/>\n'
    '    <allow send_interface="org.freedesktop.DBus.Introspectable"/>\n'
    '    <allow send_interface="org.freedesktop.DBus.ObjectManager"/>\n'
    '  </policy>\n'
    '</busconfig>\n'
)

_L2_AGENT_SCRIPT = """#!/usr/bin/env python3
import select
import socket
import struct
import sys
import time

# Linux AF_PACKET `sll_pkttype` constant (`<linux/if_packet.h>`):
# Frames injected into `vwifi_phy` by `raw_sock.send(frame)` are echoed back
# on `wlan0`, while frames transmitted out of `vwifi_phy` itself have
# `addr[2] == PACKET_OUTGOING (4)`. Filtering out `PACKET_OUTGOING` prevents
# infinite L2 broadcast reflection loops across the virtual switch.
PACKET_OUTGOING = 4

def recv_exact(sock, num_bytes):
    buf = bytearray()
    while len(buf) < num_bytes:
        chunk = sock.recv(num_bytes - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

def _forward_from_raw(raw_sock, usock):
    frame, addr = raw_sock.recvfrom(65535)
    if len(addr) < 3 or addr[2] != PACKET_OUTGOING:
        usock.sendall(struct.pack('!H', len(frame)) + frame)


def _forward_from_unix(raw_sock, usock):
    hdr = recv_exact(usock, 2)
    if not hdr:
        raise ConnectionError('EOF')
    frame = recv_exact(usock, struct.unpack('!H', hdr)[0])
    if not frame:
        raise ConnectionError('EOF')
    if len(frame) < 14:
        return
    try:
        raw_sock.send(frame)
    except (BlockingIOError, InterruptedError):
        pass


def pump_once(raw_sock, usock):
    rlist, _, _ = select.select([raw_sock, usock], [], [], 1.0)
    for ready in rlist:
        if ready is raw_sock:
            _forward_from_raw(raw_sock, usock)
        else:
            _forward_from_unix(raw_sock, usock)


def _run_bridge_session(station_id, unix_sock_path, raw_sock):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as usock:
        usock.connect(unix_sock_path)
        sid_bytes = station_id.encode('utf-8')
        usock.sendall(struct.pack('!H', len(sid_bytes)) + sid_bytes)
        while True:
            pump_once(raw_sock, usock)


def main():
    if len(sys.argv) < 4:
        return 1
    station_id, ifname, unix_sock_path = sys.argv[1], sys.argv[2], sys.argv[3]
    raw_sock = socket.socket(
        socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003)
    )
    raw_sock.bind((ifname, 0))
    raw_sock.setblocking(False)
    while True:
        try:
            _run_bridge_session(station_id, unix_sock_path, raw_sock)
        except Exception:
            time.sleep(0.2)


if __name__ == '__main__':
    sys.exit(main())
"""

_DHCPCD_SHIM_SCRIPT = """#!/usr/bin/env python3
import os
import subprocess
import sys

def main():
    station_ip = os.environ.get("VIRTUAL_WIFI_IPV4", "10.0.1.10")
    station_ip6 = os.environ.get("VIRTUAL_WIFI_IPV6", "fd11:22::10")
    subprocess.run(["ip", "link", "set", "dev", "wlan0", "up"], check=False)
    subprocess.run(
        ["ip", "addr", "replace", f"{station_ip}/24", "dev", "wlan0"],
        check=False,
    )
    subprocess.run(
        ["ip", "-6", "addr", "replace", f"{station_ip6}/64", "dev", "wlan0"],
        check=False,
    )
    print(f"wlan0: leased {station_ip} for 86400 seconds")
    print("wlan0: adding default route via 10.0.1.1")
    return 0

if __name__ == "__main__":
    sys.exit(main())
"""


class DockerVirtualWiFiManager:
  """Manages container wlan0 interfaces, L2 switch sockets, and D-Bus."""

  def __init__(
      self,
      server: VirtualWiFiServer,
      runtime_dir: Optional[str] = None,
  ):
    if not runtime_dir:
      runtime_dir = os.environ.get(
          'CIRQUE_VIRTUAL_WIFI_RUNTIME_DIR', '/tmp/cirque_virtual_wifi'
      )
    self.server = server
    self.host = server.host
    self.control_port = server.control_port
    self.mgmt_port = server.mgmt_port
    self.data_port = server.data_port
    self.runtime_dir = runtime_dir
    self.containers_dir = os.path.join(runtime_dir, 'containers')
    self.bin_dir = os.path.join(runtime_dir, 'bin')
    self.control_sock_path = os.path.join(runtime_dir, 'control.sock')
    self.data_sock_path = os.path.join(runtime_dir, 'data.sock')
    self.wpa_conf_path = os.path.join(runtime_dir, 'fi.w1.wpa_supplicant1.conf')
    self.l2_agent_path = os.path.join(self.bin_dir, 'vwifi_l2_agent.py')
    self.iwlist_path = os.path.join(self.bin_dir, 'iwlist')
    self.dhcpcd_path = os.path.join(self.bin_dir, 'dhcpcd')
    self._lock = threading.RLock()
    self._station_nodes: Dict[str, object] = {}
    self._station_ips: Dict[str, Tuple[str, str]] = {}
    self._control_proxy = UnixToTcpProxy(
        self.control_sock_path,
        self.host,
        self.control_port,
        backlog=32,
        name='vwifi-unix-ctrl',
    )
    self._data_proxy = UnixToTcpProxy(
        self.data_sock_path,
        self.host,
        self.data_port,
        bidirectional=True,
        backlog=32,
        name='vwifi-unix-data',
    )
    self._ensure_support_files()
    self._control_proxy.start()
    try:
      self._data_proxy.start()
    except BaseException:
      self._control_proxy.stop()
      raise

  def _ensure_support_files(self) -> None:
    os.makedirs(self.runtime_dir, exist_ok=True)
    os.makedirs(self.containers_dir, exist_ok=True)
    os.makedirs(self.bin_dir, exist_ok=True)
    self._generate_wpa_dbus_conf()
    self._write_script(self.l2_agent_path, _L2_AGENT_SCRIPT)
    self._generate_iwlist_shim()
    self._write_script(self.dhcpcd_path, _DHCPCD_SHIM_SCRIPT)

  def _write_script(self, path: str, content: str) -> None:
    write_executable_script(path, content, mode=0o755)

  def _generate_wpa_dbus_conf(self) -> str:
    with open(self.wpa_conf_path, 'w', encoding='utf-8') as conf_file:
      conf_file.write(_WPA_DBUS_CONF_XML)
    os.chmod(self.wpa_conf_path, 0o644)
    return self.wpa_conf_path

  def _generate_iwlist_shim(self) -> None:
    iwlist_script = (
        '#!/usr/bin/env python3\n'
        'import json, os, socket, sys\n'
        "SOCK_PATHS = ['/dev/virtual_wifi/control.sock',"
        f" '{self.control_sock_path}']\n"
        f"HOST, PORT = '{self.host}', {self.control_port}\n"
        'def query_aps():\n'
        '    req = (json.dumps({"cmd": "list_aps"}) + "\\n").encode("utf-8")\n'
        '    for p in SOCK_PATHS:\n'
        '        if os.path.exists(p):\n'
        '            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as'
        ' s:\n'
        '                s.settimeout(3.0)\n'
        '                s.connect(p)\n'
        '                s.sendall(req)\n'
        '                return'
        ' json.loads(s.makefile("r").readline()).get("aps", [])\n'
        '    with socket.create_connection((HOST, PORT), timeout=3.0) as s:\n'
        '        s.sendall(req)\n'
        '        return json.loads(s.makefile("r").readline()).get("aps", [])\n'
        'def main():\n'
        '    print("wlan0     Scan completed :")\n'
        '    for idx, ap in enumerate(query_aps(), 1):\n'
        '        print(f"          Cell {idx:02d} - Address:'
        " {ap.get('bssid')}\")\n"
        "        print(f\"                    Channel:{ap.get('channel',"
        ' 6)}")\n'
        '        print(f"                    Quality=70/70  Signal'
        " level={ap.get('signal', -40)} dBm\")\n"
        '        print("                    Encryption key:on")\n'
        '        print(f\'                    ESSID:"{ap.get("ssid")}"\')\n'
        '    return 0\n'
        'if __name__ == "__main__":\n'
        '    sys.exit(main())\n'
    )
    self._write_script(self.iwlist_path, iwlist_script)

  def get_container_dbus_dir(self, station_id: str) -> str:
    """Returns a dedicated /run/dbus host directory for a Wi-Fi container."""
    return prepare_container_dbus_dir(
        os.path.join(self.containers_dir, station_id, 'dbus')
    )

  def register_station(
      self,
      station_id: str,
      idx: int,
      is_ap: bool = False,
  ) -> Tuple[str, str, str]:
    """Allocates MAC, IPv4, and IPv6 addresses for a virtual Wi-Fi interface."""
    with self._lock:
      if is_ap:
        mac = f'02:00:00:00:01:{idx + 1:02x}'
        ipv4 = '10.0.1.1'
        ipv6 = 'fd11:22::1'
      else:
        host_num = 10 + idx
        mac = f'02:00:00:00:02:{host_num:02x}'
        ipv4 = f'10.0.1.{host_num}'
        ipv6 = f'fd11:22::{host_num:x}'
      self._station_ips[station_id] = (ipv4, ipv6)
      self.server.register_station(
          station_id=station_id,
          ifname='wlan0',
          mac_addr=mac,
          ipv4_addr=ipv4,
          ipv6_addr=ipv6,
          is_ap_bridge=is_ap,
      )
      return mac, ipv4, ipv6

  def setup_container_interface(
      self,
      station_id: str,
      docker_node: object,
      mac_addr: str,
      ipv4_addr: str = '',
      ipv6_addr: str = '',
      auto_connect: bool = False,
  ) -> None:
    """Creates wlan0<->vwifi_phy veth inside container and starts L2 relay."""
    with self._lock:
      self._station_nodes[station_id] = docker_node
    container = getattr(docker_node, 'container', None)
    if container is None:
      return
    peer_mac = '02:00:00:00:fe:' + mac_addr.split(':')[-1]
    cmds = [
        'chmod 0666 /run/dbus/system_bus_socket 2>/dev/null || true',
        (
            'mkdir -p /etc/wpa_supplicant && touch'
            ' /etc/wpa_supplicant/wpa_supplicant.conf'
        ),
        'ip link del wlan0 2>/dev/null || true',
        'ip link add wlan0 type veth peer name vwifi_phy',
        f'ip link set dev wlan0 address {mac_addr}',
        f'ip link set dev vwifi_phy address {peer_mac}',
        'ip link set dev wlan0 multicast on',
        'ip link set dev vwifi_phy promisc on up',
        'ip link set dev wlan0 up',
        'sysctl -w net.ipv6.conf.wlan0.disable_ipv6=0 >/dev/null 2>&1 || true',
        'sysctl -w net.ipv6.conf.all.disable_ipv6=0 >/dev/null 2>&1 || true',
        'ethtool -K wlan0 tx off rx off >/dev/null 2>&1 || true',
        'ethtool -K vwifi_phy tx off rx off >/dev/null 2>&1 || true',
    ]
    if auto_connect:
      if ipv4_addr:
        cmds.append(f'ip addr replace {ipv4_addr}/24 dev wlan0')
      if ipv6_addr:
        cmds.append(f'ip -6 addr replace {ipv6_addr}/64 dev wlan0 nodad')
      st = self.server.get_station(station_id)
      if st is not None:
        st.state = 'completed'
    container.exec_run('sh -c "' + ' && '.join(cmds) + '"')
    container.exec_run(
        'python3 /dev/virtual_wifi/bin/vwifi_l2_agent.py '
        f'{station_id} vwifi_phy /dev/virtual_wifi/data.sock',
        detach=True,
    )

  def connect_station_to_ap(
      self, station_id: str, ssid: str, psk: str
  ) -> Dict[str, object]:
    """Authenticates with VirtualWiFiServer and configures L3 IPv4/IPv6."""
    res = self.server.authenticate_and_associate(station_id, ssid, psk)
    if not res.get('ok'):
      return res
    with self._lock:
      docker_node = self._station_nodes.get(station_id)
      ipv4_addr, ipv6_addr = self._station_ips.get(
          station_id, ('10.0.1.15', 'fd11:22::15')
      )
    container = getattr(docker_node, 'container', None) if docker_node else None
    if container is not None:
      suffix = ipv4_addr.split('.')[-1]
      ll_ipv6 = f'fe80::200:ff:fe00:2{int(suffix):02x}'
      cmds = [
          'ip link set dev vwifi_phy up',
          'ip link set dev wlan0 up',
          f'ip -6 addr replace {ll_ipv6}/64 dev wlan0 scope link nodad',
          f'ip -6 addr replace {ipv6_addr}/64 dev wlan0 nodad',
          f'ip addr replace {ipv4_addr}/24 dev wlan0',
      ]
      container.exec_run('sh -c "' + ' && '.join(cmds) + '"')
    return res

  def disconnect_station(self, station_id: str) -> None:
    self.server.disconnect_station(station_id)
    with self._lock:
      docker_node = self._station_nodes.get(station_id)
    container = getattr(docker_node, 'container', None) if docker_node else None
    if container is not None:
      container.exec_run('sh -c "ip addr flush dev wlan0 || true"')

  def unregister_station(self, station_id: str) -> None:
    with self._lock:
      self._station_nodes.pop(station_id, None)
      self._station_ips.pop(station_id, None)
    self.server.unregister_station(station_id)

  def stop_all(self) -> None:
    self._control_proxy.stop()
    self._data_proxy.stop()
