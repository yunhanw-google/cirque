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
"""Bluetooth H4 UART Transport Framing, HCI Command/Event Engine, and L2CAP/ATT/BTP."""

from dataclasses import dataclass, field
from enum import IntEnum
import os
import struct
from typing import Callable, Dict, List, Optional, Tuple


class H4PacketType(IntEnum):
  """Standard Bluetooth H4 UART Transport packet indicators (Vol 4, Part A)."""

  COMMAND = 0x01
  ACL_DATA = 0x02
  SCO_DATA = 0x03
  EVENT = 0x04
  ISO_DATA = 0x05


class HciOpcode(IntEnum):
  """Standard Bluetooth HCI Command Opcodes (OGF << 10 | OCF)."""

  # Link Control Commands (OGF = 0x01)
  DISCONNECT = 0x0406
  READ_REMOTE_VERSION_INFO = 0x041D

  # Controller & Baseband Commands (OGF = 0x03)
  SET_EVENT_MASK = 0x0C01
  RESET = 0x0C03
  WRITE_LOCAL_NAME = 0x0C13
  READ_LOCAL_NAME = 0x0C14
  READ_LE_HOST_SUPPORT = 0x0C6C
  WRITE_LE_HOST_SUPPORT = 0x0C6D

  # Informational Parameters (OGF = 0x04)
  READ_LOCAL_VERSION_INFO = 0x1001
  READ_LOCAL_SUPPORTED_COMMANDS = 0x1002
  READ_LOCAL_SUPPORTED_FEATURES = 0x1003
  READ_BUFFER_SIZE = 0x1005
  READ_BD_ADDR = 0x1009

  # Status Parameters (OGF = 0x05)
  READ_RSSI = 0x1405

  # LE Controller Commands (OGF = 0x08)
  LE_SET_EVENT_MASK = 0x2001
  LE_READ_BUFFER_SIZE = 0x2002
  LE_READ_LOCAL_SUPPORTED_FEATURES = 0x2003
  LE_SET_RANDOM_ADDRESS = 0x2005
  LE_SET_ADVERTISING_PARAMETERS = 0x2006
  LE_READ_ADVERTISING_CHANNEL_TX_POWER = 0x2007
  LE_SET_ADVERTISING_DATA = 0x2008
  LE_SET_SCAN_RESPONSE_DATA = 0x2009
  LE_SET_ADVERTISING_ENABLE = 0x200A
  LE_SET_SCAN_PARAMETERS = 0x200B
  LE_SET_SCAN_ENABLE = 0x200C
  LE_CREATE_CONNECTION = 0x200D
  LE_CREATE_CONNECTION_CANCEL = 0x200E
  LE_READ_FILTER_ACCEPT_LIST_SIZE = 0x200F
  LE_CLEAR_FILTER_ACCEPT_LIST = 0x2010
  LE_CONNECTION_UPDATE = 0x2013
  LE_READ_REMOTE_FEATURES = 0x2016
  LE_ENCRYPT = 0x2017
  LE_RAND = 0x2018
  LE_READ_SUPPORTED_STATES = 0x201C
  LE_SET_DATA_LENGTH = 0x2022
  LE_SET_EXTENDED_ADVERTISING_PARAMETERS = 0x2036
  LE_SET_EXTENDED_ADVERTISING_DATA = 0x2037
  LE_SET_EXTENDED_SCAN_RESPONSE_DATA = 0x2038
  LE_SET_EXTENDED_ADVERTISING_ENABLE = 0x2039
  LE_SET_EXTENDED_SCAN_PARAMETERS = 0x2041
  LE_SET_EXTENDED_SCAN_ENABLE = 0x2042
  LE_EXTENDED_CREATE_CONNECTION = 0x2043


