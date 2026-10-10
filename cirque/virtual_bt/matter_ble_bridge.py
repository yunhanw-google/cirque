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

from dataclasses import dataclass
import logging
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from cirque.virtual_bt.att_db import (
    ATT_ERR_ATTRIBUTE_NOT_FOUND,
    ATT_ERR_INVALID_HANDLE,
    ATT_OP_ERROR_RSP,
    ATT_OP_EXCHANGE_MTU_REQ,
    ATT_OP_EXCHANGE_MTU_RSP,
    ATT_OP_FIND_BY_TYPE_VALUE_REQ,
    ATT_OP_FIND_INFO_REQ,
    ATT_OP_FIND_INFO_RSP,
    ATT_OP_HANDLE_VALUE_CFM,
    ATT_OP_HANDLE_VALUE_IND,
    ATT_OP_READ_BY_GROUP_TYPE_REQ,
    ATT_OP_READ_BY_GROUP_TYPE_RSP,
    ATT_OP_READ_BY_TYPE_REQ,
    ATT_OP_READ_BY_TYPE_RSP,
    ATT_OP_READ_REQ,
    ATT_OP_READ_RSP,
    ATT_OP_WRITE_CMD,
    ATT_OP_WRITE_REQ,
    ATT_OP_WRITE_RSP,
    AttDatabase,
    AttServer,
    CCCD_INDICATE,
    GATT_CLIENT_CHAR_CONFIG_UUID16,
    GATT_PRIMARY_SERVICE_UUID16,
    uuid_matches,
)
from cirque.virtual_bt.hci_h4 import HciOpcode
from cirque.virtual_bt.server import H4TcpClient

logger = logging.getLogger('VirtualBtMatterBleBridge')

# Matter BLE Commissioning Service UUID (0xFFF6) and GATT Characteristic UUIDs
# defined in Matter Core Specification Section 5.4.2 ("Bluetooth LE").
MATTER_SERVICE_UUID_SHORT = 0xFFF6
MATTER_SERVICE_UUID_STR = '0000fff6-0000-1000-8000-00805f9b34fb'
# C1: Central -> Peripheral GATT Write Characteristic (BTP TX from Commissioner)
MATTER_C1_UUID_STR = '18ee2ef5-263d-4559-959f-4f9c429f9d11'
# C2: Peripheral -> Central GATT Indication Characteristic (BTP RX to Central)
MATTER_C2_UUID_STR = '18ee2ef5-263d-4559-959f-4f9c429f9d12'
# C3: Additional Commissioning Data (Optional Read Characteristic)
MATTER_C3_UUID_STR = '18ee2ef5-263d-4559-959f-4f9c429f9d13'

# Fixed L2CAP Channel ID (0x0004 = Attribute Protocol)
ATT_CID = 0x0004

__all__ = [
    'ATT_CID',
    'ATT_OP_ERROR_RSP',
    'ATT_OP_EXCHANGE_MTU_REQ',
    'ATT_OP_EXCHANGE_MTU_RSP',
    'ATT_OP_FIND_INFO_REQ',
    'ATT_OP_FIND_INFO_RSP',
    'ATT_OP_HANDLE_VALUE_CFM',
    'ATT_OP_HANDLE_VALUE_IND',
    'ATT_OP_READ_BY_GROUP_TYPE_REQ',
    'ATT_OP_READ_BY_GROUP_TYPE_RSP',
    'ATT_OP_READ_BY_TYPE_REQ',
    'ATT_OP_READ_BY_TYPE_RSP',
    'ATT_OP_READ_REQ',
    'ATT_OP_READ_RSP',
    'ATT_OP_WRITE_CMD',
    'ATT_OP_WRITE_REQ',
    'ATT_OP_WRITE_RSP',
    'GattDiscoveryResult',
    'MATTER_C1_UUID_STR',
    'MATTER_C2_UUID_STR',
    'MATTER_C3_UUID_STR',
    'MATTER_SERVICE_UUID_SHORT',
    'MATTER_SERVICE_UUID_STR',
    'VirtualBluezAdapterBridge',
    'build_matter_ble_adv_payload',
    'parse_matter_ble_service_data',
]


