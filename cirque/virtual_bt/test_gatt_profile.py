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
"""Tests for BLE GATT profiles in the protocol-independent virtual BT core."""

import os
import struct
import subprocess
import sys
import time
from typing import Optional
import unittest
from unittest import mock

from cirque.virtual_bt import gatt_profile
from cirque.virtual_bt.adapter_bridge import VirtualBluezAdapterBridge
from cirque.virtual_bt.att_db import (
    GATT_CLIENT_CHAR_CONFIG_UUID16,
    AttDatabase,
)
from cirque.virtual_bt.gatt_profile import BleGattProfile
from cirque.virtual_bt.server import VirtualBluetoothServer

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# A made-up, non-Matter profile: 16-bit service 0xFEED with one write and one
# indicate characteristic. Service Data carries the discriminator as LE16.
_TEST_SERVICE_UUID16 = 0xFEED
_TEST_SERVICE_UUID = '0000feed-0000-1000-8000-00805f9b34fb'
_TEST_WRITE_UUID = '6e400002-b5a3-f393-e0a9-e50e24dcca9e'
_TEST_INDICATE_UUID = '6e400003-b5a3-f393-e0a9-e50e24dcca9e'


def _test_adv_data(discriminator: int) -> bytes:
  service_data = struct.pack('<HH', _TEST_SERVICE_UUID16, discriminator)
  return bytes([0x02, 0x01, 0x06, len(service_data) + 1, 0x16]) + service_data


def _test_discriminator(service_data: bytes) -> Optional[int]:
  if len(service_data) < 2:
    return None
  return struct.unpack('<H', service_data[:2])[0]


TEST_PROFILE = BleGattProfile(
    name='Test FEED',
    service_uuid=_TEST_SERVICE_UUID,
    service_uuid16=_TEST_SERVICE_UUID16,
    write_char_uuid=_TEST_WRITE_UUID,
    indicate_char_uuid=_TEST_INDICATE_UUID,
    build_adv_data=_test_adv_data,
    local_name=lambda d: f'FEED-{d}',
    discriminator_from_service_data=_test_discriminator,
    default_discriminator=7,
    default_service_data=b'\x07\x00',
)


def _test_profile_database() -> AttDatabase:
  db = AttDatabase(start_handle=1)
  svc = db.add_primary_service(_TEST_SERVICE_UUID)
  db.add_characteristic(svc, _TEST_WRITE_UUID, properties=0x08)
  db.add_characteristic(svc, _TEST_INDICATE_UUID, properties=0x20)
  db.add_descriptor(svc, GATT_CLIENT_CHAR_CONFIG_UUID16, is_cccd=True)
  return db


def _run_isolated(code: str) -> str:
  env = dict(os.environ, PYTHONPATH=_REPO_ROOT)
  proc = subprocess.run(
      [sys.executable, '-c', code],
      cwd=_REPO_ROOT,
      env=env,
      capture_output=True,
      text=True,
      timeout=60,
      check=False,
  )
  if proc.returncode != 0:
    raise AssertionError(f'subprocess failed: {proc.stderr}')
  return proc.stdout.strip()


class TestCoreIndependence(unittest.TestCase):
  """The virtual BT core must not import cirque.protocols at import time."""

  def test_importing_virtual_bt_core_does_not_import_protocols(self):
    out = _run_isolated(
        'import sys\n'
        'import cirque.virtual_bt\n'
        'import cirque.virtual_bt.adapter_bridge\n'
        'import cirque.virtual_bt.bluez_dbus_daemon\n'
        'import cirque.virtual_bt.bluez_gatt_helpers\n'
        'import cirque.virtual_bt.bluez_gatt_mixin\n'
        'import cirque.virtual_bt.docker_hci_bridge\n'
        'import cirque.capabilities.bluetoothcapability\n'
        'print(sorted(m for m in sys.modules'
        " if m.startswith('cirque.protocols')))\n"
    )
    self.assertEqual(out, '[]')

  def test_default_profile_falls_back_to_matter_when_unset(self):
    out = _run_isolated(
        'from cirque.virtual_bt import gatt_profile\n'
        'print(gatt_profile.get_default_profile().name)\n'
    )
    self.assertEqual(out, 'Matter BTP')

  def test_no_profile_available_raises(self):
    saved = gatt_profile._default_profile  # pylint: disable=protected-access
    try:
      gatt_profile.set_default_profile(None)
      with mock.patch.object(
          gatt_profile, 'FALLBACK_PROFILE_MODULES', ('cirque_no_such_module',)
      ):
        with self.assertRaises(RuntimeError):
          gatt_profile.get_default_profile()
    finally:
      gatt_profile.set_default_profile(saved)

  def test_explicit_default_profile_is_used(self):
    saved = gatt_profile._default_profile  # pylint: disable=protected-access
    try:
      gatt_profile.set_default_profile(TEST_PROFILE)
      bridge = VirtualBluezAdapterBridge('c0', 'AA:BB:CC:DD:EE:01', '127.0.0.1', 1)
      self.assertIs(bridge.profile, TEST_PROFILE)
    finally:
      gatt_profile.set_default_profile(saved)

  def test_legacy_matter_names_still_resolve(self):
    out = _run_isolated(
        'import cirque.virtual_bt as vbt\n'
        'from cirque.virtual_bt import docker_hci_bridge as dhb\n'
        'from cirque.protocols.matter import ble_bridge as mb\n'
        'assert dhb.MATTER_SERVICE_UUID_STR == mb.MATTER_SERVICE_UUID_STR\n'
        'assert vbt.build_matter_ble_adv_payload is mb.build_matter_ble_adv_payload\n'
        'assert issubclass(mb.VirtualBluezAdapterBridge, vbt.VirtualBluezAdapterBridge)\n'
        "for n in ('start_matter_advertising', 'stop_matter_advertising',\n"
        "          'scan_for_matter_discriminator', 'write_c1_request',\n"
        "          'send_c2_indication'):\n"
        '  assert hasattr(mb.VirtualBluezAdapterBridge, n), n\n'
        "print('ok')\n"
    )
    self.assertEqual(out, 'ok')


