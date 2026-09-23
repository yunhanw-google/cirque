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
"""Virtual Bluetooth LE Controller state machine over H4 and Link Layer PHY."""

import struct
import threading
import time
from typing import Callable, Dict, List, Optional

from cirque.virtual_bt.hci_h4 import (
    H4Packet,
    H4PacketType,
    HciOpcode,
    VirtualControllerState,
    bdaddr_bytes_to_str,
    build_command_complete_event,
    build_command_status_event,
    build_disconnection_complete_event,
    build_le_advertising_report_event,
    build_le_connection_complete_event,
    build_number_of_completed_packets_event,
    build_static_hci_reply,
    controller_state_to_dict,
)
from cirque.virtual_bt.link_layer import (
    LinkLayerFrame,
    LinkLayerHub,
    LinkLayerPduType,
)


class VirtualBluetoothController:
  """Emulates a Bluetooth LE Controller over H4 and Link Layer PHY."""

  def __init__(
      self,
      controller_id: str,
      bd_addr: str,
      phy_hub: LinkLayerHub,
      local_name: str = 'CirqueVirtualBT',
  ):
    self.state = VirtualControllerState(
        controller_id=controller_id,
        bd_addr=bd_addr.upper(),
        local_name=local_name,
    )
    self.phy_hub = phy_hub
    self._lock = threading.RLock()
    self._h4_sinks: List[Callable[[bytes], None]] = []
    self._next_handle: int = 0x0040
    self._peer_to_handle: Dict[str, int] = {}
    self._adv_thread: Optional[threading.Thread] = None
    self._adv_stop_event = threading.Event()
    self.dedicated_hci_port: int = 0
    self.phy_hub.register_local_endpoint(self.state.bd_addr, self.on_phy_frame)

  def close(self) -> None:
    self._adv_stop_event.set()
    self.phy_hub.unregister_local_endpoint(self.state.bd_addr)

  def add_h4_sink(self, sink: Callable[[bytes], None]) -> None:
    with self._lock:
      self._h4_sinks.append(sink)

  def remove_h4_sink(self, sink: Callable[[bytes], None]) -> None:
    with self._lock:
      if sink in self._h4_sinks:
        self._h4_sinks.remove(sink)

  def emit_h4(self, packet: H4Packet) -> None:
    """Serializes `packet` and dispatches it to all registered H4 TCP sinks."""
    raw = packet.to_bytes()
    # Snapshot `_h4_sinks` under lock and invoke callbacks outside lock so a
    # disconnecting TCP client can safely call `remove_h4_sink()` without
    # deadlocking against an incoming PHY frame.
    with self._lock:
      self.state.tx_packets += 1
      sinks = list(self._h4_sinks)
    for sink in sinks:
      try:
        sink(raw)
      except Exception:  # pylint: disable=broad-exception-caught
        pass

  def broadcast_advertising_once(self) -> None:
    """Broadcasts an ADV_IND frame onto the Link Layer PHY bus."""
    with self._lock:
      if not self.state.adv_enabled:
        return
      adv_payload, addr_type = self.state.adv_data, self.state.own_addr_type
      src_addr = (
          self.state.random_addr if addr_type == 0x01 else self.state.bd_addr
      )
    self.phy_hub.transmit(
        LinkLayerFrame(
            pdu_type=LinkLayerPduType.ADV_IND.value,
            src_bd_addr=src_addr,
            dst_bd_addr='FF:FF:FF:FF:FF:FF',
            src_addr_type=addr_type,
            payload_hex=adv_payload.hex(),
        )
    )

  def _adv_beacon_loop(self) -> None:
    """Periodically rebroadcasts `ADV_IND` every 80ms while advertising is on.

    This ensures a Central controller that enables scanning *after* the
    Peripheral called `LE_Set_Advertising_Enable(1)` still discovers the
    peripheral without waiting for an explicit `SCAN_REQ`.
    """
    while not self._adv_stop_event.is_set():
      with self._lock:
        enabled = self.state.adv_enabled
      if not enabled:
        break
      self.broadcast_advertising_once()
      self._adv_stop_event.wait(0.08)

  def _start_adv_beacon_loop(self) -> None:
    if self._adv_thread and self._adv_thread.is_alive():
      return
    self._adv_stop_event.clear()
    self._adv_thread = threading.Thread(
        target=self._adv_beacon_loop, daemon=True
    )
    self._adv_thread.start()

  def process_h4_packet(self, packet: H4Packet) -> None:
    """Processes an incoming H4 packet from the Host over the HCI TCP channel."""
    with self._lock:
      self.state.rx_packets += 1
    if packet.packet_type == H4PacketType.COMMAND:
      self._handle_hci_command(packet.payload)
    elif packet.packet_type == H4PacketType.ACL_DATA:
      self._handle_hci_acl_data(packet.payload)

  def _handle_info_or_baseband_command(
      self, opcode: int, params: bytes
  ) -> bool:
    """Handles informational, baseband, and LE capability read HCI opcodes."""
    if opcode == HciOpcode.RESET:
      with self._lock:
        self.state.adv_enabled = False
        self.state.scan_enabled = False
        self.state.connections.clear()
        self._peer_to_handle.clear()
      self.emit_h4(build_command_complete_event(opcode, 0x00))
      return True
    reply = build_static_hci_reply(opcode, self.state)
    if reply is not None:
      self.emit_h4(build_command_complete_event(opcode, 0x00, reply))
      return True
    if opcode == HciOpcode.SET_EVENT_MASK and len(params) >= 8:
      self.state.event_mask = params[:8]
    elif opcode == HciOpcode.WRITE_LOCAL_NAME:
      self.state.local_name = params.split(b'\x00', 1)[0].decode(
          'utf-8', errors='ignore'
      )
    elif opcode == HciOpcode.WRITE_LE_HOST_SUPPORT and params:
      self.state.le_host_support = params[0]
    elif opcode == HciOpcode.LE_SET_EVENT_MASK and len(params) >= 8:
      self.state.le_event_mask = params[:8]
    elif opcode not in (
        HciOpcode.SET_EVENT_MASK,
        HciOpcode.WRITE_LE_HOST_SUPPORT,
        HciOpcode.LE_SET_EVENT_MASK,
    ):
      return False
    self.emit_h4(build_command_complete_event(opcode, 0x00))
    return True

  def _handle_le_adv_or_scan_command(self, opcode: int, params: bytes) -> bool:
    """Handles LE advertising and scanning configuration HCI opcodes."""
    if opcode == HciOpcode.LE_SET_RANDOM_ADDRESS and len(params) >= 6:
      self.state.random_addr = bdaddr_bytes_to_str(params[:6])
    elif opcode == HciOpcode.LE_SET_ADVERTISING_PARAMETERS:
      self.state.adv_params = params
      if len(params) >= 6:
        # Byte 5 of LE_Set_Advertising_Parameters is Own_Address_Type.
        self.state.own_addr_type = params[5]
    elif opcode in (
        HciOpcode.LE_SET_ADVERTISING_DATA,
        HciOpcode.LE_SET_EXTENDED_ADVERTISING_DATA,
    ):
      # Legacy LE_Set_Advertising_Data (0x2008) has a 1-byte length header
      # (`[Adv_Data_Len][Adv_Data:31B]`), whereas Extended Advertising Data
      # (0x2037) has a 4-byte header (`[Adv_Handle][Op][Frag_Pref][Len]`).
      off = 1 if opcode == HciOpcode.LE_SET_ADVERTISING_DATA else 4
      if len(params) >= off:
        self.state.adv_data = params[
            off : off + min(params[off - 1], len(params) - off)
        ]
    elif opcode in (
        HciOpcode.LE_SET_SCAN_RESPONSE_DATA,
        HciOpcode.LE_SET_EXTENDED_SCAN_RESPONSE_DATA,
    ):
      off = 1 if opcode == HciOpcode.LE_SET_SCAN_RESPONSE_DATA else 4
      if len(params) >= off:
        self.state.scan_rsp_data = params[
            off : off + min(params[off - 1], len(params) - off)
        ]
    elif opcode in (
        HciOpcode.LE_SET_SCAN_PARAMETERS,
        HciOpcode.LE_SET_EXTENDED_SCAN_PARAMETERS,
    ):
      if params:
        self.state.scan_type = params[0]
    elif opcode in (
        HciOpcode.LE_SET_ADVERTISING_ENABLE,
        HciOpcode.LE_SET_EXTENDED_ADVERTISING_ENABLE,
        HciOpcode.LE_SET_SCAN_ENABLE,
        HciOpcode.LE_SET_EXTENDED_SCAN_ENABLE,
    ):
      return self._handle_le_enable_toggle(opcode, params)
    elif opcode != HciOpcode.LE_SET_RANDOM_ADDRESS:
      return False
    self.emit_h4(build_command_complete_event(opcode, 0x00))
    return True

  def _handle_le_enable_toggle(self, opcode: int, params: bytes) -> bool:
    """Handles LE advertising or scan enable/disable HCI commands."""
    enable = bool(params[0]) if params else False
    is_adv = opcode in (
        HciOpcode.LE_SET_ADVERTISING_ENABLE,
        HciOpcode.LE_SET_EXTENDED_ADVERTISING_ENABLE,
    )
    with self._lock:
      if is_adv:
        self.state.adv_enabled = enable
      else:
        self.state.scan_enabled = enable
    self.emit_h4(build_command_complete_event(opcode, 0x00))
    if enable and is_adv:
      self.broadcast_advertising_once()
      self._start_adv_beacon_loop()
    elif enable:
      # When scanning starts, broadcast an immediate SCAN_REQ onto the virtual
      # PHY so any already-advertising peripherals respond immediately with
      # ADV_IND + optional SCAN_RSP.
      self.phy_hub.transmit(
          LinkLayerFrame(
              pdu_type=LinkLayerPduType.SCAN_REQ.value,
              src_bd_addr=self.state.bd_addr,
              dst_bd_addr='FF:FF:FF:FF:FF:FF',
              src_addr_type=self.state.own_addr_type,
          )
      )
    return True

  def _handle_le_create_conn(self, opcode: int, params: bytes) -> None:
    """Parses LE_Create_Connection parameters and transmits CONNECT_IND.

    Per Bluetooth Core Spec Vol 4, Part E, Section 7.8.12:
      1. `LE_Create_Connection` (0x200D) parameter layout:
         [Scan_Interval:2B][Scan_Window:2B][Initiator_Filter_Policy:1B]
         [Peer_Address_Type:1B][Peer_Address:6B LE][Own_Address_Type:1B]
         [Conn_Interval_Min:2B][Conn_Interval_Max:2B][Max_Latency:2B]
         [Supervision_Timeout:2B][Min_CE_Len:2B][Max_CE_Len:2B]
      2. Controller immediately replies with `HCI_Command_Status (0x0F)`
         (`status=0x00`) and transmits `CONNECT_IND` on the Link Layer PHY.
      3. Once the peripheral replies with `CONNECT_RSP`, both sides emit
         `LE_Meta_Event: LE_Connection_Complete (0x3E/0x01)`.
    """
    if opcode == HciOpcode.LE_CREATE_CONNECTION and len(params) >= 12:
      peer_type, peer_addr = params[5], bdaddr_bytes_to_str(params[6:12])
      interval = (
          struct.unpack_from('<H', params, 13)[0] if len(params) >= 15 else 24
      )
      latency = (
          struct.unpack_from('<H', params, 17)[0] if len(params) >= 19 else 0
      )
      timeout = (
          struct.unpack_from('<H', params, 19)[0] if len(params) >= 21 else 500
      )
    elif len(params) >= 9:
      # LE_Extended_Create_Connection (0x2043) places Peer_Address_Type at
      # byte 2 and Peer_Address at bytes 3..9.
      peer_type, peer_addr = params[2], bdaddr_bytes_to_str(params[3:9])
      interval, latency, timeout = 0x0018, 0x0000, 0x01F4
    else:
      self.emit_h4(build_command_status_event(opcode, 0x12))
      return
    self.emit_h4(build_command_status_event(opcode, 0x00))
    self.phy_hub.transmit(
        LinkLayerFrame(
            pdu_type=LinkLayerPduType.CONNECT_IND.value,
            src_bd_addr=self.state.bd_addr,
            dst_bd_addr=peer_addr,
            src_addr_type=self.state.own_addr_type,
            dst_addr_type=peer_type,
            conn_interval=interval,
            conn_latency=latency,
            supervision_timeout=timeout,
        )
    )

  def _handle_le_conn_command(self, opcode: int, params: bytes) -> bool:
    """Handles LE connection, disconnection, and RSSI HCI opcodes."""
    if opcode in (
        HciOpcode.LE_CREATE_CONNECTION,
        HciOpcode.LE_EXTENDED_CREATE_CONNECTION,
    ):
      self._handle_le_create_conn(opcode, params)
      return True
    if opcode == HciOpcode.LE_CREATE_CONNECTION_CANCEL:
      self.emit_h4(build_command_complete_event(opcode, 0x00))
      return True
    if opcode == HciOpcode.DISCONNECT:
      handle, reason = (
          struct.unpack_from('<HB', params, 0)
          if len(params) >= 3
          else (0x0040, 0x13)
      )
      self.emit_h4(build_command_status_event(opcode, 0x00))
      with self._lock:
        conn = self.state.connections.pop(handle, None)
        peer_addr = str(conn.get('peer_bd_addr', '')) if conn else ''
        self._peer_to_handle.pop(peer_addr, None)
      if peer_addr:
        self.phy_hub.transmit(
            LinkLayerFrame(
                pdu_type=LinkLayerPduType.LL_TERMINATE_IND.value,
                src_bd_addr=self.state.bd_addr,
                dst_bd_addr=peer_addr,
                reason=reason,
            )
        )
      self.emit_h4(build_disconnection_complete_event(handle, reason, 0x00))
      return True
    if opcode == HciOpcode.READ_RSSI:
      handle = (
          struct.unpack_from('<H', params, 0)[0] if len(params) >= 2 else 0x0040
      )
      with self._lock:
        conn = self.state.connections.get(handle)
        peer_addr = str(conn.get('peer_bd_addr', '')) if conn else ''
      rssi = (
          self.phy_hub.get_rssi(peer_addr, self.state.bd_addr)
          if peer_addr
          else -45
      )
      self.emit_h4(
          build_command_complete_event(
              opcode, 0x00, struct.pack('<Hb', handle, rssi)
          )
      )
      return True
    return False

  def _handle_hci_command(self, payload: bytes) -> None:
    if len(payload) < 3:
      return
    opcode, param_len = struct.unpack_from('<HB', payload, 0)
    params = payload[3 : 3 + param_len]
    for handler in (
        self._handle_info_or_baseband_command,
        self._handle_le_adv_or_scan_command,
        self._handle_le_conn_command,
    ):
      if handler(opcode, params):
        return
    self.emit_h4(build_command_complete_event(opcode, 0x00))

  def _handle_hci_acl_data(self, payload: bytes) -> None:
    if len(payload) < 4:
      return
    handle = struct.unpack_from('<HH', payload, 0)[0] & 0x0FFF
    with self._lock:
      self.state.acl_tx_packets += 1
      conn = self.state.connections.get(handle)
      peer_addr = str(conn['peer_bd_addr']) if conn else None
    self.emit_h4(build_number_of_completed_packets_event(handle, 1))
    if peer_addr:
      self.phy_hub.transmit(
          LinkLayerFrame(
              pdu_type=LinkLayerPduType.LL_DATA.value,
              src_bd_addr=self.state.bd_addr,
              dst_bd_addr=peer_addr,
              payload_hex=payload[4:].hex(),
          )
      )

  def _allocate_connection(
      self, peer_bd_addr: str, peer_addr_type: int, role: int
  ) -> int:
    with self._lock:
      peer_upper = peer_bd_addr.upper()
      if peer_upper in self._peer_to_handle:
        return self._peer_to_handle[peer_upper]
      handle = self._next_handle
      self._next_handle += 1
      self.state.connections[handle] = {
          'handle': handle,
          'peer_bd_addr': peer_upper,
          'peer_addr_type': peer_addr_type,
          'role': role,
          'connected_at': time.time(),
      }
      self._peer_to_handle[peer_upper] = handle
      return handle

  def _on_phy_adv_or_scan(self, frame: LinkLayerFrame) -> bool:
    """Processes ADV_IND, SCAN_REQ, and SCAN_RSP frames from the PHY."""
    pdu = frame.pdu_type
    if pdu in (LinkLayerPduType.ADV_IND.value, LinkLayerPduType.SCAN_RSP.value):
      with self._lock:
        scanning, active = self.state.scan_enabled, self.state.scan_type == 0x01
      if scanning:
        ev_type = 0x00 if pdu == LinkLayerPduType.ADV_IND.value else 0x04
        self.emit_h4(
            build_le_advertising_report_event(
                ev_type,
                frame.src_addr_type,
                frame.src_bd_addr,
                frame.payload,
                frame.rssi,
            )
        )
        if active and ev_type == 0x00:
          self.phy_hub.transmit(
              LinkLayerFrame(
                  pdu_type=LinkLayerPduType.SCAN_REQ.value,
                  src_bd_addr=self.state.bd_addr,
                  dst_bd_addr=frame.src_bd_addr,
                  src_addr_type=self.state.own_addr_type,
              )
          )
      return True
    if pdu == LinkLayerPduType.SCAN_REQ.value:
      with self._lock:
        adv_enabled, scan_rsp = self.state.adv_enabled, self.state.scan_rsp_data
      if adv_enabled:
        self.broadcast_advertising_once()
        if scan_rsp:
          self.phy_hub.transmit(
              LinkLayerFrame(
                  pdu_type=LinkLayerPduType.SCAN_RSP.value,
                  src_bd_addr=self.state.bd_addr,
                  dst_bd_addr=frame.src_bd_addr,
                  src_addr_type=self.state.own_addr_type,
                  payload_hex=scan_rsp.hex(),
              )
          )
      return True
    return False

  def _on_phy_conn_frame(self, frame: LinkLayerFrame) -> None:
    """Processes CONNECT_IND and CONNECT_RSP frames from the PHY."""
    is_periph = frame.pdu_type == LinkLayerPduType.CONNECT_IND.value
    role = 0x01 if is_periph else 0x00
    if is_periph:
      with self._lock:
        self.state.adv_enabled = False
    handle = self._allocate_connection(
        frame.src_bd_addr, frame.src_addr_type, role=role
    )
    timing = {
        'conn_interval': frame.conn_interval,
        'conn_latency': frame.conn_latency,
        'supervision_timeout': frame.supervision_timeout,
    }
    self.emit_h4(
        build_le_connection_complete_event(
            0x00,
            handle,
            role,
            frame.src_addr_type,
            frame.src_bd_addr,
            **timing,
        )
    )
    if is_periph:
      self.phy_hub.transmit(
          LinkLayerFrame(
              pdu_type=LinkLayerPduType.CONNECT_RSP.value,
              src_bd_addr=self.state.bd_addr,
              dst_bd_addr=frame.src_bd_addr,
              src_addr_type=self.state.own_addr_type,
              dst_addr_type=frame.src_addr_type,
              **timing,
          )
      )

  def on_phy_frame(self, frame: LinkLayerFrame) -> None:
    """Handles an incoming Link Layer PDU from the virtual PHY TCP Channel."""
    if self._on_phy_adv_or_scan(frame):
      return
    pdu = frame.pdu_type
    if pdu in (
        LinkLayerPduType.CONNECT_IND.value,
        LinkLayerPduType.CONNECT_RSP.value,
    ):
      self._on_phy_conn_frame(frame)
    elif pdu == LinkLayerPduType.LL_DATA.value:
      with self._lock:
        handle = self._peer_to_handle.get(frame.src_bd_addr.upper())
        if handle is not None:
          self.state.acl_rx_packets += 1
      if handle is not None:
        hdr = struct.pack(
            '<HH', (handle & 0x0FFF) | (0x02 << 12), len(frame.payload)
        )
        self.emit_h4(H4Packet(H4PacketType.ACL_DATA, hdr + frame.payload))
    elif pdu == LinkLayerPduType.LL_TERMINATE_IND.value:
      with self._lock:
        handle = self._peer_to_handle.pop(frame.src_bd_addr.upper(), None)
        if handle is not None:
          self.state.connections.pop(handle, None)
      if handle is not None:
        self.emit_h4(
            build_disconnection_complete_event(handle, frame.reason, 0x00)
        )

  def to_dict(self) -> Dict[str, object]:
    with self._lock:
      return controller_state_to_dict(self.state, self.dedicated_hci_port)