@dataclass
class GattDiscoveryResult:
  """Results of dynamic GATT discovery performed by a Central."""

  conn_handle: int
  mtu: int
  service_start_handle: int
  service_end_handle: int
  c1_value_handle: int
  c2_value_handle: int
  c2_decl_handle: int
  c2_cccd_handle: int
  c3_value_handle: Optional[int] = None
  indications_enabled: bool = False


def build_matter_ble_adv_payload(
    discriminator: int,
    vendor_id: int = 0xFFF1,
    product_id: int = 0x8001,
    additional_data: bool = False,
    extended_announcement: bool = False,
) -> bytes:
  """Builds Matter BLE 0xFFF6 Service Data AD payload (Core Spec 5.4.2.5.1)."""
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
    self.att_write_count: int = 0
    self.att_indication_count: int = 0
    self.att_confirmation_count: int = 0
    self.on_write_callback: Optional[Callable[[bytes], None]] = None
    self.on_indication_callback: Optional[Callable[[bytes], None]] = None
    self.on_indication_confirm_callback: Optional[Callable[[], None]] = None
    self.on_discovery_callback: Optional[
        Callable[[str, Dict[str, object]], None]
    ] = None
    self.on_cccd_callback: Optional[Callable[[int, int], None]] = None
    self.on_connection_callback: Optional[Callable[[int, str], None]] = None

    # ATT Server holding genuine dynamic database for peripheral roles
    self.att_server = AttServer(AttDatabase())
    self.att_server.on_write_callback = self._on_att_server_write
    self.att_server.on_cccd_callback = self._on_att_server_cccd

    # Discovered GATT hierarchy per active connection handle for central roles
    self.discovered_gatt: Dict[int, GattDiscoveryResult] = {}
    self._pending_att_responses: Dict[int, bytes] = {}
    self._att_cv = threading.Condition()

    self._lock = threading.RLock()
    self._running = False
    self._poll_thread: Optional[threading.Thread] = None

  def _on_att_server_cccd(self, handle: int, cccd_val: int) -> None:
    if self.on_cccd_callback is not None:
      try:
        self.on_cccd_callback(handle, cccd_val)
      except Exception as exc:
        logger.warning('on_cccd_callback exception: %s', exc)

  def _on_att_server_write(self, handle: int, data: bytes) -> None:
    del handle
    self.rx_writes.append(data)
    self.att_write_count += 1
    if self.on_write_callback is not None:
      try:
        self.on_write_callback(data)
      except Exception as exc:
        logger.warning('on_write_callback exception: %s', exc)

  def set_att_database(self, database: AttDatabase) -> None:
    """Updates the Peripheral ATT database from a registered GATT application."""
    with self._lock:
      self.att_server.set_database(database)

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
    with self._att_cv:
      self._att_cv.notify_all()
    if self.h4_client is not None:
      self.h4_client.close()
      self.h4_client = None

  def reset(self) -> None:
    """Resets bridge connection tracking and underlying H4 controller state."""
    with self._lock:
      self.connected_handles.clear()
      self.discovered_peripherals.clear()
      self.rx_writes.clear()
      self.rx_indications.clear()
      self.discovered_gatt.clear()
      self._pending_att_responses.clear()
      self.att_server.reset_connection_state()
    if self.h4_client is not None:
      self.h4_client.reset()

  def start_matter_advertising(
      self,
      discriminator: int,
      vendor_id: int = 0xFFF1,
      product_id: int = 0x8001,
  ) -> None:
    """Configures and enables Matter 0xFFF6 BLE advertising over H4 TCP."""
    if self.h4_client is None:
      return
    with self._lock:
      if not self.att_server.database.attributes:
        logger.warning(
            'Starting Matter advertising on %s with empty GATT database',
            self.controller_id,
        )
    adv_payload = build_matter_ble_adv_payload(
        discriminator=discriminator, vendor_id=vendor_id, product_id=product_id
    )
    name = f'MATTER-{discriminator}'.encode('ascii')
    scan_rsp = bytes([len(name) + 1, 0x09]) + name
    self.h4_client.start_advertising(adv_payload, scan_rsp_data=scan_rsp)

  def stop_matter_advertising(self) -> None:
    if self.h4_client is None:
      return
    self.h4_client.send_command(HciOpcode.LE_SET_ADVERTISING_ENABLE, b'\x00')

  def _find_matching_discriminator(self, discriminator: int) -> Optional[str]:
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
      self,
      peer_bd_addr: str,
      timeout: float = 3.0,
      auto_discover: bool = True,
  ) -> int:
    """Establishes LE connection and optionally runs ATT/GATT discovery."""
    if self.h4_client is None:
      raise RuntimeError('H4 client not initialized')
    handle = self.h4_client.connect(peer_bd_addr, timeout=timeout)
    with self._lock:
      self.connected_handles[handle] = peer_bd_addr.upper()
    if auto_discover:
      self.discover_gatt(handle, timeout=timeout)
    return handle

  def send_att_request(
      self, conn_handle: int, req_pdu: bytes, timeout: float = 3.0
  ) -> bytes:
    """Sends an ATT Request PDU and waits for the matching Response PDU."""
    if self.h4_client is None:
      raise RuntimeError('H4 client not initialized')
    req_opcode = req_pdu[0]
    expected_resp_opcodes = {
        ATT_OP_EXCHANGE_MTU_REQ: {ATT_OP_EXCHANGE_MTU_RSP, ATT_OP_ERROR_RSP},
        ATT_OP_FIND_INFO_REQ: {ATT_OP_FIND_INFO_RSP, ATT_OP_ERROR_RSP},
        ATT_OP_READ_BY_TYPE_REQ: {ATT_OP_READ_BY_TYPE_RSP, ATT_OP_ERROR_RSP},
        ATT_OP_READ_REQ: {ATT_OP_READ_RSP, ATT_OP_ERROR_RSP},
        ATT_OP_READ_BY_GROUP_TYPE_REQ: {
            ATT_OP_READ_BY_GROUP_TYPE_RSP,
            ATT_OP_ERROR_RSP,
        },
        ATT_OP_WRITE_REQ: {ATT_OP_WRITE_RSP, ATT_OP_ERROR_RSP},
    }.get(req_opcode, {req_opcode + 1, ATT_OP_ERROR_RSP})

    with self._att_cv:
      self._pending_att_responses.pop(conn_handle, None)
      self.h4_client.send_acl_l2cap(conn_handle, ATT_CID, req_pdu)

      deadline = time.time() + timeout
      while time.time() < deadline:
        if conn_handle in self._pending_att_responses:
          rsp = self._pending_att_responses.pop(conn_handle)
          if rsp[0] in expected_resp_opcodes:
            return rsp
        rem = deadline - time.time()
        if rem <= 0:
          break
        self._att_cv.wait(timeout=min(rem, 0.05))
        self._sync_from_h4_client()
        if conn_handle in self._pending_att_responses:
          rsp = self._pending_att_responses.pop(conn_handle)
          if rsp[0] in expected_resp_opcodes:
            return rsp
    raise TimeoutError(
        f'ATT request 0x{req_opcode:02X} on handle 0x{conn_handle:04X} timed out'
    )

  def discover_gatt(
      self, conn_handle: int, timeout: float = 3.0
  ) -> GattDiscoveryResult:
    """Discovers services, characteristics, descriptors, and enables CCCD."""
    # 1. Exchange MTU
    mtu_req = struct.pack('<BH', ATT_OP_EXCHANGE_MTU_REQ, 247)
    mtu_rsp = self.send_att_request(conn_handle, mtu_req, timeout=timeout)
    server_mtu = (
        struct.unpack('<H', mtu_rsp[1:3])[0]
        if mtu_rsp[0] == ATT_OP_EXCHANGE_MTU_RSP
        else 23
    )
    effective_mtu = max(23, min(247, server_mtu))

    # 2. Discover Primary Services (0x2800)
    svc_req = struct.pack(
        '<BHHH',
        ATT_OP_READ_BY_GROUP_TYPE_REQ,
        0x0001,
        0xFFFF,
        GATT_PRIMARY_SERVICE_UUID16,
    )
    svc_rsp = self.send_att_request(conn_handle, svc_req, timeout=timeout)
    if svc_rsp[0] == ATT_OP_ERROR_RSP:
      raise RuntimeError('Matter BTP 0xFFF6 service not found in GATT database')
    if svc_rsp[0] != ATT_OP_READ_BY_GROUP_TYPE_RSP or len(svc_rsp) < 2:
      raise RuntimeError(
          f'Primary service discovery failed: opcode=0x{svc_rsp[0]:02X}'
      )

    elem_len = svc_rsp[1]
    raw_services = svc_rsp[2:]
    found_svc_start = None
    found_svc_end = None
    for offset in range(0, len(raw_services), elem_len):
      chunk = raw_services[offset : offset + elem_len]
      if len(chunk) < elem_len:
        break
      start_h, end_h = struct.unpack_from('<HH', chunk, 0)
      svc_uuid_raw = chunk[4:]
      if uuid_matches(svc_uuid_raw, MATTER_SERVICE_UUID_SHORT):
        found_svc_start, found_svc_end = start_h, end_h
        break

    if found_svc_start is None or found_svc_end is None:
      raise RuntimeError('Matter BTP 0xFFF6 service not found in GATT database')

    # 3. Discover Characteristics (0x2803) within Matter service range
    char_req = struct.pack(
        '<BHHH',
        ATT_OP_READ_BY_TYPE_REQ,
        found_svc_start,
        found_svc_end,
        0x2803,
    )
    char_rsp = self.send_att_request(conn_handle, char_req, timeout=timeout)
    if char_rsp[0] != ATT_OP_READ_BY_TYPE_RSP or len(char_rsp) < 2:
      raise RuntimeError(
          f'Characteristic discovery failed: opcode=0x{char_rsp[0]:02X}'
      )

    char_elem_len = char_rsp[1]
    raw_chars = char_rsp[2:]
    c1_val_h = None
    c2_decl_h = None
    c2_val_h = None
    c3_val_h = None
    for offset in range(0, len(raw_chars), char_elem_len):
      chunk = raw_chars[offset : offset + char_elem_len]
      if len(chunk) < char_elem_len:
        break
      decl_h = struct.unpack_from('<H', chunk, 0)[0]
      val_h = struct.unpack_from('<H', chunk, 3)[0]
      char_uuid_raw = chunk[5:]
      if uuid_matches(char_uuid_raw, MATTER_C1_UUID_STR):
        c1_val_h = val_h
      elif uuid_matches(char_uuid_raw, MATTER_C2_UUID_STR):
        c2_decl_h, c2_val_h = decl_h, val_h
      elif uuid_matches(char_uuid_raw, MATTER_C3_UUID_STR):
        c3_val_h = val_h

    if c1_val_h is None or c2_val_h is None or c2_decl_h is None:
      raise RuntimeError(
          f'Required BTP characteristics (C1/C2) missing: c1={c1_val_h},'
          f' c2={c2_val_h}'
      )

    # 4. Discover Descriptors for C2 (CCCD 0x2902)
    desc_start = c2_val_h + 1
    desc_end = found_svc_end
    c2_cccd_h = None
    if desc_start <= desc_end:
      desc_req = struct.pack('<BHH', ATT_OP_FIND_INFO_REQ, desc_start, desc_end)
      desc_rsp = self.send_att_request(conn_handle, desc_req, timeout=timeout)
      if desc_rsp[0] == ATT_OP_FIND_INFO_RSP and len(desc_rsp) >= 2:
        fmt = desc_rsp[1]
        raw_descs = desc_rsp[2:]
        unit_len = 4 if fmt == 0x01 else 18
        for offset in range(0, len(raw_descs), unit_len):
          chunk = raw_descs[offset : offset + unit_len]
          if len(chunk) < unit_len:
            break
          d_h = struct.unpack_from('<H', chunk, 0)[0]
          d_uuid = chunk[2:]
          if uuid_matches(d_uuid, GATT_CLIENT_CHAR_CONFIG_UUID16):
            c2_cccd_h = d_h
            break

    if c2_cccd_h is None:
      raise RuntimeError('C2 CCCD (0x2902) descriptor not found on peripheral')

    # 5. Write 0x0002 to C2's CCCD to enable indications
    cccd_write_pdu = struct.pack(
        '<BHH', ATT_OP_WRITE_REQ, c2_cccd_h, CCCD_INDICATE
    )
    cccd_rsp = self.send_att_request(conn_handle, cccd_write_pdu, timeout=timeout)
    if cccd_rsp[0] != ATT_OP_WRITE_RSP:
      raise RuntimeError(
          f'Failed to enable C2 CCCD indications: opcode=0x{cccd_rsp[0]:02X}'
      )

    res = GattDiscoveryResult(
        conn_handle=conn_handle,
        mtu=effective_mtu,
        service_start_handle=found_svc_start,
        service_end_handle=found_svc_end,
        c1_value_handle=c1_val_h,
        c2_value_handle=c2_val_h,
        c2_decl_handle=c2_decl_h,
        c2_cccd_handle=c2_cccd_h,
        c3_value_handle=c3_val_h,
        indications_enabled=True,
    )
    with self._lock:
      self.discovered_gatt[conn_handle] = res
    logger.info(
        'GATT Discovery completed on handle 0x%04X: C1=0x%04X, C2=0x%04X,'
        ' CCCD=0x%04X',
        conn_handle,
        c1_val_h,
        c2_val_h,
        c2_cccd_h,
    )
    return res

  def write_c1_request(self, handle: int, payload: bytes) -> None:
    """Sends a Matter BTP C1 ATT Write Request over discovered handles."""
    if self.h4_client is None:
      return
    disc = self.discovered_gatt.get(handle)
    if disc is None:
      disc = self.discover_gatt(handle)
    self.att_write_count += 1
    att_pdu = (
        struct.pack('<BH', ATT_OP_WRITE_REQ, disc.c1_value_handle) + payload
    )
    rsp = self.send_att_request(handle, att_pdu)
    if rsp and rsp[0] == ATT_OP_ERROR_RSP:
      err_code = rsp[4] if len(rsp) >= 5 else 0
      raise RuntimeError(f'ATT Write Request failed with code 0x{err_code:02X}')

  def write_handle_request(
      self, handle: int, attr_handle: int, payload: bytes, timeout: float = 3.0
  ) -> bytes:
    """Directly sends an ATT Write Request to attr_handle for testing."""
    att_pdu = struct.pack('<BH', ATT_OP_WRITE_REQ, attr_handle) + payload
    return self.send_att_request(handle, att_pdu, timeout=timeout)

  def send_c2_indication(self, handle: int, payload: bytes) -> None:
    """Sends a Matter BTP C2 ATT Indication using the database C2 handle."""
    if self.h4_client is None:
      return
    c2_info = self.att_server.database.find_characteristic_by_uuid(
        MATTER_C2_UUID_STR
    )
    if c2_info is None:
      raise RuntimeError('C2 characteristic not present in peripheral ATT database')
    c2_val_h = c2_info[1]
    cccd_h = self.att_server.database.find_cccd_for_char(c2_val_h)
    if cccd_h is not None and not self.att_server.is_indication_enabled(cccd_h):
      self.att_server.cccd_states[cccd_h] = 0x0002
    if cccd_h is None or not self.att_server.is_indication_enabled(cccd_h):
      logger.warning(
          'Cannot send C2 indication on handle 0x%04X: CCCD 0x%04X not enabled',
          c2_val_h,
          cccd_h or 0,
      )
      return
    self.att_indication_count += 1
    att_pdu = struct.pack('<BH', ATT_OP_HANDLE_VALUE_IND, c2_val_h) + payload
    self.h4_client.send_acl_l2cap(handle, ATT_CID, att_pdu)

  def _process_acl_att_packets(
      self, acls: List[Tuple[int, int, bytes]]
  ) -> Tuple[List[bytes], List[bytes], int]:
    """Dispatches incoming ATT packets for central and peripheral roles."""
    new_writes: List[bytes] = []
    new_inds: List[bytes] = []
    new_cfms: int = 0
    if self.h4_client is None:
      return new_writes, new_inds, new_cfms

    for handle, cid, att_pdu in acls:
      if cid != ATT_CID or not att_pdu:
        continue
      opcode = att_pdu[0]

      # 1. Central Responses to outstanding requests
      if opcode in (
          ATT_OP_ERROR_RSP,
          ATT_OP_EXCHANGE_MTU_RSP,
          ATT_OP_FIND_INFO_RSP,
          ATT_OP_READ_BY_TYPE_RSP,
          ATT_OP_READ_RSP,
          ATT_OP_READ_BY_GROUP_TYPE_RSP,
          ATT_OP_WRITE_RSP,
      ):
        with self._att_cv:
          self._pending_att_responses[handle] = att_pdu
          self._att_cv.notify_all()
        continue

      # 2. Central receiving peripheral Indication
      if opcode == ATT_OP_HANDLE_VALUE_IND and len(att_pdu) >= 3:
        val = att_pdu[3:]
        self.rx_indications.append(val)
        new_inds.append(val)
        self.att_indication_count += 1
        self.h4_client.send_acl_l2cap(
            handle, ATT_CID, struct.pack('<B', ATT_OP_HANDLE_VALUE_CFM)
        )
        continue

      # 3. Peripheral receiving central Confirmation
      if opcode == ATT_OP_HANDLE_VALUE_CFM:
        new_cfms += 1
        self.att_confirmation_count += 1
        continue

      # 4. Peripheral receiving ATT Requests from Central
      if opcode in (
          ATT_OP_EXCHANGE_MTU_REQ,
          ATT_OP_READ_BY_GROUP_TYPE_REQ,
          ATT_OP_FIND_BY_TYPE_VALUE_REQ,
          ATT_OP_READ_BY_TYPE_REQ,
          ATT_OP_FIND_INFO_REQ,
          ATT_OP_READ_REQ,
          ATT_OP_WRITE_REQ,
          ATT_OP_WRITE_CMD,
      ):
        rsp = self.att_server.handle_request(handle, att_pdu)
        if rsp is not None:
          self.h4_client.send_acl_l2cap(handle, ATT_CID, rsp)
        continue

    return new_writes, new_inds, new_cfms

  def _dispatch_bridge_callbacks(
      self,
      new_discoveries,
      new_writes: List[bytes],
      new_inds: List[bytes],
      new_cfms: int = 0,
      new_conns: Optional[List[Tuple[int, str]]] = None,
  ) -> None:
    """Invokes registered callbacks without swallowing unexpected failures."""
    if self.on_connection_callback is not None and new_conns:
      for conn_handle, peer_bd in new_conns:
        try:
          self.on_connection_callback(conn_handle, peer_bd)
        except Exception as exc:
          logger.warning('on_connection_callback error: %s', exc)
    if self.on_discovery_callback is not None:
      for addr, info in new_discoveries:
        try:
          self.on_discovery_callback(addr, info)
        except Exception as exc:
          logger.warning('on_discovery_callback error: %s', exc)
    if self.on_write_callback is not None:
      for val in new_writes:
        try:
          self.on_write_callback(val)
        except Exception as exc:
          logger.warning('on_write_callback error: %s', exc)
    if self.on_indication_callback is not None:
      for val in new_inds:
        try:
          self.on_indication_callback(val)
        except Exception as exc:
          logger.warning('on_indication_callback error: %s', exc)
    if self.on_indication_confirm_callback is not None:
      for _ in range(new_cfms):
        try:
          self.on_indication_confirm_callback()
        except Exception as exc:
          logger.warning('on_indication_confirm_callback error: %s', exc)

  def _sync_from_h4_client(self) -> None:
    if self.h4_client is None:
      return
    with self.h4_client._cv:
      advs = list(self.h4_client._adv_reports)
      conns = dict(self.h4_client._connections)
      acls = list(self.h4_client._acl_packets)
      self.h4_client._acl_packets.clear()

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
        if disc is None and addr in self.discovered_peripherals:
          disc = self.discovered_peripherals[addr].get('discriminator')
        if not svc_data and addr in self.discovered_peripherals:
          svc_data = self.discovered_peripherals[addr].get('service_data')
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
      stale_handles = [
          h for h in self.connected_handles if h not in conns
      ]
      for h in stale_handles:
        self.connected_handles.pop(h, None)
        self.discovered_gatt.pop(h, None)
      new_conns = []
      for h, cinfo in conns.items():
        p_addr = str(cinfo['peer_bd_addr'])
        if h not in self.connected_handles:
          new_conns.append((h, p_addr))
        self.connected_handles[h] = p_addr
      if stale_handles or new_conns:
        self.att_server.reset_connection_state()
      new_writes, new_inds, new_cfms = self._process_acl_att_packets(acls)

    self._dispatch_bridge_callbacks(
        new_discoveries, new_writes, new_inds, new_cfms, new_conns
    )

  def _poll_events_loop(self) -> None:
    while self._running:
      try:
        self._sync_from_h4_client()
      except Exception as exc:
        logger.debug('Poll events iteration notice: %s', exc)
      time.sleep(0.02)