class TestProfileHelpers(unittest.TestCase):

  def test_find_service_data_16(self):
    adv = _test_adv_data(1234)
    self.assertEqual(TEST_PROFILE.parse_service_data(adv), struct.pack('<H', 1234))
    self.assertIsNone(gatt_profile.find_service_data_16(adv, 0xFFF6))

  def test_matter_profile_round_trip(self):
    from cirque.protocols.matter import ble_bridge  # pylint: disable=g-import-not-at-top

    profile = ble_bridge.MATTER_BLE_PROFILE
    adv = profile.build_adv_data(discriminator=0xABC, vendor_id=0xFFF1)
    svc = profile.parse_service_data(adv)
    self.assertEqual(profile.discriminator_from_service_data(svc), 0xABC)
    self.assertEqual(profile.local_name(0xABC), f'MATTER-{0xABC}')
    self.assertEqual(
        profile.discriminator_from_service_data(profile.default_service_data),
        profile.default_discriminator,
    )


class TestCustomProfileOverVirtualBt(unittest.TestCase):
  """A non-Matter profile works end to end over the virtual controllers."""

  def setUp(self):
    self.server = VirtualBluetoothServer(
        host='127.0.0.1', control_port=0, hci_port=0, phy_port=0
    )
    self.server.start()
    self.bridges = []

  def tearDown(self):
    for b in self.bridges:
      b.stop()
    self.server.stop()

  def _bridge(self, profile):
    ctrl = self.server.create_controller(dedicated_port=True)
    b = VirtualBluezAdapterBridge(
        controller_id=ctrl.state.controller_id,
        bd_addr=ctrl.state.bd_addr,
        host='127.0.0.1',
        dedicated_hci_port=ctrl.dedicated_hci_port,
        profile=profile,
    )
    b.start()
    self.bridges.append(b)
    return b

  def test_advertise_scan_discover_write_and_indicate(self):
    central = self._bridge(TEST_PROFILE)
    periph = self._bridge(TEST_PROFILE)
    periph.set_att_database(_test_profile_database())
    periph.start_profile_advertising(discriminator=4321)

    found = central.scan_for_discriminator(4321, timeout=3.0)
    self.assertIsNotNone(found)
    self.assertEqual(found.upper(), periph.bd_addr.upper())

    conn = central.connect_to_peripheral(periph.bd_addr)
    disc = central.discovered_gatt[conn]
    self.assertEqual(disc.write_value_handle, 3)
    self.assertEqual(disc.indicate_value_handle, 5)
    self.assertEqual(disc.indicate_cccd_handle, 6)
    self.assertTrue(disc.indications_enabled)

    central.write_request(conn, b'\x01\x02\x03')
    deadline = time.time() + 2.0
    while not periph.rx_writes and time.time() < deadline:
      time.sleep(0.05)
    self.assertEqual(periph.rx_writes, [b'\x01\x02\x03'])

    periph_conn = next(iter(periph.connected_handles))
    periph.send_indication(periph_conn, b'\xaa\xbb')
    deadline = time.time() + 2.0
    while not central.rx_indications and time.time() < deadline:
      time.sleep(0.05)
    self.assertEqual(central.rx_indications, [b'\xaa\xbb'])

  def test_matter_central_does_not_find_custom_service(self):
    from cirque.protocols.matter import ble_bridge  # pylint: disable=g-import-not-at-top

    central = self._bridge(ble_bridge.MATTER_BLE_PROFILE)
    periph = self._bridge(TEST_PROFILE)
    periph.set_att_database(_test_profile_database())
    periph.start_profile_advertising(discriminator=4321)
    with self.assertRaisesRegex(RuntimeError, 'not found'):
      central.connect_to_peripheral(periph.bd_addr)


if __name__ == '__main__':
  unittest.main()
