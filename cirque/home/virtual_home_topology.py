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
"""Declarative Virtual Home Topology Builder & E2E Multi-Docker RF Verifier.

Provides reusable topology specifications and end-to-end verification routines
so external GitHub repositories and developers can spin up multi-container
Cirque Virtual Homes with kernel-module-free Virtual Bluetooth (`virtbt` over
TCP) and Virtual Wi-Fi (`virtual_wifi` over TCP + `fi.w1.wpa_supplicant1` D-Bus)
inside standard unprivileged CI runners (such as GitHub Actions
`ubuntu-latest`).
"""

from dataclasses import dataclass
import re
import time
from typing import Dict, List, Optional, Sequence


@dataclass(frozen=True)
class VirtualHomeNodeSpec:
  """Specification for a containerized node in a Cirque Virtual Home."""

  name: str
  device_type: str
  base_image: str = 'project-chip/chip-cirque-device-base'
  enable_bluetooth: bool = True
  enable_wifi: bool = True
  enable_thread: bool = False
  wifi_auto_connect: bool = False
  bd_addr: Optional[str] = None
  docker_network: str = 'Ipv6'

  def capabilities_list(self) -> List[str]:
    """Returns the ordered list of enabled Cirque capability names."""
    capabilities: List[str] = []
    if self.enable_bluetooth:
      capabilities.append('Bluetooth')
    if self.enable_wifi:
      capabilities.append('WiFi')
    if self.enable_thread:
      capabilities.append('Thread')
    return capabilities

  def to_device_config(self) -> Dict[str, object]:
    """Converts this immutable spec into a `CirqueHome` device dictionary."""
    config: Dict[str, object] = {
        'type': self.device_type,
        'base_image': self.base_image,
        'capability': self.capabilities_list(),
        'docker_network': self.docker_network,
        'use_virtual_bt_tcp': True,
        'use_virtual_wifi_tcp': True,
        'wifi_auto_connect': self.wifi_auto_connect,
    }
    if self.bd_addr:
      config['bd_addr'] = self.bd_addr
    return config


@dataclass(frozen=True)
class VirtualHomeApSpec:
  """Specification for a Virtual Wi-Fi Access Point node in a Cirque Home."""

  name: str = 'wifi_ap'
  ssid: str = 'CIRQUE_HOME_AP'
  wifi_psk: str = 'cirque_home_psk'
  base_image: str = 'project-chip/chip-cirque-device-base'
  docker_network: str = 'Ipv6'

  def is_wpa2_enabled(self) -> bool:
    """Returns True when a non-empty WPA2 pre-shared key is configured."""
    return bool(self.wifi_psk)

  def to_device_config(self) -> Dict[str, object]:
    """Converts this AP spec into a `CirqueHome` `wifi_ap` configuration."""
    return {
        'type': 'wifi_ap',
        'base_image': self.base_image,
        'ssid': self.ssid,
        'psk': self.wifi_psk,
        'docker_network': self.docker_network,
        'use_virtual_wifi_tcp': self.is_wpa2_enabled(),
    }


