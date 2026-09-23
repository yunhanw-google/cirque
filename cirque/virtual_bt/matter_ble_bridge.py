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
"""Matter BLE 0xFFF6 advertisement helpers and H4-over-TCP BlueZ adapter bridge."""

import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from cirque.virtual_bt.hci_h4 import HciOpcode
from cirque.virtual_bt.server import H4TcpClient

# Matter BLE Commissioning Service UUID (0xFFF6) and GATT Characteristic UUIDs
# defined in Matter Core Specification Section 5.4.2 ("Bluetooth LE").
MATTER_SERVICE_UUID_SHORT = 0xFFF6
MATTER_SERVICE_UUID_STR = '0000fff6-0000-1000-8000-00805f9b34fb'
# C1: Central -> Peripheral GATT Write Characteristic (BTP TX from Commissioner)
MATTER_C1_UUID_STR = '18ee2ef5-263d-4559-959f-4f9c429f9d11'
# C2: Peripheral -> Central GATT Indication Characteristic (BTP RX to Central)
MATTER_C2_UUID_STR = '18ee2ef5-263d-4559-959f-4f9c429f9d12'

# Fixed L2CAP Channel ID (0x0004 = Attribute Protocol) and ATT Opcodes/Handles.
ATT_CID = 0x0004
ATT_OP_WRITE_REQ = 0x12
ATT_OP_WRITE_RSP = 0x13
ATT_OP_HANDLE_VALUE_IND = 0x1D
ATT_OP_HANDLE_VALUE_CFM = 0x1E
ATT_HANDLE_C1_VALUE = 0x0012
ATT_HANDLE_C2_VALUE = 0x0014


def build_matter_ble_adv_payload(
    discriminator: int,
    vendor_id: int = 0xFFF1,
    product_id: int = 0x8001,
    additional_data: bool = False,
    extended_announcement: bool = False,
) -> bytes:
  """Builds Matter BLE 0xFFF6 Service Data AD payload (Core Spec 5.4.2.5.1).

  Wire byte layout (14 bytes total):
    - AD Structure 1 (Flags, 3 bytes):
        [0x02 (Len)][0x01 (AD Type: Flags)][0x06 (LE General + No BR/EDR)]
    - AD Structure 2 (Service Data - 16-bit UUID, 11 bytes):
        [0x0A (Len)][0x16 (AD Type: Service Data 16b)][0xF6, 0xFF (UUID 0xFFF6)]
        [OpCode = 0x00 (1B)]
        [Discriminator (12b) | AdvVersion (4b=0) : 2B LE]
        [VendorId  : 2B LE]
        [ProductId : 2B LE]
        [AdditionalDataFlag : 1B]
  """
  op_code = 0x00
  disc_and_version = discriminator & 0x0FFF
  flags = (0x01 if additional_data else 0x00) | (
      0x02 if extended_announcement else 0x00
  )
  vid = 0 if extended_announcement else (vendor_id & 0xFFFF)
  pid = 0 if extended_announcement else (product_id & 0xFFFF)
  service_data = struct.pack(
      '<BHHHB', op_code, disc_and_version, vid, pid, flags
  )
  ad_flags = bytes([0x02, 0x01, 0x06])
  ad_svc = bytes([len(service_data) + 3, 0x16, 0xF6, 0xFF]) + service_data
  return ad_flags + ad_svc


def parse_matter_ble_service_data(adv_data: bytes) -> Optional[bytes]:
  """Extracts 0xFFF6 service data bytes from raw BLE AD structures."""
  idx = 0
  n = len(adv_data)
  while idx < n:
    length = adv_data[idx]
    if length == 0 or idx + 1 + length > n:
      break
    ad_type = adv_data[idx + 1]
    ad_body = adv_data[idx + 2 : idx + 1 + length]
    if ad_type == 0x16 and len(ad_body) >= 2:
      uuid16 = struct.unpack('<H', ad_body[:2])[0]
      if uuid16 == MATTER_SERVICE_UUID_SHORT:
        return ad_body[2:]
    idx += 1 + length
  return None