class HciEventCode(IntEnum):
  """Standard Bluetooth HCI Event Codes (Vol 4, Part E, Section 7.7)."""

  DISCONNECTION_COMPLETE = 0x05
  READ_REMOTE_VERSION_INFO_COMPLETE = 0x0C
  COMMAND_COMPLETE = 0x0E
  COMMAND_STATUS = 0x0F
  NUMBER_OF_COMPLETED_PACKETS = 0x13
  LE_META_EVENT = 0x3E


class LeSubEventCode(IntEnum):
  """Standard Bluetooth LE Meta Event Subcodes."""

  CONNECTION_COMPLETE = 0x01
  ADVERTISING_REPORT = 0x02
  CONNECTION_UPDATE_COMPLETE = 0x03
  READ_REMOTE_FEATURES_COMPLETE = 0x04
  DATA_LENGTH_CHANGE = 0x07
  ENHANCED_CONNECTION_COMPLETE = 0x0A
  EXTENDED_ADVERTISING_REPORT = 0x0D


def str_to_bdaddr_bytes(bdaddr_str: str) -> bytes:
  """Converts 'AA:BB:CC:DD:EE:FF' to little-endian 6-byteHCI BD_ADDR."""
  parts = [int(part, 16) for part in bdaddr_str.strip().split(':')]
  if len(parts) != 6:
    raise ValueError(f'Invalid BD_ADDR string: {bdaddr_str}')
  return bytes(reversed(parts))


def bdaddr_bytes_to_str(bdaddr_le: bytes) -> str:
  """Converts little-endian 6-byte HCI BD_ADDR to 'AA:BB:CC:DD:EE:FF'."""
  if len(bdaddr_le) != 6:
    raise ValueError('BD_ADDR bytes must be 6 bytes')
  return ':'.join(f'{b:02X}' for b in reversed(bdaddr_le))


@dataclass
class H4Packet:
  """Represents a framed H4 packet."""

  packet_type: H4PacketType
  payload: bytes

  def to_bytes(self) -> bytes:
    return bytes([int(self.packet_type)]) + self.payload

  @classmethod
  def from_bytes(cls, raw: bytes) -> 'H4Packet':
    if not raw:
      raise ValueError('Empty H4 packet bytes')
    return cls(H4PacketType(raw[0]), raw[1:])