class VirtualHomeTopology:
  """Builder and E2E verification harness for Cirque Virtual Homes."""

  @staticmethod
  def build_home_config(
      nodes: Sequence[VirtualHomeNodeSpec],
      ap_spec: Optional[VirtualHomeApSpec] = None,
  ) -> Dict[str, Dict[str, object]]:
    """Constructs a complete `CirqueHome.create_home()` topology dictionary."""
    home_config: Dict[str, Dict[str, object]] = {}
    if ap_spec is not None:
      home_config[ap_spec.name] = ap_spec.to_device_config()
    for node_spec in nodes:
      home_config[node_spec.name] = node_spec.to_device_config()
    return home_config

  @classmethod
  def default_two_node_ble_wifi_config(
      cls,
      base_image: str = 'project-chip/chip-cirque-device-base',
      ssid: str = 'CIRQUE_HOME_AP',
      wifi_psk: str = 'cirque_home_psk',
  ) -> Dict[str, Dict[str, object]]:
    """Builds the canonical 2-Docker-node (Mobile + IoT) + AP topology."""
    ap_spec = VirtualHomeApSpec(
        name='wifi_ap', ssid=ssid, wifi_psk=wifi_psk, base_image=base_image
    )
    nodes = (
        VirtualHomeNodeSpec(
            name='mobile_controller',
            device_type='MobileController',
            base_image=base_image,
            bd_addr='AA:BB:CC:DD:EE:01',
        ),
        VirtualHomeNodeSpec(
            name='iot_end_device',
            device_type='IoTEndDevice',
            base_image=base_image,
            bd_addr='AA:BB:CC:DD:EE:02',
        ),
    )
    return cls.build_home_config(nodes, ap_spec)

  @staticmethod
  def _exec_in_node(cirque_home, node_id: str, cmd: str) -> str:
    """Executes a shell command inside a Cirque node and returns UTF-8 text."""
    result = cirque_home.execute_device_cmd(cmd, node_id, stream=False)
    output = result.output if hasattr(result, 'output') else result
    if isinstance(output, (bytes, bytearray)):
      return bytes(output).decode('utf-8', errors='replace')
    return str(output)

  @classmethod
  def verify_virtual_bt_between_nodes(
      cls, cirque_home, controller_id: str, device_id: str
  ) -> Dict[str, str]:
    """Verifies BlueZ D-Bus adapter discovery between two Docker containers.

    Steps executed inside the containers:
      1. Power on `/org/bluez/${BLE_ADAPT:-hci0}` via `Properties.Set`.
      2. Register BLE advertising (`/chipoble/adv0`) on `device_id` via
         `org.bluez.LEAdvertisingManager1.RegisterAdvertisement`.
      3. Trigger active BLE scanning on `controller_id` via
         `org.bluez.Adapter1.StartDiscovery`.
      4. Query `org.freedesktop.DBus.ObjectManager.GetManagedObjects` to verify
         that `controller_id` discovers `device_id` as an `org.bluez.Device1`
         object (`dev_AA_BB_CC_DD_EE_0X`).
    """
    power_cmd = (
        "sh -c 'dbus-send --system --dest=org.bluez --print-reply "
        '/org/bluez/${BLE_ADAPT:-hci0} '
        'org.freedesktop.DBus.Properties.Set '
        "string:org.bluez.Adapter1 string:Powered variant:boolean:true'"
    )
    adv_cmd = (
        "sh -c 'gdbus call --system --dest org.bluez "
        '--object-path /org/bluez/${BLE_ADAPT:-hci1} '
        '--method org.bluez.LEAdvertisingManager1.RegisterAdvertisement '
        "/chipoble/adv0 \"{}\"' "
    )
    scan_cmd = (
        "sh -c 'dbus-send --system --dest=org.bluez --print-reply "
        '/org/bluez/${BLE_ADAPT:-hci0} '
        "org.bluez.Adapter1.StartDiscovery'"
    )
    managed_objs_cmd = (
        'dbus-send --system --dest=org.bluez --print-reply / '
        'org.freedesktop.DBus.ObjectManager.GetManagedObjects'
    )
    cls._exec_in_node(cirque_home, device_id, power_cmd)
    cls._exec_in_node(cirque_home, device_id, adv_cmd)
    cls._exec_in_node(cirque_home, controller_id, power_cmd)
    cls._exec_in_node(cirque_home, controller_id, scan_cmd)
    time.sleep(0.3)
    ctrl_info = cls._exec_in_node(cirque_home, controller_id, managed_objs_cmd)
    dev_info = cls._exec_in_node(cirque_home, device_id, managed_objs_cmd)
    return {
        'controller_bt': ctrl_info.strip(),
        'device_bt': dev_info.strip(),
    }

  @classmethod
  def _associate_wpa_and_dhcp(
      cls, cirque_home, node_id: str, ssid: str, wifi_psk: str
  ) -> str:
    """Associates a node over fi.w1.wpa_supplicant1 D-Bus and runs dhcpcd.

    Mirrors the exact 4-step D-Bus sequence performed by Matter's Linux
    `NetworkCommissioningWiFiDriver`:
      1. `GetInterface("wlan0")` -> `/fi/w1/wpa_supplicant1/Interfaces/0`
      2. `Interface.Scan({'Type': 'active'})`
      3. `Interface.AddNetwork({'ssid': ssid, 'psk': wifi_psk})`
      4. `Interface.SelectNetwork(.../Interfaces/0/Networks/0)`
    """
    iface_path = '/fi/w1/wpa_supplicant1/Interfaces/0'
    wpa_steps = (
        (
            'gdbus call --system --dest fi.w1.wpa_supplicant1 '
            '--object-path /fi/w1/wpa_supplicant1 '
            '--method fi.w1.wpa_supplicant1.GetInterface wlan0'
        ),
        (
            'gdbus call --system --dest fi.w1.wpa_supplicant1 '
            f'--object-path {iface_path} '
            '--method fi.w1.wpa_supplicant1.Interface.Scan '
            "\"{'Type': <'active'>}\""
        ),
        (
            'gdbus call --system --dest fi.w1.wpa_supplicant1 '
            f'--object-path {iface_path} '
            '--method fi.w1.wpa_supplicant1.Interface.AddNetwork '
            f"\"{{'ssid': <'{ssid}'>, 'psk': <'{wifi_psk}'>}}\""
        ),
        (
            'gdbus call --system --dest fi.w1.wpa_supplicant1 '
            f'--object-path {iface_path} '
            '--method fi.w1.wpa_supplicant1.Interface.SelectNetwork '
            f'{iface_path}/Networks/0'
        ),
    )
    for step_cmd in wpa_steps:
      cls._exec_in_node(cirque_home, node_id, step_cmd)
    state_cmd = (
        'gdbus call --system --dest fi.w1.wpa_supplicant1 '
        f'--object-path {iface_path} '
        '--method org.freedesktop.DBus.Properties.Get '
        'fi.w1.wpa_supplicant1.Interface State'
    )
    wpa_state = cls._exec_in_node(cirque_home, node_id, state_cmd).strip()
    cls._exec_in_node(cirque_home, node_id, 'dhcpcd wlan0')
    return wpa_state

  @classmethod
  def _read_wlan0_ipv4(cls, cirque_home, node_id: str) -> str:
    """Returns the IPv4 address assigned to wlan0 inside a container node."""
    addr_out = cls._exec_in_node(
        cirque_home, node_id, 'ip -4 addr show dev wlan0'
    )
    ip_match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', addr_out)
    return ip_match.group(1) if ip_match else ''

  @classmethod
  def verify_virtual_wifi_commissioning_and_data_plane(
      cls,
      cirque_home,
      controller_id: str,
      device_id: str,
      *wifi_creds: str,
  ) -> Dict[str, object]:
    """Executes D-Bus Wi-Fi scan, WPA2 association, DHCP, and ping."""
    ssid = wifi_creds[0] if len(wifi_creds) >= 1 else 'CIRQUE_HOME_AP'
    wifi_psk = wifi_creds[1] if len(wifi_creds) >= 2 else 'cirque_home_psk'
    ctrl_wpa = cls._associate_wpa_and_dhcp(
        cirque_home, controller_id, ssid, wifi_psk
    )
    dev_wpa = cls._associate_wpa_and_dhcp(
        cirque_home, device_id, ssid, wifi_psk
    )
    time.sleep(0.5)
    ctrl_ip = cls._read_wlan0_ipv4(cirque_home, controller_id)
    dev_ip = cls._read_wlan0_ipv4(cirque_home, device_id)
    ping_out = cls._exec_in_node(
        cirque_home, controller_id, f'ping -c 2 -W 2 {dev_ip}'
    )
    return {
        'controller_wpa': ctrl_wpa,
        'device_wpa': dev_wpa,
        'controller_ip': ctrl_ip,
        'device_ip': dev_ip,
        'ping_output': ping_out.strip(),
        'packet_loss_zero': bool(
            re.search(r'(?<!\d)0%\s+packet\s+loss', ping_out)
        ),
    }
