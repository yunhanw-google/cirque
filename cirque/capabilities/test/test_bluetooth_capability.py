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
"""Unit and E2E tests for the Cirque TCP/IP Virtual Bluetooth Controller System."""

import os
import shutil
import subprocess
import tempfile
import termios
import time
import unittest

from cirque.capabilities.bluetoothcapability import BlueToothCapability
from cirque.virtual_bt import (
    H4Packet,
    H4PacketType,
    H4StreamParser,
    H4TcpClient,
    HciOpcode,
    VirtualBluetoothServer,
    VirtualBtControlClient,
)
from cirque.virtual_bt.docker_hci_bridge import DockerVirtualBtManager


class TestVirtualBluetoothControllerSystem(unittest.TestCase):
  """Tests Control, HCI (H4), Link Layer (PHY), and Docker HCI/BlueZ bridges."""

  def setUp(self):
    self.server = VirtualBluetoothServer(
        host='127.0.0.1', control_port=0, hci_port=0, phy_port=0
    )
    self.control_port, self.hci_port, self.phy_port = self.server.start()
    self.control_client = VirtualBtControlClient('127.0.0.1', self.control_port)

  def tearDown(self):
    self.server.stop()
    BlueToothCapability.stop_virtual_server()
    BlueToothCapability.BLE_ADAPTS_LIST.clear()

  def test_h4_stream_parser_fragmented_bytes(self):
    """Verifies H4StreamParser reassembles 1-byte TCP fragments accurately."""
    parser = H4StreamParser()
    pkt1 = H4Packet(H4PacketType.COMMAND, b'\x03\x0c\x00')  # HCI_Reset
    pkt2 = H4Packet(
        H4PacketType.ACL_DATA, b'\x40\x20\x06\x00\x02\x00\x04\x00\xaa\xbb'
    )
    combined = pkt1.to_bytes() + pkt2.to_bytes()

    recovered = []
    for i in range(len(combined)):
      recovered.extend(parser.feed(combined[i : i + 1]))

    self.assertEqual(len(recovered), 2)
    self.assertEqual(recovered[0].packet_type, H4PacketType.COMMAND)
    self.assertEqual(recovered[0].payload, b'\x03\x0c\x00')
    self.assertEqual(recovered[1].packet_type, H4PacketType.ACL_DATA)
    self.assertEqual(recovered[1].payload, pkt2.payload)

  def test_h4_client_survives_idle_longer_than_connect_timeout(self):
    """Regression: connect timeout must not kill the rx thread when idle.

    Previously BlueZ StartDiscovery failed with "HCI opcode 0x200B timed
    out" once the bridge link had been idle for more than 5 seconds.
    """
    ctrl = self.server.create_controller(dedicated_port=True)
    client = H4TcpClient('127.0.0.1', ctrl.dedicated_hci_port, timeout=0.2)
    try:
      time.sleep(0.6)
      client.start_scanning(active=True)
      self.assertEqual(client.read_bd_addr(), ctrl.state.bd_addr)
    finally:
      client.close()

  def test_docker_pty_is_raw_and_does_not_stall_bridge(self):
    """Verifies the HCI PTY does not echo and an unread PTY is harmless."""
    runtime_dir = tempfile.mkdtemp(prefix='cirque_vbt_test_')
    manager = DockerVirtualBtManager(
        '127.0.0.1',
        self.control_port,
        self.hci_port,
        self.phy_port,
        runtime_dir=runtime_dir,
    )
    try:
      ctrl = self.server.create_controller(dedicated_port=True)
      _, bridge = manager.register_controller(
          ctrl.state.controller_id,
          ctrl.state.bd_addr,
          ctrl.dedicated_hci_port,
      )
      pty_bridge = manager.pty_bridges[ctrl.state.controller_id]
      lflag = termios.tcgetattr(pty_bridge.slave_fd)[3]
      self.assertFalse(lflag & termios.ECHO)
      self.assertFalse(lflag & termios.ICANON)
      # Nobody reads the PTY slave; the D-Bus bridge must keep working.
      for _ in range(200):
        bridge.h4_client.read_bd_addr()
      bridge.h4_client.start_scanning(active=True)
    finally:
      manager.stop_all()
      shutil.rmtree(runtime_dir, ignore_errors=True)

  def test_control_channel_device_lifecycle_and_scenarios(self):
    """Verifies Control/Test TCP channel commands for device & radio management."""
    ping = self.control_client.request({'cmd': 'ping'})
    self.assertEqual(ping['status'], 'ok')
    self.assertEqual(ping['hci_port'], self.hci_port)

    c0 = self.control_client.create_controller(
        controller_id='hci0', bd_addr='AA:BB:CC:11:22:01'
    )
    self.assertEqual(c0['status'], 'ok')
    self.assertEqual(c0['controller']['bd_addr'], 'AA:BB:CC:11:22:01')

    c1 = self.control_client.create_controller(
        controller_id='hci1', bd_addr='AA:BB:CC:11:22:02'
    )
    self.assertEqual(c1['status'], 'ok')

    ctrls = self.control_client.list_controllers()
    self.assertEqual(len(ctrls), 2)

    rssi_resp = self.control_client.set_rssi(
        -68, 'AA:BB:CC:11:22:01', 'AA:BB:CC:11:22:02'
    )
    self.assertEqual(rssi_resp['status'], 'ok')
    self.assertEqual(
        self.server.phy_hub.get_rssi('AA:BB:CC:11:22:01', 'AA:BB:CC:11:22:02'),
        -68,
    )

    del_resp = self.control_client.destroy_controller('hci0')
    self.assertTrue(del_resp['removed'])
    self.assertEqual(len(self.control_client.list_controllers()), 1)

  def test_e2e_ble_advertising_connection_and_matter_btp_data(self):
    """Verifies E2E BLE advertising, scanning, connection, and ACL/BTP over TCP."""
    peripheral = H4TcpClient('127.0.0.1', self.hci_port)
    central = H4TcpClient('127.0.0.1', self.hci_port)
    try:
      peripheral.reset()
      central.reset()

      periph_addr = peripheral.read_bd_addr()
      central_addr = central.read_bd_addr()
      self.assertNotEqual(periph_addr, central_addr)

      self.control_client.set_rssi(-52, periph_addr, central_addr)

      matter_adv_payload = bytes.fromhex('0201060b16f6ff00000f5a23010000')
      peripheral.start_advertising(matter_adv_payload)
      central.start_scanning(active=True)

      adv_report = central.wait_for_advertisement(target_bd_addr=periph_addr)
      self.assertEqual(adv_report['bd_addr'], periph_addr)
      self.assertEqual(adv_report['adv_data'], matter_adv_payload)
      self.assertEqual(adv_report['rssi'], -52)

      central_handle = central.connect(periph_addr)
      periph_handle = peripheral.wait_for_connection(central_addr)
      self.assertGreater(central_handle, 0)
      self.assertGreater(periph_handle, 0)

      btp_handshake_req = bytes.fromhex('656c04000000f40005')
      central.send_acl_l2cap(central_handle, 0x0004, btp_handshake_req)
      rx_handle, rx_cid, rx_payload = peripheral.receive_acl_l2cap()
      self.assertEqual(rx_handle, periph_handle)
      self.assertEqual(rx_cid, 0x0004)
      self.assertEqual(rx_payload, btp_handshake_req)

      btp_handshake_rsp = bytes.fromhex('656c0400f40005')
      peripheral.send_acl_l2cap(periph_handle, 0x0004, btp_handshake_rsp)
      c_handle, c_cid, c_payload = central.receive_acl_l2cap()
      self.assertEqual(c_handle, central_handle)
      self.assertEqual(c_cid, 0x0004)
      self.assertEqual(c_payload, btp_handshake_rsp)
    finally:
      peripheral.close()
      central.close()

  def test_cross_instance_link_layer_phy_tcp_bridge(self):
    """Verifies two independent VirtualBluetoothServer instances bridged via Link Layer TCP Channel."""
    server_b = VirtualBluetoothServer(
        host='127.0.0.1', control_port=0, hci_port=0, phy_port=0
    )
    _, hci_port_b, phy_port_b = server_b.start()
    try:
      bridge_res = self.control_client.bridge_remote_phy(
          '127.0.0.1', phy_port_b
      )
      self.assertEqual(bridge_res['status'], 'ok')

      host_on_a = H4TcpClient('127.0.0.1', self.hci_port)
      host_on_b = H4TcpClient('127.0.0.1', hci_port_b)
      try:
        host_on_a.reset()
        host_on_b.reset()
        addr_a = host_on_a.read_bd_addr()
        addr_b = host_on_b.read_bd_addr()

        host_on_a.start_advertising(b'\x02\x01\x06CrossHostBLE')
        host_on_b.start_scanning(active=True)

        report = host_on_b.wait_for_advertisement(target_bd_addr=addr_a)
        self.assertEqual(report['bd_addr'], addr_a)

        handle_b = host_on_b.connect(addr_a)
        handle_a = host_on_a.wait_for_connection(addr_b)

        host_on_b.send_acl_l2cap(handle_b, 0x0004, b'PING_ACROSS_PHY_TCP')
        _, cid, data = host_on_a.receive_acl_l2cap()
        self.assertEqual(cid, 0x0004)
        self.assertEqual(data, b'PING_ACROSS_PHY_TCP')
      finally:
        host_on_a.close()
        host_on_b.close()
    finally:
      server_b.stop()

  def test_cirque_bluetooth_capability_and_docker_hci_visibility(self):
    """Verifies BlueToothCapability exposes Docker HCI PTY and hciconfig."""
    cap0 = BlueToothCapability(use_virtual_bt_tcp=True)
    cap1 = BlueToothCapability(use_virtual_bt_tcp=True)
    try:
      self.assertEqual(cap0.name, 'Bluetooth')
      self.assertEqual(cap0.ble_adapt, 'hci0')
      self.assertEqual(cap0.ble_adapt_id, 0)
      self.assertEqual(cap1.ble_adapt, 'hci1')
      self.assertEqual(cap1.ble_adapt_id, 1)

      args0 = cap0.get_docker_run_args(None)
      self.assertEqual(args0['environment']['VIRTUAL_BT_ENABLED'], '1')
      self.assertEqual(args0['environment']['BLE_ADAPT'], 'hci0')
      self.assertEqual(args0['environment']['BLE_ADAPT_ID'], '0')
      self.assertTrue(os.path.exists(cap0.pty_device_path))
      self.assertTrue(os.path.exists(cap1.pty_device_path))

      # Verify inside-Docker hciconfig CLI reports hci0 and hci1
      hciconfig_bin = cap0.description['hciconfig_path']
      self.assertTrue(os.path.exists(hciconfig_bin))
      out = subprocess.check_output([hciconfig_bin], text=True)
      self.assertIn('hci0:\tType: Primary  Bus: Virtual', out)
      self.assertIn('hci1:\tType: Primary  Bus: Virtual', out)
      self.assertIn('UP RUNNING PSCAN ISCAN', out)

      # Verify per-container D-Bus directory and org.bluez.conf are mounted
      # without forcing network_mode: host (preserving docker_network: Ipv6).
      self.assertIn(
          '/tmp/cirque_virtual_bt/containers/hci0/dbus:/run/dbus',
          args0['volumes'],
      )
      self.assertIn(
          '/tmp/cirque_virtual_bt/org.bluez.conf:'
          '/etc/dbus-1/system.d/org.bluez.conf:ro',
          args0['volumes'],
      )
      self.assertNotIn('network_mode', args0)
      dbus_svc = BlueToothCapability._SHARED_BLUEZ_DBUS
      self.assertIsNotNone(dbus_svc)
      managed = dbus_svc._build_managed_objects_dict()
      self.assertIn('/org/bluez/hci0', managed)
      self.assertIn('/org/bluez/hci1', managed)

      self._verify_matter_ble_over_docker_bridges(cap1.bd_addr)
    finally:
      cap0.disable_capability(None)
      cap1.disable_capability(None)

  def _verify_matter_ble_over_docker_bridges(self, cap1_bd_addr):
    """Verifies DockerVirtualBtManager bridges perform Matter 0xFFF6 BLE."""
    mgr = BlueToothCapability._SHARED_DOCKER_MANAGER
    bridge_central = mgr.adapter_bridges['hci0']
    bridge_periph = mgr.adapter_bridges['hci1']

    bridge_periph.start_matter_advertising(discriminator=3584)
    found_addr = bridge_central.scan_for_matter_discriminator(
        discriminator=3584, timeout=3.0
    )
    self.assertEqual(found_addr, cap1_bd_addr)

    handle = bridge_central.connect_to_peripheral(found_addr)
    self.assertGreater(handle, 0)

    deadline = time.time() + 2.0
    while not bridge_periph.connected_handles and time.time() < deadline:
      time.sleep(0.05)
    self.assertTrue(bridge_periph.connected_handles)
    periph_handle = next(iter(bridge_periph.connected_handles.keys()))

    bridge_central.write_c1_request(handle, b'\x65\x6c\x04\x00\x00\x00')
    deadline = time.time() + 2.0
    while not bridge_periph.rx_writes and time.time() < deadline:
      time.sleep(0.05)
    self.assertEqual(bridge_periph.rx_writes[0], b'\x65\x6c\x04\x00\x00\x00')

    ind_payload = b'\x65\x6c\x04\x00\xf4\x00'
    bridge_periph.send_c2_indication(periph_handle, ind_payload)
    deadline = time.time() + 2.0
    while not bridge_central.rx_indications and time.time() < deadline:
      time.sleep(0.05)
    self.assertEqual(bridge_central.rx_indications[0], ind_payload)


if __name__ == '__main__':
  unittest.main(verbosity=2)