class H4StreamParser:
  """Stateful H4 byte stream deframer tolerant of TCP chunk fragmentation.

  Bluetooth H4 over UART/TCP prepends a 1-byte packet indicator before each
  HCI frame without an outer length envelope. Because TCP may split or coalesce
  arbitrary byte boundaries, `H4StreamParser` inspects the inner protocol header
  of the buffered packet type to compute the exact frame length:

    - COMMAND (0x01): [0x01][Opcode:2B LE][ParamLen:1B][Params:ParamLen B]
                      Total length = 4 + buffer[3]
    - ACL_DATA (0x02): [0x02][Handle+Flags:2B LE][DataLen:2B LE][Data:DataLen B]
                       Total length = 5 + uint16_le(buffer[3:5])
    - SCO_DATA (0x03): [0x03][Handle+Flags:2B LE][DataLen:1B][Data:DataLen B]
                       Total length = 4 + buffer[3]
    - EVENT (0x04):    [0x04][EventCode:1B][ParamLen:1B][Params:ParamLen B]
                       Total length = 3 + buffer[2]
    - ISO_DATA (0x05): [0x05][Handle+Flags:2B LE][DataLen:14b LE][Data]
                       Total length = 5 + (uint16_le(buffer[3:5]) & 0x3FFF)
  """

  def __init__(self):
    self._buffer = bytearray()

  def _next_packet_frame(self) -> Optional[Tuple[H4PacketType, int]]:
    """Returns (packet_type, total_frame_len) or None if header incomplete."""
    pkt_type_val = self._buffer[0]
    buf_len = len(self._buffer)
    if pkt_type_val == H4PacketType.COMMAND:
      return (
          (H4PacketType.COMMAND, 4 + self._buffer[3]) if buf_len >= 4 else None
      )
    if pkt_type_val == H4PacketType.ACL_DATA:
      if buf_len < 5:
        return None
      return (
          H4PacketType.ACL_DATA,
          5 + struct.unpack_from('<H', self._buffer, 3)[0],
      )
    if pkt_type_val == H4PacketType.SCO_DATA:
      return (
          (H4PacketType.SCO_DATA, 4 + self._buffer[3]) if buf_len >= 4 else None
      )
    if pkt_type_val == H4PacketType.EVENT:
      return (H4PacketType.EVENT, 3 + self._buffer[2]) if buf_len >= 3 else None
    if pkt_type_val == H4PacketType.ISO_DATA:
      if buf_len < 5:
        return None
      # ISO_DATA length occupies the lower 14 bits of bytes 3..4 (Core v5.3).
      data_len = struct.unpack_from('<H', self._buffer, 3)[0] & 0x3FFF
      return H4PacketType.ISO_DATA, 5 + data_len
    return None

  def feed(self, data: bytes) -> List[H4Packet]:
    """Feeds raw TCP stream bytes and returns complete H4Packet frames."""
    if data:
      self._buffer.extend(data)
    packets: List[H4Packet] = []
    valid_types = {int(t) for t in H4PacketType}
    while self._buffer:
      # Discard any leading desynchronized bytes until a valid H4 type byte.
      if self._buffer[0] not in valid_types:
        del self._buffer[0]
        continue
      frame_spec = self._next_packet_frame()
      if frame_spec is None:
        break
      pkt_type, total_len = frame_spec
      # Wait for the next TCP recv() if the full payload has not arrived yet.
      if len(self._buffer) < total_len:
        break
      payload = bytes(self._buffer[1:total_len])
      del self._buffer[:total_len]
      packets.append(H4Packet(pkt_type, payload))
    return packets


def build_command_complete_event(
    opcode: int, status: int = 0x00, return_params: bytes = b''
) -> H4Packet:
  """Builds an HCI Command Complete event (0x0E) wrapped in H4.

  Wire layout:
    [0x04][0x0E][ParamLen][Num_HCI_Command_Packets=1][Opcode:2B LE][Status:1B]
    [Return_Parameters...]
  """
  params = struct.pack('<BHB', 1, opcode, status) + return_params
  event_payload = (
      struct.pack('<BB', HciEventCode.COMMAND_COMPLETE, len(params)) + params
  )
  return H4Packet(H4PacketType.EVENT, event_payload)


def build_command_status_event(opcode: int, status: int = 0x00) -> H4Packet:
  """Builds an HCI Command Status event (0x0F) wrapped in H4.

  Used by asynchronous baseband operations (such as `LE_Create_Connection` and
  `Disconnect`) to acknowledge command receipt before the Link Layer handshake
  completes.
  """
  params = struct.pack('<BBH', status, 1, opcode)
  event_payload = (
      struct.pack('<BB', HciEventCode.COMMAND_STATUS, len(params)) + params
  )
  return H4Packet(H4PacketType.EVENT, event_payload)


def build_disconnection_complete_event(
    handle: int, reason: int = 0x13, status: int = 0x00
) -> H4Packet:
  """Builds an HCI Disconnection Complete event wrapped in H4."""
  params = struct.pack('<BHB', status, handle & 0x0FFF, reason)
  event_payload = (
      struct.pack('<BB', HciEventCode.DISCONNECTION_COMPLETE, len(params))
      + params
  )
  return H4Packet(H4PacketType.EVENT, event_payload)


def build_number_of_completed_packets_event(
    handle: int, num_completed: int = 1
) -> H4Packet:
  """Builds an HCI Number Of Completed Packets event wrapped in H4."""
  params = struct.pack('<BHH', 1, handle & 0x0FFF, num_completed)
  event_payload = (
      struct.pack('<BB', HciEventCode.NUMBER_OF_COMPLETED_PACKETS, len(params))
      + params
  )
  return H4Packet(H4PacketType.EVENT, event_payload)


