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
"""Userspace TCP Virtual 802.11 Medium, AP Registry, WPA2 Auth & L2 Switch.

Exposes three local TCP ports (`control_port`, `mgmt_port`, `data_port`) for
kernel-module-free 802.11 scanning, WPA2-PSK 4-way handshake authentication,
and gated L2 Ethernet frame switching between Docker containers.
"""

from dataclasses import asdict, dataclass, field
import json
import logging
import socket
import struct
import threading
from typing import Dict, List, Optional

logger = logging.getLogger('VirtualWiFiServer')


def _ones_complement_checksum(payload_bytes: bytes) -> int:
  """Computes the RFC 1071 16-bit one's complement Internet checksum."""
  if len(payload_bytes) % 2 == 1:
    payload_bytes += b'\x00'
  word_count = len(payload_bytes) // 2
  total = sum(struct.unpack(f'!{word_count}H', payload_bytes))
  while total >> 16:
    total = (total & 0xFFFF) + (total >> 16)
  return (~total) & 0xFFFF


def _fix_ipv4_l4_checksum(frame: bytes) -> bytes:
  """Recomputes IPv4 UDP/TCP checksums for frames relayed across TAP bridges.

  Why this is required:
    Linux container `veth` interfaces enable TX checksum offload
    (`CHECKSUM_PARTIAL`) by default, leaving only the pseudo-header seed in the
    UDP (bytes 6..8) or TCP (bytes 16..18) checksum field. When `vwifi_l2_agent`
    reads raw Ethernet frames via `AF_PACKET` and forwards them over userspace
    TCP (`data_port`) into another container's `AF_PACKET` socket, the kernel
    marks the injected skb as `CHECKSUM_NONE` (received from wire) and drops
    any UDP/TCP packet with an incomplete L4 checksum.

  Ethernet + IPv4 offset layout:
    - Bytes 0..14  : Ethernet II header (`dst_mac[6]`, `src_mac[6]`, `0x0800`)
    - Byte  14     : `Version (4b) | IHL (4b)` -> `ihl = (frame[14] & 0x0F) * 4`
    - Bytes 16..18 : IPv4 `Total Length` (`!H`)
    - Byte  23     : IPv4 `Protocol` (`17` = UDP, `6` = TCP)
    - Bytes 26..34 : `src_ip[4]`, `dst_ip[4]`
    - Bytes 14+ihl : L4 segment start
  """
  ihl = (frame[14] & 0x0F) * 4
  total_len = struct.unpack('!H', frame[16:18])[0]
  if 14 + total_len > len(frame) or total_len < ihl:
    return frame
  protocol_num = frame[23]
  src_ip = frame[26:30]
  dst_ip = frame[30:34]
  l4_segment = bytearray(frame[14 + ihl : 14 + total_len])
  if protocol_num == 17 and len(l4_segment) >= 8:
    l4_segment[6:8] = b'\x00\x00'
    pseudo = src_ip + dst_ip + struct.pack('!BBH', 0, 17, len(l4_segment))
    checksum = _ones_complement_checksum(pseudo + bytes(l4_segment))
    # Per RFC 768, a computed UDP checksum of 0x0000 is transmitted as 0xFFFF.
    struct.pack_into('!H', l4_segment, 6, 0xFFFF if checksum == 0 else checksum)
    return frame[: 14 + ihl] + bytes(l4_segment) + frame[14 + total_len :]
  if protocol_num == 6 and len(l4_segment) >= 20:
    l4_segment[16:18] = b'\x00\x00'
    pseudo = src_ip + dst_ip + struct.pack('!BBH', 0, 6, len(l4_segment))
    checksum = _ones_complement_checksum(pseudo + bytes(l4_segment))
    struct.pack_into('!H', l4_segment, 16, checksum)
    return frame[: 14 + ihl] + bytes(l4_segment) + frame[14 + total_len :]
  return frame


