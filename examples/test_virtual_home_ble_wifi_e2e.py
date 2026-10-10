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
"""End-to-End Virtual Home Test with 2 Docker Nodes (Virtual BT + Wi-Fi).

Spawns a Cirque Virtual Home with:
  - `mobile_controller`: Docker container with Virtual Bluetooth (`hci0` over
    TCP `VirtualBtControllerServer`) and Virtual Wi-Fi (`wlan0` over TCP
    `VirtualWiFiServer` + `fi.w1.wpa_supplicant1` D-Bus).
  - `iot_end_device`: Docker container with Virtual Bluetooth (`hci0`) and
    Virtual Wi-Fi (`wlan0`).
  - `wifi_ap`: Virtual Wi-Fi Access Point (`CIRQUE_HOME_AP`).

Verifies:
  1. Cross-container BLE discovery over BlueZ D-Bus (`org.bluez.Adapter1` &
     `org.bluez.Device1`) without `btvirt` or kernel `vhci`.
  2. Cross-container Wi-Fi scanning, WPA2-PSK 4-way handshake association
     (`State == 'completed'`), DHCP IPv4 lease assignment (`10.0.1.x/24`), and
     0% packet loss ICMP ping between `mobile_controller` and `iot_end_device`
     over `wlan0` without `mac80211_hwsim`.
"""

import os
import re
import subprocess
from typing import Dict, Optional
import unittest

from cirque.home.home import CirqueHome
from cirque.home.virtual_home_topology import VirtualHomeTopology

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VALIDATE_SCRIPT_PATH = os.path.join(SCRIPT_DIR, 'validate_virtual_home.sh')