def build_le_advertising_report_event(
    event_type: int,
    addr_type: int,
    bdaddr_str: str,
    adv_data: bytes,
    rssi: int = -45,
) -> H4Packet:
  """Builds an HCI LE Meta Event: LE Advertising Report (0x02)."""
  bdaddr_le = str_to_bdaddr_bytes(bdaddr_str)
  clipped_adv = adv_data[:31]
  rssi_byte = struct.pack('<b', max(-127, min(20, rssi)))
  params = (
      struct.pack(
          '<BBBB6sB',
          LeSubEventCode.ADVERTISING_REPORT,
          1,  # Num_Reports
          event_type,
          addr_type,
          bdaddr_le,
          len(clipped_adv),
      )
      + clipped_adv
      + rssi_byte
  )
  event_payload = (
      struct.pack('<BB', HciEventCode.LE_META_EVENT, len(params)) + params
  )
  return H4Packet(H4PacketType.EVENT, event_payload)


def build_le_connection_complete_event(
    status: int,
    handle: int,
    role: int,
    peer_addr_type: int,
    peer_bdaddr_str: str,
    **conn_params: int,
) -> H4Packet:
  """Builds an HCI LE Meta Event: LE Connection Complete (0x01)."""
  conn_interval = conn_params.get('conn_interval', 0x0018)
  conn_latency = conn_params.get('conn_latency', 0x0000)
  supervision_timeout = conn_params.get('supervision_timeout', 0x01F4)
  peer_le = str_to_bdaddr_bytes(peer_bdaddr_str)
  params = struct.pack(
      '<BBHBB6sHHHB',
      LeSubEventCode.CONNECTION_COMPLETE,
      status,
      handle & 0x0FFF,
      role,  # 0x00 = Central/Master, 0x01 = Peripheral/Slave
      peer_addr_type,
      peer_le,
      conn_interval,
      conn_latency,
      supervision_timeout,
      0x00,  # Central_Clock_Accuracy
  )
  event_payload = (
      struct.pack('<BB', HciEventCode.LE_META_EVENT, len(params)) + params
  )
  return H4Packet(H4PacketType.EVENT, event_payload)


def build_acl_packet(
    handle: int, l2cap_cid: int, l2cap_payload: bytes, pb_flag: int = 0x02
) -> H4Packet:
  """Builds an H4 ACL Data packet encapsulating an L2CAP PDU.

  ACL + L2CAP wire layout:
    [0x02 (ACL_DATA)]
    [Handle (12b) | PB_Flag (2b) | BC_Flag (2b) : 2B LE]
    [ACL_Data_Total_Length : 2B LE]
      [L2CAP_Length : 2B LE]
      [L2CAP_CID    : 2B LE (e.g. 0x0004 for BLE ATT)]
      [L2CAP_Payload: N bytes]
  """
  l2cap_hdr = struct.pack('<HH', len(l2cap_payload), l2cap_cid)
  acl_body = l2cap_hdr + l2cap_payload
  # pb_flag=0x02 marks the start of a non-automatically-flushable L2CAP PDU.
  handle_flags = (handle & 0x0FFF) | ((pb_flag & 0x03) << 12)
  acl_hdr = struct.pack('<HH', handle_flags, len(acl_body))
  return H4Packet(H4PacketType.ACL_DATA, acl_hdr + acl_body)


def parse_acl_packet(payload: bytes) -> Tuple[int, int, bytes]:
  """Parses an H4 ACL payload into (conn_handle, l2cap_cid, l2cap_payload)."""
  if len(payload) < 8:
    raise ValueError('ACL payload too short for L2CAP header')
  handle_flags, acl_len = struct.unpack_from('<HH', payload, 0)
  handle = handle_flags & 0x0FFF
  l2cap_len, l2cap_cid = struct.unpack_from('<HH', payload, 4)
  l2cap_payload = payload[8 : 8 + min(acl_len - 4, l2cap_len)]
  return handle, l2cap_cid, l2cap_payload