def _fix_ipv6_l4_checksum(frame: bytes) -> bytes:
  """Recomputes IPv6 UDP/TCP/ICMPv6 checksums for TAP-relayed frames.

  Ethernet + IPv6 offset layout:
    - Bytes 0..14  : Ethernet II header (`dst_mac[6]`, `src_mac[6]`, `0x86DD`)
    - Bytes 18..20 : IPv6 `Payload Length` (`!H`)
    - Byte  20     : IPv6 `Next Header` (`17` = UDP, `6` = TCP, `58` = ICMPv6)
    - Bytes 22..54 : `src_ip6[16]`, `dst_ip6[16]`
    - Byte  54     : L4 segment start (fixed 40-byte IPv6 header)
  """
  payload_len = struct.unpack('!H', frame[18:20])[0]
  if 54 + payload_len > len(frame):
    return frame
  next_hdr = frame[20]
  src_ip6 = frame[22:38]
  dst_ip6 = frame[38:54]
  l4_segment = bytearray(frame[54 : 54 + payload_len])
  pseudo = src_ip6 + dst_ip6 + struct.pack('!I3xB', len(l4_segment), next_hdr)
  # Map Next Header -> (min_l4_header_len, checksum_byte_offset, zero_is_ffff).
  offset_map = {17: (8, 6, True), 6: (20, 16, False), 58: (4, 2, False)}
  if next_hdr not in offset_map:
    return frame
  min_len, csum_offset, zero_is_ffff = offset_map[next_hdr]
  if len(l4_segment) < min_len:
    return frame
  l4_segment[csum_offset : csum_offset + 2] = b'\x00\x00'
  checksum = _ones_complement_checksum(pseudo + bytes(l4_segment))
  if zero_is_ffff and checksum == 0:
    checksum = 0xFFFF
  struct.pack_into('!H', l4_segment, csum_offset, checksum)
  return frame[:54] + bytes(l4_segment) + frame[54 + payload_len :]


def fix_l4_checksum(frame: bytes) -> bytes:
  """Finalizes IPv4/IPv6 L4 checksums on Ethernet frames relayed via TAP."""
  if len(frame) < 14:
    return frame
  ethertype = struct.unpack('!H', frame[12:14])[0]
  if ethertype == 0x0800 and len(frame) >= 34:
    return _fix_ipv4_l4_checksum(frame)
  if ethertype == 0x86DD and len(frame) >= 54:
    return _fix_ipv6_l4_checksum(frame)
  return frame


@dataclass
class VirtualWiFiApState:
  """State of a virtual 802.11 Access Point registered on the medium."""

  ap_id: str
  ssid: str
  psk: str
  bssid: str = '02:00:00:00:01:00'
  frequency: int = 2437
  channel: int = 6
  signal: int = -40
  security: str = 'WPA2-PSK'
  ipv4_addr: str = '10.0.1.1'
  ipv6_addr: str = 'fd11:22::1'


@dataclass
class VirtualWiFiStationState:
  """State of a virtual 802.11 Station interface (wlan0) on the medium."""

  station_id: str
  ifname: str = 'wlan0'
  mac_addr: str = '02:00:00:00:02:01'
  state: str = 'disconnected'
  associated_ap_id: str = ''
  associated_ssid: str = ''
  associated_bssid: str = ''
  ipv4_addr: str = ''
  ipv6_addr: str = ''
  rx_packets: int = 0
  tx_packets: int = 0
  configured_networks: List[Dict[str, str]] = field(default_factory=list)