class VirtualBluezAdapterBridge:
  """Bridges a VirtualBluetoothController over H4 TCP for Matter BLE BTP."""

  def __init__(
      self,
      controller_id: str,
      bd_addr: str,
      host: str,
      dedicated_hci_port: int,
  ):
    self.controller_id = controller_id
    self.bd_addr = bd_addr
    self.host = host
    self.dedicated_hci_port = dedicated_hci_port
    self.h4_client: Optional[H4TcpClient] = None
    self.discovered_peripherals: Dict[str, Dict[str, object]] = {}
    self.connected_handles: Dict[int, str] = {}
    self.rx_indications: List[bytes] = []
    self.rx_writes: List[bytes] = []
    self.on_write_callback: Optional[Callable[[bytes], None]] = None
    self.on_indication_callback: Optional[Callable[[bytes], None]] = None
    self.on_discovery_callback: Optional[
        Callable[[str, Dict[str, object]], None]
    ] = None
    self._lock = threading.RLock()
    self._running = False
    self._poll_thread: Optional[threading.Thread] = None

  def start(self) -> None:
    self.h4_client = H4TcpClient(self.host, self.dedicated_hci_port)
    self.h4_client.reset()
    self._running = True
    self._poll_thread = threading.Thread(
        target=self._poll_events_loop,
        name=f'bluez-bridge-{self.controller_id}',
        daemon=True,
    )
    self._poll_thread.start()

  def stop(self) -> None:
    self._running = False
    if self.h4_client is not None:
      self.h4_client.close()
      self.h4_client = None

  def start_matter_advertising(
      self,
      discriminator: int,
      vendor_id: int = 0xFFF1,
      product_id: int = 0x8001,
  ) -> None:
    """Configures and enables Matter 0xFFF6 BLE advertising over H4 TCP."""
    if self.h4_client is None:
      return
    adv_payload = build_matter_ble_adv_payload(
        discriminator=discriminator, vendor_id=vendor_id, product_id=product_id
    )
    self.h4_client.start_advertising(adv_payload)

  def stop_matter_advertising(self) -> None:
    if self.h4_client is None:
      return
    self.h4_client.send_command(HciOpcode.LE_SET_ADVERTISING_ENABLE, b'\x00')

  def _find_matching_discriminator(self, discriminator: int) -> Optional[str]:
    """Returns the BD_ADDR of a discovered peripheral matching discriminator."""
    with self._lock:
      for addr, info in self.discovered_peripherals.items():
        if info.get('discriminator') == discriminator:
          return addr
    return None

  def scan_for_matter_discriminator(
      self, discriminator: int, timeout: float = 3.0
  ) -> Optional[str]:
    """Scans over H4 TCP for a Matter device matching discriminator."""
    if self.h4_client is None:
      return None
    self.h4_client.start_scanning(active=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
      self._sync_from_h4_client()
      addr = self._find_matching_discriminator(discriminator)
      if addr is not None:
        self.h4_client.send_command(HciOpcode.LE_SET_SCAN_ENABLE, b'\x00\x00')
        return addr
      time.sleep(0.05)
    self.h4_client.send_command(HciOpcode.LE_SET_SCAN_ENABLE, b'\x00\x00')
    return None

  def connect_to_peripheral(
      self, peer_bd_addr: str, timeout: float = 3.0
  ) -> int:
    """Establishes an LE connection over H4 TCP and returns handle."""
    if self.h4_client is None:
      raise RuntimeError('H4 client not initialized')
    handle = self.h4_client.connect(peer_bd_addr, timeout=timeout)
    with self._lock:
      self.connected_handles[handle] = peer_bd_addr.upper()
    return handle

  def write_c1_request(self, handle: int, payload: bytes) -> None:
    """Sends a Matter BTP C1 ATT Write Request over H4 TCP ACL."""
    if self.h4_client is None:
      return
    att_pdu = (
        struct.pack('<BH', ATT_OP_WRITE_REQ, ATT_HANDLE_C1_VALUE) + payload
    )
    self.h4_client.send_acl_l2cap(handle, ATT_CID, att_pdu)

  def send_c2_indication(self, handle: int, payload: bytes) -> None:
    """Sends a Matter BTP C2 ATT Handle Value Indication over H4 TCP ACL."""
    if self.h4_client is None:
      return
    att_pdu = (
        struct.pack('<BH', ATT_OP_HANDLE_VALUE_IND, ATT_HANDLE_C2_VALUE)
        + payload
    )
    self.h4_client.send_acl_l2cap(handle, ATT_CID, att_pdu)

  def _process_acl_att_packets(
      self, acls: List[Tuple[int, int, bytes]]
  ) -> Tuple[List[bytes], List[bytes]]:
    """Processes incoming ATT C1 write and C2 indication ACL packets."""
    new_writes: List[bytes] = []
    new_inds: List[bytes] = []
    if self.h4_client is None:
      return new_writes, new_inds
    for handle, cid, att_pdu in acls:
      if cid != ATT_CID or len(att_pdu) < 3:
        continue
      opcode = att_pdu[0]
      att_handle = struct.unpack('<H', att_pdu[1:3])[0]
      value = att_pdu[3:]
      if opcode == ATT_OP_WRITE_REQ and att_handle == ATT_HANDLE_C1_VALUE:
        self.rx_writes.append(value)
        new_writes.append(value)
        self.h4_client.send_acl_l2cap(
            handle, ATT_CID, struct.pack('<B', ATT_OP_WRITE_RSP)
        )
      elif (
          opcode == ATT_OP_HANDLE_VALUE_IND
          and att_handle == ATT_HANDLE_C2_VALUE
      ):
        self.rx_indications.append(value)
        new_inds.append(value)
        self.h4_client.send_acl_l2cap(
            handle, ATT_CID, struct.pack('<B', ATT_OP_HANDLE_VALUE_CFM)
        )
    return new_writes, new_inds

  def _dispatch_bridge_callbacks(
      self, new_discoveries, new_writes: List[bytes], new_inds: List[bytes]
  ) -> None:
    """Invokes registered discovery, write, and indication callbacks."""
    if self.on_discovery_callback is not None:
      for addr, info in new_discoveries:
        try:
          self.on_discovery_callback(addr, info)
        except Exception:  # pylint: disable=broad-exception-caught
          pass
    if self.on_write_callback is not None:
      for val in new_writes:
        try:
          self.on_write_callback(val)
        except Exception:  # pylint: disable=broad-exception-caught
          pass
    if self.on_indication_callback is not None:
      for val in new_inds:
        try:
          self.on_indication_callback(val)
        except Exception:  # pylint: disable=broad-exception-caught
          pass

  def _sync_from_h4_client(self) -> None:
    if self.h4_client is None:
      return
    with self.h4_client._cv:  # pylint: disable=protected-access
      advs = list(self.h4_client._adv_reports)  # pylint: disable=protected-access
      conns = dict(self.h4_client._connections)  # pylint: disable=protected-access
      acls = list(self.h4_client._acl_packets)  # pylint: disable=protected-access
      self.h4_client._acl_packets.clear()  # pylint: disable=protected-access

    new_discoveries = []
    with self._lock:
      for rep in advs:
        addr = str(rep['bd_addr'])
        adv_data = bytes(rep['adv_data'])
        svc_data = parse_matter_ble_service_data(adv_data)
        disc = (
            struct.unpack('<H', svc_data[1:3])[0] & 0x0FFF
            if svc_data and len(svc_data) >= 3
            else None
        )
        is_new = addr not in self.discovered_peripherals
        info = {
            'bd_addr': addr,
            'rssi': rep.get('rssi', -45),
            'adv_data': adv_data,
            'service_data': svc_data,
            'discriminator': disc,
        }
        self.discovered_peripherals[addr] = info
        if is_new:
          new_discoveries.append((addr, info))
      for h, cinfo in conns.items():
        self.connected_handles[h] = str(cinfo['peer_bd_addr'])
      new_writes, new_inds = self._process_acl_att_packets(acls)

    self._dispatch_bridge_callbacks(new_discoveries, new_writes, new_inds)

  def _poll_events_loop(self) -> None:
    while self._running:
      try:
        self._sync_from_h4_client()
      except Exception:  # pylint: disable=broad-exception-caught
        pass
      time.sleep(0.02)