@dataclass
class VirtualControllerState:
  """Internal state of an emulated Bluetooth LE Controller."""

  controller_id: str
  bd_addr: str
  random_addr: str = 'C0:00:00:00:00:01'
  local_name: str = 'CirqueVirtualBT'
  event_mask: bytes = b'\xff\xff\xff\xff\xff\xff\xff\xff'
  le_event_mask: bytes = b'\xff\x07\x00\x00\x00\x00\x00\x00'
  le_host_support: int = 1
  adv_enabled: bool = False
  adv_params: bytes = b''
  adv_data: bytes = b''
  scan_rsp_data: bytes = b''
  scan_enabled: bool = False
  scan_type: int = 0x01  # Active scan by default
  own_addr_type: int = 0x00
  connections: Dict[int, Dict[str, object]] = field(default_factory=dict)
  tx_packets: int = 0
  rx_packets: int = 0
  acl_tx_packets: int = 0
  acl_rx_packets: int = 0


def controller_state_to_dict(
    state: VirtualControllerState, dedicated_hci_port: int = 0
) -> Dict[str, object]:
  """Serializes the controller state into a JSON-compatible dictionary."""
  return {
      'controller_id': state.controller_id,
      'bd_addr': state.bd_addr,
      'random_addr': state.random_addr,
      'local_name': state.local_name,
      'adv_enabled': state.adv_enabled,
      'adv_data_hex': state.adv_data.hex(),
      'scan_enabled': state.scan_enabled,
      'dedicated_hci_port': dedicated_hci_port,
      'active_connections': list(state.connections.values()),
      'tx_packets': state.tx_packets,
      'rx_packets': state.rx_packets,
      'acl_tx_packets': state.acl_tx_packets,
      'acl_rx_packets': state.acl_rx_packets,
  }


def build_static_hci_reply(
    opcode: int, state: VirtualControllerState
) -> Optional[bytes]:
  """Returns the static HCI Command Complete payload for read opcodes."""
  raw_name = state.local_name.encode('utf-8')[:248]
  replies = {
      HciOpcode.READ_BD_ADDR: str_to_bdaddr_bytes(state.bd_addr),
      HciOpcode.READ_LOCAL_VERSION_INFO: struct.pack(
          '<BHBHH', 0x0D, 0x0001, 0x0D, 0x00E0, 0x0001
      ),
      HciOpcode.READ_LOCAL_SUPPORTED_COMMANDS: bytes([0xFF] * 64),
      HciOpcode.READ_LOCAL_SUPPORTED_FEATURES: bytes(
          [0, 0, 0, 0, 0x60, 0, 0, 0]
      ),
      HciOpcode.READ_BUFFER_SIZE: struct.pack('<HBHH', 1024, 64, 16, 8),
      HciOpcode.READ_LOCAL_NAME: raw_name + b'\x00' * (248 - len(raw_name)),
      HciOpcode.READ_LE_HOST_SUPPORT: struct.pack(
          '<BB', state.le_host_support, 0
      ),
      HciOpcode.LE_READ_BUFFER_SIZE: struct.pack('<HB', 251, 16),
      HciOpcode.LE_READ_LOCAL_SUPPORTED_FEATURES: bytes(
          [0xFF, 1, 0, 0, 0, 0, 0, 0]
      ),
      HciOpcode.LE_READ_SUPPORTED_STATES: bytes([0xFF] * 5 + [3, 0, 0]),
      HciOpcode.LE_READ_ADVERTISING_CHANNEL_TX_POWER: struct.pack('<b', 0),
      HciOpcode.LE_READ_FILTER_ACCEPT_LIST_SIZE: struct.pack('<B', 16),
      HciOpcode.LE_RAND: os.urandom(8),
  }
  return replies.get(opcode)