class VirtualWiFiServer:
  """3-port TCP Virtual Wi-Fi Medium Server (Control, 802.11 Mgmt, L2 Data)."""

  def __init__(
      self, host: str = '127.0.0.1', control_port: int = 0, **kwargs: int
  ):
    self.host = host
    self.control_port = control_port
    self.mgmt_port = int(kwargs.get('mgmt_port', 0))
    self.data_port = int(kwargs.get('data_port', 0))
    self._lock = threading.RLock()
    self._running = False
    self._aps: Dict[str, VirtualWiFiApState] = {}
    self._stations: Dict[str, VirtualWiFiStationState] = {}
    self._data_clients: Dict[str, socket.socket] = {}
    self._control_sock: Optional[socket.socket] = None
    self._mgmt_sock: Optional[socket.socket] = None
    self._data_sock: Optional[socket.socket] = None
    self._threads: List[threading.Thread] = []

  def start(self) -> None:
    """Starts the Control, Management, and L2 Data Switch TCP listeners."""
    with self._lock:
      if self._running:
        return
      self._control_sock = self._bind_tcp(self.control_port)
      self.control_port = self._control_sock.getsockname()[1]
      self._mgmt_sock = self._bind_tcp(self.mgmt_port)
      self.mgmt_port = self._mgmt_sock.getsockname()[1]
      self._data_sock = self._bind_tcp(self.data_port)
      self.data_port = self._data_sock.getsockname()[1]
      self._running = True
      for thread_name, sock, handler in (
          ('vwifi-ctrl', self._control_sock, self._handle_json_client),
          ('vwifi-mgmt', self._mgmt_sock, self._handle_json_client),
          ('vwifi-data', self._data_sock, self._handle_data_client),
      ):
        worker = threading.Thread(
            target=self._accept_loop,
            args=(sock, handler),
            name=thread_name,
            daemon=True,
        )
        worker.start()
        self._threads.append(worker)
      logger.info(
          'VirtualWiFiServer listening on %s (ctrl=%d, mgmt=%d, data=%d)',
          self.host,
          self.control_port,
          self.mgmt_port,
          self.data_port,
      )

  def _bind_tcp(self, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((self.host, port))
    sock.listen(32)
    return sock

  def stop(self) -> None:
    """Stops all TCP listeners and clears state."""
    with self._lock:
      self._running = False
      for client_sock in list(self._data_clients.values()):
        client_sock.close()
      self._data_clients.clear()
      for sock in (self._control_sock, self._mgmt_sock, self._data_sock):
        if sock is not None:
          sock.close()
      self._control_sock = None
      self._mgmt_sock = None
      self._data_sock = None

  def register_ap(
      self,
      ssid: str,
      psk: str,
      ap_id: Optional[str] = None,
      **kwargs: object,
  ) -> VirtualWiFiApState:
    """Registers or updates a Virtual 802.11 Access Point on the medium."""
    frequency = int(kwargs.get('frequency', 2437))
    channel = int(kwargs.get('channel', 6))
    signal = int(kwargs.get('signal', -40))
    bssid = kwargs.get('bssid')
    with self._lock:
      for existing in self._aps.values():
        if existing.ssid == ssid:
          existing.psk = psk
          existing.frequency = frequency
          existing.channel = channel
          existing.signal = signal
          return existing
      idx = len(self._aps) + 1
      resolved_id = ap_id or f'ap{idx - 1}'
      resolved_bssid = str(bssid) if bssid else f'02:00:00:00:01:{idx:02x}'
      ap_state = VirtualWiFiApState(
          ap_id=resolved_id,
          ssid=ssid,
          psk=psk,
          bssid=resolved_bssid,
          frequency=frequency,
          channel=channel,
          signal=signal,
      )
      self._aps[resolved_id] = ap_state
      logger.info('Registered Virtual Wi-Fi AP %s (%s)', resolved_id, ssid)
      return ap_state

  def unregister_ap(self, ap_id: str) -> None:
    with self._lock:
      self._aps.pop(ap_id, None)

  def list_aps(self) -> List[VirtualWiFiApState]:
    with self._lock:
      return list(self._aps.values())

  def register_station(
      self,
      station_id: str,
      ifname: str = 'wlan0',
      mac_addr: Optional[str] = None,
      **kwargs: object,
  ) -> VirtualWiFiStationState:
    """Registers a Virtual Wi-Fi Station (wlan0) on the medium."""
    ipv4_addr = str(kwargs.get('ipv4_addr', ''))
    ipv6_addr = str(kwargs.get('ipv6_addr', ''))
    is_ap_bridge = bool(kwargs.get('is_ap_bridge', False))
    with self._lock:
      existing = self._stations.get(station_id)
      if existing is not None:
        if ipv4_addr:
          existing.ipv4_addr = ipv4_addr
        if ipv6_addr:
          existing.ipv6_addr = ipv6_addr
        if is_ap_bridge:
          existing.state = 'completed'
        return existing
      idx = len(self._stations) + 1
      resolved_mac = mac_addr or f'02:00:00:00:02:{idx:02x}'
      station = VirtualWiFiStationState(
          station_id=station_id,
          ifname=ifname,
          mac_addr=resolved_mac,
          state='completed' if is_ap_bridge else 'disconnected',
          ipv4_addr=ipv4_addr,
          ipv6_addr=ipv6_addr,
      )
      self._stations[station_id] = station
      return station

  def unregister_station(self, station_id: str) -> None:
    with self._lock:
      self._stations.pop(station_id, None)
      sock = self._data_clients.pop(station_id, None)
      if sock is not None:
        sock.close()

  def get_station(self, station_id: str) -> Optional[VirtualWiFiStationState]:
    with self._lock:
      return self._stations.get(station_id)

  def list_stations(self) -> List[VirtualWiFiStationState]:
    with self._lock:
      return list(self._stations.values())

  def authenticate_and_associate(
      self, station_id: str, ssid: str, psk: str
  ) -> Dict[str, object]:
    """Performs 802.11 Authentication + WPA2-PSK 4-Way Handshake check."""
    with self._lock:
      station = self._stations.get(station_id) or self.register_station(
          station_id
      )
      station.tx_packets += 4

      target_ap = next(
          (ap for ap in self._aps.values() if ap.ssid == ssid), None
      )
      if target_ap is None:
        station.state = 'disconnected'
        return {'ok': False, 'reason': 'ssid_not_found', 'status_code': 1}

      if target_ap.psk and target_ap.psk != psk:
        station.state = 'disconnected'
        return {'ok': False, 'reason': 'invalid_psk', 'status_code': 15}

      station.state = 'completed'
      station.associated_ap_id = target_ap.ap_id
      station.associated_ssid = target_ap.ssid
      station.associated_bssid = target_ap.bssid
      station.rx_packets += 4
      logger.info(
          'Station %s authenticated and associated to AP %s (ssid=%s)',
          station_id,
          target_ap.ap_id,
          target_ap.ssid,
      )
      return {
          'ok': True,
          'ap': asdict(target_ap),
          'station': asdict(station),
      }

  def disconnect_station(self, station_id: str) -> Dict[str, object]:
    with self._lock:
      station = self._stations.get(station_id)
      if station is not None:
        station.state = 'disconnected'
        station.associated_ap_id = ''
        station.associated_ssid = ''
        station.associated_bssid = ''
      return {'ok': True}

  def _accept_loop(self, srv: socket.socket, handler) -> None:
    while self._running:
      try:
        client_sock, _ = srv.accept()
      except OSError:
        break
      threading.Thread(target=handler, args=(client_sock,), daemon=True).start()

  def _recv_exact(self, sock: socket.socket, num_bytes: int) -> Optional[bytes]:
    buffer = bytearray()
    while len(buffer) < num_bytes:
      chunk = sock.recv(num_bytes - len(buffer))
      if not chunk:
        return None
      buffer.extend(chunk)
    return bytes(buffer)

  def _relay_l2_frame(self, station_id: str, frame: bytes) -> None:
    """Forwards an Ethernet frame between WPA2-authenticated stations.

    Security & Isolation Invariant:
      - Unassociated stations (`state != 'completed'`) have their L2 switch
        port locked: any frame sent by or destined for an unauthenticated
        station is silently dropped.
      - This guarantees that a Matter node cannot exchange ARP, ICMPv6, mDNS,
        or Operational CASE (`UDP/5540`) packets over `wlan0` until BLE
        commissioning completes `AddOrUpdateWiFiNetwork` + `ConnectNetwork`.
    """
    fixed_frame = fix_l4_checksum(frame)
    out_pkt = struct.pack('!H', len(fixed_frame)) + fixed_frame
    with self._lock:
      src_st = self._stations.get(station_id)
      if src_st is not None:
        src_st.tx_packets += 1
        if src_st.state != 'completed':
          return
      targets = [
          (peer_id, peer_sock)
          for peer_id, peer_sock in self._data_clients.items()
          if peer_id != station_id
      ]
    for peer_id, peer_sock in targets:
      with self._lock:
        dst_st = self._stations.get(peer_id)
        if dst_st is not None and dst_st.state != 'completed':
          continue
        if dst_st is not None:
          dst_st.rx_packets += 1
      try:
        peer_sock.sendall(out_pkt)
      except OSError:
        pass

  def _cleanup_data_client(
      self, station_id: str, client_sock: socket.socket
  ) -> None:
    if station_id:
      with self._lock:
        if self._data_clients.get(station_id) is client_sock:
          self._data_clients.pop(station_id, None)
    client_sock.close()

  def _handle_data_client(self, client_sock: socket.socket) -> None:
    """Relays L2 Ethernet frames between associated stations on data_port.

    Wire framing on `data_port`:
      1. Handshake packet: `[StationIdLen:2B BE][StationId UTF-8 bytes]`
      2. Ethernet stream : `[FrameLen:2B BE][Raw Ethernet II frame bytes]...`
    """
    station_id = ''
    try:
      client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
      hdr = self._recv_exact(client_sock, 2)
      if not hdr:
        return
      hello_bytes = self._recv_exact(client_sock, struct.unpack('!H', hdr)[0])
      if not hello_bytes:
        return
      station_id = hello_bytes.decode('utf-8', errors='replace').strip()
      with self._lock:
        self._data_clients[station_id] = client_sock
        if station_id not in self._stations:
          self.register_station(station_id)
      while self._running:
        fhdr = self._recv_exact(client_sock, 2)
        if not fhdr:
          break
        flen = struct.unpack('!H', fhdr)[0]
        frame = self._recv_exact(client_sock, flen) if flen > 0 else None
        if not frame:
          break
        self._relay_l2_frame(station_id, frame)
    except OSError:
      pass
    finally:
      self._cleanup_data_client(station_id, client_sock)

  def _process_json_stream(self, client_sock: socket.socket) -> None:
    f_in = client_sock.makefile('r', encoding='utf-8')
    for raw_line in f_in:
      line = raw_line.strip()
      if line:
        resp = self._dispatch_rpc(json.loads(line))
        client_sock.sendall((json.dumps(resp) + '\n').encode('utf-8'))

  def _handle_json_client(self, client_sock: socket.socket) -> None:
    try:
      self._process_json_stream(client_sock)
    except (OSError, ValueError, json.JSONDecodeError):
      pass
    finally:
      client_sock.close()

  def _dispatch_rpc(self, req: Dict[str, object]) -> Dict[str, object]:
    cmd = str(req.get('cmd', '')).strip().lower()
    if cmd == 'register_ap':
      ap_state = self.register_ap(
          str(req.get('ssid', '')),
          str(req.get('psk', '')),
          ap_id=req.get('ap_id'),
          bssid=req.get('bssid'),
          frequency=int(req.get('frequency', 2437)),
          channel=int(req.get('channel', 6)),
          signal=int(req.get('signal', -40)),
      )
      return {'ok': True, 'ap': asdict(ap_state)}
    if cmd == 'register_station':
      station = self.register_station(
          str(req.get('station_id', 'wifi0')),
          mac_addr=req.get('mac') or req.get('mac_addr'),
          ipv4_addr=str(req.get('ipv4_addr', '')),
          ipv6_addr=str(req.get('ipv6_addr', '')),
      )
      if req.get('auto_connect') and self.list_aps():
        ap0 = self.list_aps()[0]
        self.authenticate_and_associate(station.station_id, ap0.ssid, ap0.psk)
      return {'ok': True, 'station': asdict(station)}
    if cmd in ('list_aps', 'scan'):
      return {'ok': True, 'aps': [asdict(a) for a in self.list_aps()]}
    if cmd == 'list_stations':
      return {'ok': True, 'stations': [asdict(s) for s in self.list_stations()]}
    if cmd in ('authenticate', 'connect'):
      return self.authenticate_and_associate(
          str(req.get('station_id', 'wifi0')),
          str(req.get('ssid', '')),
          str(req.get('psk', '')),
      )
    if cmd in ('disconnect_station', 'disconnect'):
      return self.disconnect_station(str(req.get('station_id', 'wifi0')))
    if cmd == 'get_state':
      st = self.get_station(str(req.get('station_id', 'wifi0')))
      return {'ok': st is not None, 'station': asdict(st) if st else {}}
    if cmd == 'get_status':
      return {
          'ok': True,
          'aps': [asdict(a) for a in self.list_aps()],
          'stations': [asdict(s) for s in self.list_stations()],
      }
    return {'ok': False, 'error': f'unknown_cmd:{cmd}'}