class TestVirtualHomeBleWiFiE2E(unittest.TestCase):
  """E2E test for a 2-Docker-node + AP Virtual Home with Virtual BT & Wi-Fi."""

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    cls.base_image = os.environ.get(
        'CIRQUE_E2E_IMAGE', 'cirque-device-base:latest'
    )
    cls.ssid = 'CIRQUE_HOME_AP'
    cls.wifi_psk = 'cirque_home_psk'
    cls.home = CirqueHome()
    cls.home_config = VirtualHomeTopology.default_two_node_ble_wifi_config(
        base_image=cls.base_image,
        ssid=cls.ssid,
        wifi_psk=cls.wifi_psk,
    )
    try:
      cls.home.create_home(cls.home_config)
    except Exception:
      cls.home.destroy_home()
      raise
    cls.controller_id = ''
    cls.device_id = ''
    cls.controller_container = ''
    cls.device_container = ''
    for dev_id, node in cls.home.home['devices'].items():
      if node.type == 'MobileController':
        cls.controller_id = dev_id
        cls.controller_container = getattr(node.container, 'name', node.name)
      elif node.type == 'IoTEndDevice':
        cls.device_id = dev_id
        cls.device_container = getattr(node.container, 'name', node.name)

  @classmethod
  def tearDownClass(cls):
    if hasattr(cls, 'home') and cls.home is not None:
      cls.home.destroy_home()
    super().tearDownClass()

  def test_01_virtual_home_containers_created(self):
    self.assertTrue(self.controller_id, 'MobileController container missing')
    self.assertTrue(self.device_id, 'IoTEndDevice container missing')
    self.assertEqual(len(self.home.home['devices']), 3)

  def test_02_virtual_bluetooth_cross_container_discovery(self):
    bt_res = VirtualHomeTopology.verify_virtual_bt_between_nodes(
        self.home, self.controller_id, self.device_id
    )
    self.assertIn('AA:BB:CC:DD:EE:01', bt_res['controller_bt'])
    self.assertIn('AA:BB:CC:DD:EE:02', bt_res['device_bt'])
    self.assertIn(
        '/org/bluez/hci0/dev_AA_BB_CC_DD_EE_02', bt_res['controller_bt']
    )
    self.assertIn('AA:BB:CC:DD:EE:02', bt_res['controller_bt'])

  def test_03_virtual_wifi_wpa2_and_cross_container_data_plane(self):
    wifi_res = (
        VirtualHomeTopology.verify_virtual_wifi_commissioning_and_data_plane(
            self.home,
            self.controller_id,
            self.device_id,
            self.ssid,
            self.wifi_psk,
        )
    )
    self.assertIn('completed', wifi_res['controller_wpa'])
    self.assertIn('completed', wifi_res['device_wpa'])
    self.assertTrue(wifi_res['controller_ip'].startswith('10.0.1.'))
    self.assertTrue(wifi_res['device_ip'].startswith('10.0.1.'))
    self.assertNotEqual(wifi_res['controller_ip'], wifi_res['device_ip'])
    self.assertTrue(
        wifi_res['packet_loss_zero'],
        f"Cross-container wlan0 ping failed: {wifi_res['ping_output']}",
    )

  def test_04_validate_virtual_home_cli_script_gatt_ble_and_wifi(self):
    proc = subprocess.run(
        [
            VALIDATE_SCRIPT_PATH,
            self.controller_container,
            self.device_container,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(
        proc.returncode,
        0,
        f'validate_virtual_home.sh failed:\nSTDOUT:\n{proc.stdout}\n'
        f'STDERR:\n{proc.stderr}',
    )
    self.assertIn(
        'SUCCESS: Virtual Bluetooth (GATT + BLE) and Virtual Wi-Fi Validated!',
        proc.stdout,
    )
    self.assertIn('0000fff6-0000-1000-8000-00805f9b34fb', proc.stdout)
    self.assertIn('0% packet loss', proc.stdout)


class TestRealChipBleWiFiE2E(unittest.TestCase):
  """E2E test for real compiled chip-tool and chip-all-clusters-app over Virtual BT/Wi-Fi."""

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    chip_vbt_host = os.environ.get(
        'CHIP_BUILD_ROOT', os.environ.get('CHIP_VBT_PATH', '')
    )
    if not chip_vbt_host or not os.path.isdir(chip_vbt_host):
      raise unittest.SkipTest(
          'CHIP_BUILD_ROOT / CHIP_VBT_PATH not set or directory not found'
      )

    cls.base_image = os.environ.get(
        'CIRQUE_E2E_IMAGE', 'cirque-device-base:latest'
    )
    cls.ssid = 'CIRQUE_HOME_AP'
    cls.wifi_psk = 'cirque_home_psk'
    cls.home = CirqueHome()
    cls.home_config = VirtualHomeTopology.default_two_node_ble_wifi_config(
        base_image=cls.base_image,
        ssid=cls.ssid,
        wifi_psk=cls.wifi_psk,
    )
    try:
      cls.home.create_home(cls.home_config)
    except Exception:
      cls.home.destroy_home()
      raise
    cls.controller_id = ''
    cls.device_id = ''
    cls.controller_container = ''
    cls.device_container = ''
    for dev_id, node in cls.home.home['devices'].items():
      if node.type == 'MobileController':
        cls.controller_id = dev_id
        cls.controller_container = getattr(node.container, 'name', node.name)
      elif node.type == 'IoTEndDevice':
        cls.device_id = dev_id
        cls.device_container = getattr(node.container, 'name', node.name)

  @classmethod
  def tearDownClass(cls):
    if hasattr(cls, 'home') and cls.home is not None:
      cls.home.destroy_home()
    super().tearDownClass()

  def _assert_real_commissioning(
      self,
      res: Dict[str, object],
      bt_before: Dict[str, int],
      bt_after: Dict[str, int],
      wifi_before: Optional[Dict[str, int]] = None,
      wifi_after: Optional[Dict[str, int]] = None,
  ) -> None:
    """Rigorous external oracle asserting genuine CHIP commissioning and CASE over wlan0."""
    self.assertEqual(
        res.get('status'), 'success', f'Commissioning failed: {res}'
    )
    self.assertEqual(res.get('exit_code'), 0)
    self.assertEqual(res.get('phase'), 'operational_interaction')
    self.assertIn(
        'Device commissioning completed', res.get('pairing_output', '')
    )

    # 1. Real wlan0 IP acquired via DHCP
    dev_ip = res.get('device_ip', '')
    if not dev_ip or not dev_ip.startswith('10.0.1.'):
      raise AssertionError(f'Device did not acquire real wlan0 IP: {dev_ip}')
    dev_ip_live = VirtualHomeTopology._read_wlan0_ipv4(
        self.home, self.device_id
    )
    if dev_ip_live != dev_ip or not dev_ip_live.startswith('10.0.1.'):
      raise AssertionError(f'Live wlan0 IP {dev_ip_live} != reported {dev_ip}')

    # 2. WPA state is completed
    dev_wpa = VirtualHomeTopology._read_wpa_state(self.home, self.device_id)
    if 'completed' not in dev_wpa:
      raise AssertionError(
          f'Device wpa_supplicant state is not completed: {dev_wpa}'
      )

    # 3. Bluetooth ATT write packet delta > 0
    att_delta = bt_after['att_write_packets'] - bt_before['att_write_packets']
    if att_delta <= 0:
      raise AssertionError(
          f'Expected positive ATT write delta, got {att_delta}'
      )

    # 4. Operational CASE routing over %wlan0 (none over %eth0)
    toggle_out = res.get('toggle_output', '')
    read_out = res.get('read_output', '')
    combined_ops = toggle_out + '\n' + read_out

    if 'CASE_Sigma1' not in combined_ops:
      raise AssertionError('No CASE_Sigma1 message found in toggle/read output')

    tx_udp_peers = re.findall(r'Msg TX .*?\[UDP:([^\]]+)\]', combined_ops)
    if not tx_udp_peers:
      raise AssertionError(
          'No Msg TX [UDP:...] lines found in toggle/read output'
      )

    for peer in tx_udp_peers:
      is_wlan0 = (
          '%wlan0' in peer
          or '10.0.1.' in peer
          or 'fd11:22::' in peer
      )
      if not is_wlan0:
        raise AssertionError(f"Msg TX peer '{peer}' does not route over wlan0")
      if '%eth0' in peer:
        raise AssertionError(f"Msg TX peer '{peer}' contains forbidden %eth0")

    # 5. Operational command execution & data-plane ping
    self.assertEqual(res.get('toggle_exit_code'), 0)
    self.assertEqual(res.get('read_exit_code'), 0)
    self.assertIn('OnOff: TRUE', read_out)
    self.assertTrue(
        res.get('packet_loss_zero', False),
        f"Ping failed: {res.get('ping_output')}",
    )

    # 6. Virtual Wi-Fi L2 frame counters delta > 0
    if wifi_before is not None and wifi_after is not None:
      wifi_delta = wifi_after['total_packets'] - wifi_before['total_packets']
      if wifi_delta <= 0:
        raise AssertionError(
            f'Expected positive virtual Wi-Fi frame delta, got {wifi_delta}'
        )

    # 7. Relayed UDP 5540 frame delta > 0 across operational toggle/read window
    ops_before = res.get('ops_wifi_before')
    ops_after = res.get('ops_wifi_after')
    if ops_before is not None and ops_after is not None:
      udp5540_delta = ops_after.get(
          'relayed_udp5540_frames', 0
      ) - ops_before.get('relayed_udp5540_frames', 0)
      if udp5540_delta <= 0:
        raise AssertionError(
            'Expected positive relayed udp5540 frame delta, got'
            f' {udp5540_delta} (before={ops_before}, after={ops_after})'
        )

  def test_01_real_chip_ble_wifi_commissioning_and_operational_case(self):
    from cirque.capabilities.bluetoothcapability import BlueToothCapability
    from cirque.capabilities.wificapability import WiFiCapability

    server_bt = BlueToothCapability.get_or_start_virtual_server()
    server_wifi = WiFiCapability.get_or_start_virtual_server()

    counters_bt_before = server_bt.get_frame_counters()
    counters_wifi_before = server_wifi.get_frame_counters()

    res = VirtualHomeTopology.verify_real_chip_ble_wifi_commissioning(
        self.home,
        self.controller_id,
        self.device_id,
        node_id=1001,
        discriminator=3840,
        passcode=20202021,
        ssid=self.ssid,
        wifi_psk=self.wifi_psk,
    )

    # Save operational outputs to on-disk log files for verification
    toggle_log_path = '/tmp/test_01_toggle.log'
    with open(toggle_log_path, 'w', encoding='utf-8') as f:
      f.write(res.get('toggle_output', ''))

    counters_bt_after = server_bt.get_frame_counters()
    counters_wifi_after = server_wifi.get_frame_counters()

    self._assert_real_commissioning(
        res,
        counters_bt_before,
        counters_bt_after,
        counters_wifi_before,
        counters_wifi_after,
    )

    ops_b = res.get('ops_wifi_before', {})
    ops_a = res.get('ops_wifi_after', {})
    delta_5540 = ops_a.get('relayed_udp5540_frames', 0) - ops_b.get(
        'relayed_udp5540_frames', 0
    )
    print(
        f'[test_01] Operational UDP 5540 frames delta: {delta_5540} '
        f'(before={ops_b.get("relayed_udp5540_frames", 0)}, '
        f'after={ops_a.get("relayed_udp5540_frames", 0)})'
    )

  def test_02_real_chip_negative_control_invalid_pin(self):
    from cirque.capabilities.bluetoothcapability import BlueToothCapability
    from cirque.capabilities.wificapability import WiFiCapability

    server = BlueToothCapability.get_or_start_virtual_server()
    server_wifi = WiFiCapability.get_or_start_virtual_server()
    counters_before = server.get_frame_counters()
    wifi_before = server_wifi.get_frame_counters()

    # Pass setup_pin_code=12345678 (invalid PIN) vs passcode=20202021
    res = VirtualHomeTopology.verify_real_chip_ble_wifi_commissioning(
        self.home,
        self.controller_id,
        self.device_id,
        node_id=1002,
        discriminator=3840,
        passcode=20202021,
        setup_pin_code=12345678,
        ssid=self.ssid,
        wifi_psk=self.wifi_psk,
    )
    counters_after = server.get_frame_counters()
    wifi_after = server_wifi.get_frame_counters()

    self.assertEqual(res['status'], 'failed')
    self.assertNotEqual(res['exit_code'], 0)
    self.assertEqual(res['phase'], 'pase_authentication')

    # Assert anchored E2 PASE failure in chip-tool output
    self.assertTrue(
        "Failed to verify peer's MAC" in res['pairing_output']
        or 'Secure Pairing Failed' in res['pairing_output'],
        f"Expected anchored PASE failure in output: {res['pairing_output']}",
    )
    self.assertNotIn('PASE establishment successful', res['pairing_output'])

    # Assert non-zero ATT write delta (BLE connected and exchanged PASE frames)
    att_delta = (
        counters_after['att_write_packets']
        - counters_before['att_write_packets']
    )
    self.assertGreater(
        att_delta, 0, 'Expected non-zero ATT write delta during failed PASE'
    )

    # Device must not have acquired an operational wlan0 IP from rejected commissioning
    dev_ip = VirtualHomeTopology._read_wlan0_ipv4(self.home, self.device_id)
    self.assertEqual(dev_ip, '')

    # Assert zero operational UDP 5540 frames relayed over Wi-Fi
    dev_node = self.home.devices.get(self.device_id)
    wifi_cap = (
        next(
            (
                c
                for c in dev_node.capabilities
                if getattr(c, 'name', '') == 'WiFi'
            ),
            None,
        )
        if dev_node
        else None
    )
    dev_st_id = getattr(wifi_cap, 'station_id', None)
    dev_5540 = 0
    if dev_st_id:
      dev_5540 = wifi_after.get('stations', {}).get(dev_st_id, {}).get(
          'relayed_udp5540_frames', 0
      ) - wifi_before.get('stations', {}).get(dev_st_id, {}).get(
          'relayed_udp5540_frames', 0
      )
    self.assertEqual(
        dev_5540,
        0,
        f'Expected 0 relayed udp5540 frames from device: {dev_5540}',
    )
    total_5540 = wifi_after.get('relayed_udp5540_frames', 0) - wifi_before.get(
        'relayed_udp5540_frames', 0
    )
    self.assertEqual(
        total_5540, 0, f'Expected 0 total relayed udp5540 frames: {total_5540}'
    )

  def test_03_real_chip_negative_control_invalid_wifi_psk(self):
    from cirque.capabilities.wificapability import WiFiCapability

    server_wifi = WiFiCapability.get_or_start_virtual_server()
    wifi_before = server_wifi.get_frame_counters()

    # Pass wrong_psk to device provisioning while controller keeps valid wifi_psk
    res = VirtualHomeTopology.verify_real_chip_ble_wifi_commissioning(
        self.home,
        self.controller_id,
        self.device_id,
        node_id=1003,
        discriminator=3840,
        passcode=20202021,
        ssid=self.ssid,
        wifi_psk=self.wifi_psk,
        device_wifi_psk='wrong_psk',
    )
    wifi_after = server_wifi.get_frame_counters()

    self.assertEqual(res['status'], 'failed')
    self.assertNotEqual(res['exit_code'], 0)
    self.assertEqual(
        res['phase'],
        'wifi_provisioning',
        f"Expected wifi_provisioning but got {res['phase']}.\nPAIRING"
        f" OUTPUT:\n{res.get('pairing_output')}\nAPP"
        f" LOG:\n{res.get('app_log')}",
    )

    # Assert anchored ConnectNetwork failure / non-zero status
    self.assertIn('PASE establishment successful', res['pairing_output'])
    status = res.get('connect_network_status')
    self.assertIsNotNone(
        status,
        f"Expected connect_network_status in result: {res['pairing_output']}",
    )
    self.assertNotEqual(
        status, 0, f'Expected non-zero network status, got {status}'
    )

    # Controller wlan0 IP must remain valid and intact
    ctrl_ip = VirtualHomeTopology._read_wlan0_ipv4(
        self.home, self.controller_id
    )
    self.assertTrue(
        ctrl_ip.startswith('10.0.1.'),
        f'Controller lost wlan0 IP: {ctrl_ip}',
    )

    # Device must not have acquired an operational wlan0 IP
    dev_ip = VirtualHomeTopology._read_wlan0_ipv4(self.home, self.device_id)
    self.assertEqual(dev_ip, '')

    # Assert zero operational UDP 5540 frames relayed over Wi-Fi
    dev_node = self.home.devices.get(self.device_id)
    wifi_cap = (
        next(
            (
                c
                for c in dev_node.capabilities
                if getattr(c, 'name', '') == 'WiFi'
            ),
            None,
        )
        if dev_node
        else None
    )
    dev_st_id = getattr(wifi_cap, 'station_id', None)
    dev_5540 = 0
    if dev_st_id:
      dev_5540 = wifi_after.get('stations', {}).get(dev_st_id, {}).get(
          'relayed_udp5540_frames', 0
      ) - wifi_before.get('stations', {}).get(dev_st_id, {}).get(
          'relayed_udp5540_frames', 0
      )
    self.assertEqual(
        dev_5540,
        0,
        f'Expected 0 relayed udp5540 frames from device: {dev_5540}',
    )
    total_5540 = wifi_after.get('relayed_udp5540_frames', 0) - wifi_before.get(
        'relayed_udp5540_frames', 0
    )
    self.assertEqual(
        total_5540, 0, f'Expected 0 total relayed udp5540 frames: {total_5540}'
    )

  def test_04_real_chip_negative_control_ble_relay_disabled(self):
    from cirque.capabilities.bluetoothcapability import BlueToothCapability
    from cirque.capabilities.wificapability import WiFiCapability

    server = BlueToothCapability.get_or_start_virtual_server()
    server_wifi = WiFiCapability.get_or_start_virtual_server()

    # Precondition: clean device state and restart chip-all-clusters-app
    VirtualHomeTopology.clean_chip_device_state_and_restart(
        self.home,
        self.device_id,
        self.controller_id,
        discriminator=3840,
        passcode=20202021,
    )

    server.set_relay_enabled(False)
    counters_before = server.get_frame_counters()
    wifi_before = server_wifi.get_frame_counters()
    try:
      res = VirtualHomeTopology.verify_real_chip_ble_wifi_commissioning(
          self.home,
          self.controller_id,
          self.device_id,
          node_id=1004,
          discriminator=3840,
          passcode=20202021,
          ssid=self.ssid,
          wifi_psk=self.wifi_psk,
          restart_app=False,
      )
    finally:
      server.set_relay_enabled(True)

    counters_after = server.get_frame_counters()
    wifi_after = server_wifi.get_frame_counters()

    self.assertEqual(res['status'], 'failed')
    self.assertNotEqual(res['exit_code'], 0)
    self.assertEqual(res['phase'], 'ble_discovery_timeout')
    self.assertNotIn('PBKDFParamResponse', res['pairing_output'])
    self.assertNotIn('PASE establishment successful', res['pairing_output'])

    # With relay disabled, zero ATT write packets could be delivered
    att_delta = (
        counters_after['att_write_packets']
        - counters_before['att_write_packets']
    )
    self.assertEqual(
        att_delta,
        0,
        f'Expected 0 ATT writes with relay disabled, got {att_delta}',
    )

    # Assert zero operational UDP 5540 frames relayed over Wi-Fi
    dev_node = self.home.devices.get(self.device_id)
    wifi_cap = (
        next(
            (
                c
                for c in dev_node.capabilities
                if getattr(c, 'name', '') == 'WiFi'
            ),
            None,
        )
        if dev_node
        else None
    )
    dev_st_id = getattr(wifi_cap, 'station_id', None)
    dev_5540 = 0
    if dev_st_id:
      dev_5540 = wifi_after.get('stations', {}).get(dev_st_id, {}).get(
          'relayed_udp5540_frames', 0
      ) - wifi_before.get('stations', {}).get(dev_st_id, {}).get(
          'relayed_udp5540_frames', 0
      )
    self.assertEqual(
        dev_5540,
        0,
        f'Expected 0 relayed udp5540 frames from device: {dev_5540}',
    )
    total_5540 = wifi_after.get('relayed_udp5540_frames', 0) - wifi_before.get(
        'relayed_udp5540_frames', 0
    )
    self.assertEqual(
        total_5540, 0, f'Expected 0 total relayed udp5540 frames: {total_5540}'
    )

  def test_05_mutation_check_print_only_standin_fails(self):
    from unittest.mock import patch
    from cirque.capabilities.bluetoothcapability import BlueToothCapability
    from cirque.capabilities.wificapability import WiFiCapability

    server_bt = BlueToothCapability.get_or_start_virtual_server()
    server_wifi = WiFiCapability.get_or_start_virtual_server()

    # Precondition: Clean device state and restart chip-all-clusters-app
    VirtualHomeTopology.clean_chip_device_state_and_restart(
        self.home,
        self.device_id,
        self.controller_id,
        discriminator=3840,
        passcode=20202021,
    )

    # Obviously synthetic fixture simulating a print-only mock tool
    fake_chip_tool_output = (
        'CHIP:CTL: Device commissioning completed\n'
        '[1790727000.000] [TOO] Commissioning success\n'
        '<<< [E:0000i S:0 M:0] (U) Msg TX from 0000000000000000 to'
        ' 0:0000000000000000 [0000] [UDP:[fe80::1%wlan0]:5540]'
        ' --- Type 0000:30 (SecureChannel:CASE_Sigma1) (B:000)\n'
        'CHIP:DMG: Endpoint 1, Cluster 0x0000_0006 Action OnOff: TRUE\n'
    )

    original_exec = VirtualHomeTopology._exec_in_node_with_exit_code

    def fake_exec(cirque_home, node_id, cmd):
      if 'chip-tool' in cmd:
        return 0, fake_chip_tool_output
      return original_exec(cirque_home, node_id, cmd)

    bt_before = server_bt.get_frame_counters()
    wifi_before = server_wifi.get_frame_counters()

    with patch.object(
        VirtualHomeTopology,
        '_exec_in_node_with_exit_code',
        side_effect=fake_exec,
    ):
      res = VirtualHomeTopology.verify_real_chip_ble_wifi_commissioning(
          self.home,
          self.controller_id,
          self.device_id,
          node_id=1005,
          discriminator=3840,
          passcode=20202021,
          ssid=self.ssid,
          wifi_psk=self.wifi_psk,
          restart_app=False,
      )

    bt_after = server_bt.get_frame_counters()
    wifi_after = server_wifi.get_frame_counters()

    # Independently evaluate each external oracle against the mutated result
    failed_oracles = []

    # 1. wlan0 IP oracle (real IP acquired via DHCP on wlan0)
    dev_ip = res.get('device_ip', '')
    dev_ip_live = VirtualHomeTopology._read_wlan0_ipv4(
        self.home, self.device_id
    )
    if not (
        dev_ip
        and dev_ip.startswith('10.0.1.')
        and dev_ip_live.startswith('10.0.1.')
    ):
      failed_oracles.append('wlan0 IP oracle')

    # 2. ATT delta oracle (real Bluetooth ATT writes during PASE)
    att_delta = bt_after['att_write_packets'] - bt_before['att_write_packets']
    if att_delta <= 0:
      failed_oracles.append('ATT delta oracle')

    # 3. %wlan0 CASE oracle (on real operational toggle/read output)
    toggle_out = res.get('toggle_output', '')
    read_out = res.get('read_output', '')
    combined_ops = toggle_out + '\n' + read_out
    has_real_case = 'CASE_Sigma1' in combined_ops and bool(
        re.search(r'Msg TX .*?\[UDP:.*?%wlan0.*?:5540\]', combined_ops)
    )
    if not has_real_case:
      failed_oracles.append('%wlan0 CASE oracle')

    # 4. Wi-Fi data-frame oracle (relayed data/udp5540 frames)
    relayed_delta = wifi_after.get('relayed_data_frames', 0) - wifi_before.get(
        'relayed_data_frames', 0
    )
    udp5540_delta = wifi_after.get(
        'relayed_udp5540_frames', 0
    ) - wifi_before.get('relayed_udp5540_frames', 0)
    if relayed_delta <= 0 or udp5540_delta <= 0:
      failed_oracles.append('Wi-Fi data-frame oracle')

    print(f'\n[Mutation Check] Failed independent oracles: {failed_oracles}')
    self.assertIn('wlan0 IP oracle', failed_oracles)
    self.assertIn('ATT delta oracle', failed_oracles)
    self.assertTrue(len(failed_oracles) >= 2)


class TestAndroidDockerNodeBleWiFiE2E(unittest.TestCase):
  """E2E test for AndroidDockerNode with /dev/kvm passthrough and real CHIP.
  """

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    chip_vbt_host = os.environ.get(
        'CHIP_BUILD_ROOT', os.environ.get('CHIP_VBT_PATH', '')
    )
    if not chip_vbt_host or not os.path.isdir(chip_vbt_host):
      raise unittest.SkipTest(
          'CHIP_BUILD_ROOT / CHIP_VBT_PATH not set or directory not found'
      )

    cls.base_image = os.environ.get(
        'CIRQUE_E2E_IMAGE', 'cirque-device-base:latest'
    )
    cls.ssid = 'CIRQUE_HOME_AP'
    cls.wifi_psk = 'cirque_home_psk'
    cls.home = CirqueHome()
    cls.home_config = (
        VirtualHomeTopology.default_android_two_node_ble_wifi_config(
            base_image=cls.base_image,
            ssid=cls.ssid,
            wifi_psk=cls.wifi_psk,
        )
    )
    try:
      cls.home.create_home(cls.home_config)
    except Exception:
      cls.home.destroy_home()
      raise
    cls.controller_id = ''
    cls.device_id = ''
    cls.android_node = None
    for dev_id, node in cls.home.home['devices'].items():
      if node.type == 'android_controller':
        cls.controller_id = dev_id
        cls.android_node = node
      elif node.type == 'IoTEndDevice':
        cls.device_id = dev_id

  @classmethod
  def tearDownClass(cls):
    if hasattr(cls, 'home') and cls.home is not None:
      cls.home.destroy_home()
    super().tearDownClass()

  def test_06_android_docker_node_kvm_passthrough_and_commissioning(self):
    from cirque.nodes.androiddockernode import AndroidDockerNode
    from cirque.capabilities.bluetoothcapability import BlueToothCapability
    from cirque.capabilities.wificapability import WiFiCapability

    # 1. Assert android_node is an instance of AndroidDockerNode
    self.assertIsInstance(self.android_node, AndroidDockerNode)

    # 2. Check /dev/kvm passthrough if present on host
    if os.path.exists('/dev/kvm'):
      self.assertEqual(self.android_node.runtime_mode, 'kvm_emulator')
      self.assertIn('/dev/kvm:/dev/kvm:rwm', self.android_node.devices)
      ec_c, _ = VirtualHomeTopology._exec_in_node_with_exit_code(
          self.home, self.controller_id, 'test -c /dev/kvm'
      )
      self.assertEqual(
          ec_c, 0, '/dev/kvm is not a character device in container'
      )
      stat_out = VirtualHomeTopology._exec_in_node(
          self.home, self.controller_id, "stat -c '%t:%T' /dev/kvm"
      ).strip()
      self.assertEqual(
          stat_out, 'a:e8', f'Unexpected /dev/kvm major:minor: {stat_out}'
      )
    if os.path.exists('/dev/net/tun'):
      self.assertIn('/dev/net/tun:/dev/net/tun:rwm', self.android_node.devices)
      ec_tun, _ = VirtualHomeTopology._exec_in_node_with_exit_code(
          self.home, self.controller_id, 'test -c /dev/net/tun'
      )
      self.assertEqual(
          ec_tun, 0, '/dev/net/tun is not a character device in container'
      )

    # 3. Sample Bluetooth and Wi-Fi frame counters before commissioning
    server_bt = BlueToothCapability.get_or_start_virtual_server()
    server_wifi = WiFiCapability.get_or_start_virtual_server()
    counters_bt_before = server_bt.get_frame_counters()
    counters_wifi_before = server_wifi.get_frame_counters()

    # 4. Commission device from android_controller using real chip-tool
    res = VirtualHomeTopology.verify_real_chip_ble_wifi_commissioning(
        self.home,
        self.controller_id,
        self.device_id,
        node_id=1006,
        discriminator=3840,
        passcode=20202021,
        ssid=self.ssid,
        wifi_psk=self.wifi_psk,
    )

    counters_bt_after = server_bt.get_frame_counters()
    counters_wifi_after = server_wifi.get_frame_counters()

    # 5. Assert real commissioning and operational interaction
    TestRealChipBleWiFiE2E._assert_real_commissioning(
        self,
        res,
        counters_bt_before,
        counters_bt_after,
        counters_wifi_before,
        counters_wifi_after,
    )


if __name__ == '__main__':
  unittest.main()
