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
"""Unit tests for kernel-module-free VirtualWiFiServer and WiFiCapability."""

import json
import os
import socket
import struct
import time
import unittest

from cirque.capabilities.wificapability import WiFiCapability
from cirque.virtual_wifi.server import VirtualWiFiServer, fix_l4_checksum


class TestVirtualWiFiControllerSystem(unittest.TestCase):
  """Verifies VirtualWiFiServer 3-port TCP planes and L2 gate."""

  def setUp(self):
    super().setUp()
    self.server = VirtualWiFiServer(host='127.0.0.1')
    self.server.start()

  def tearDown(self):
    self.server.stop()
    WiFiCapability.WIFI_STATIONS_LIST.clear()
    WiFiCapability.stop_virtual_server()
    super().tearDown()

  def _send_ctrl(self, payload: dict) -> dict:
    with socket.create_connection(
        ('127.0.0.1', self.server.control_port), timeout=3.0
    ) as sock:
      sock.sendall((json.dumps(payload) + '\n').encode('utf-8'))
      data = b''
      while b'\n' not in data:
        chunk = sock.recv(4096)
        if not chunk:
          break
        data += chunk
      return json.loads(data.decode('utf-8').strip())

  def test_ap_registration_scan_and_wpa2_auth(self):
    """Verifies AP registration, scan, wrong-key rejection, and WPA2."""
    res = self._send_ctrl({
        'cmd': 'REGISTER_AP',
        'ap_id': 'ap0',
        'ssid': 'CHIP-Test-SSID',
        'psk': 'ValidPassphrase123',
        'bssid': '02:00:00:00:01:01',
    })
    self.assertTrue(res.get('ok'))

    self._send_ctrl({
        'cmd': 'REGISTER_STATION',
        'station_id': 'wifi0',
        'mac': '02:00:00:00:02:0a',
        'ipv4_addr': '10.0.1.10',
        'ipv6_addr': 'fd11:22::a',
    })

    scan_res = self._send_ctrl({'cmd': 'SCAN', 'station_id': 'wifi0'})
    self.assertTrue(scan_res.get('ok'))
    aps = scan_res.get('aps', [])
    self.assertEqual(len(aps), 1)
    self.assertEqual(aps[0]['ssid'], 'CHIP-Test-SSID')

    # Wrong PSK must fail authentication and keep station disconnected.
    bad_conn = self._send_ctrl({
        'cmd': 'CONNECT',
        'station_id': 'wifi0',
        'ssid': 'CHIP-Test-SSID',
        'psk': 'WrongPassword',
    })
    self.assertFalse(bad_conn.get('ok'))
    st = self._send_ctrl({'cmd': 'GET_STATE', 'station_id': 'wifi0'})['station']
    self.assertEqual(st['state'], 'disconnected')

    # Valid PSK completes 4-way handshake and transitions state to 'completed'.
    good_conn = self._send_ctrl({
        'cmd': 'CONNECT',
        'station_id': 'wifi0',
        'ssid': 'CHIP-Test-SSID',
        'psk': 'ValidPassphrase123',
    })
    self.assertTrue(good_conn.get('ok'))
    st = self._send_ctrl({'cmd': 'GET_STATE', 'station_id': 'wifi0'})['station']
    self.assertEqual(st['state'], 'completed')

  def test_l2_switch_blocks_unauthenticated_and_forwards_authenticated_frames(
      self,
  ):
    """Verifies L2 switch drops frames until stations complete WPA2."""
    self._send_ctrl(
        dict(cmd='REGISTER_AP', ap_id='ap0', ssid='AP1', psk='Pass123')
    )
    self._send_ctrl(
        dict(cmd='REGISTER_STATION', station_id='sta_a', auto_connect=True)
    )
    self._send_ctrl(
        dict(cmd='REGISTER_STATION', station_id='sta_b', auto_connect=False)
    )
    addr = ('127.0.0.1', self.server.data_port)
    with (
        socket.create_connection(addr, timeout=1.0) as sa,
        socket.create_connection(addr, timeout=1.0) as sb,
    ):
      sa.sendall(struct.pack('!H', 5) + b'sta_a')
      sb.sendall(struct.pack('!H', 5) + b'sta_b')
      sb.settimeout(0.25)
      eth_frame = bytes.fromhex('02000000020b02000000020a0800') + b'PAYLOAD'
      pkt = struct.pack('!H', len(eth_frame)) + eth_frame

      sa.sendall(pkt)
      with self.assertRaises(socket.timeout):
        sb.recv(1024)

      conn_res = self._send_ctrl(
          dict(cmd='CONNECT', station_id='sta_b', ssid='AP1', psk='Pass123')
      )
      self.assertTrue(conn_res.get('ok'))
      sa.sendall(pkt)
      frame_len = struct.unpack('!H', sb.recv(2))[0]
      self.assertEqual(sb.recv(frame_len), eth_frame)

  def test_l2_switch_mac_learning_and_unicast_vs_broadcast(self):
    """Verifies L2 switch MAC learning.

    Unicast forwards only to target, broadcast floods to all peers.
    """
    self._send_ctrl(
        dict(cmd='REGISTER_AP', ap_id='ap0', ssid='AP1', psk='Pass123')
    )
    for sid, mac in (
        ('sta_a', '02:00:00:00:02:0a'),
        ('sta_b', '02:00:00:00:02:0b'),
        ('sta_c', '02:00:00:00:02:0c'),
    ):
      self._send_ctrl(
          dict(
              cmd='REGISTER_STATION',
              station_id=sid,
              mac=mac,
              auto_connect=True,
          )
      )

    addr = ('127.0.0.1', self.server.data_port)
    with (
        socket.create_connection(addr, timeout=1.0) as sa,
        socket.create_connection(addr, timeout=1.0) as sb,
        socket.create_connection(addr, timeout=1.0) as sc,
    ):
      sa.sendall(struct.pack('!H', 5) + b'sta_a')
      sb.sendall(struct.pack('!H', 5) + b'sta_b')
      sc.sendall(struct.pack('!H', 5) + b'sta_c')
      deadline = time.monotonic() + 2.0
      while (
          any(
              s not in self.server._data_clients
              for s in ('sta_a', 'sta_b', 'sta_c')
          )
          and time.monotonic() < deadline
      ):
        time.sleep(0.01)
      self.assertIn('sta_a', self.server._data_clients)
      self.assertIn('sta_b', self.server._data_clients)
      self.assertIn('sta_c', self.server._data_clients)
      sb.settimeout(0.5)
      sc.settimeout(0.2)

      # 1. sta_b sends a frame to train L2 switch with src 02:00:00:00:02:0b
      b_mac = bytes.fromhex('02000000020b')
      c_mac = bytes.fromhex('02000000020c')
      a_mac = bytes.fromhex('02000000020a')
      b_train_frame = c_mac + b_mac + bytes.fromhex('0800') + b'TRAIN'
      sb.sendall(struct.pack('!H', len(b_train_frame)) + b_train_frame)
      sc_len = struct.unpack('!H', sc.recv(2))[0]
      self.assertEqual(sc.recv(sc_len), b_train_frame)

      # 2. sta_a sends a unicast frame destined to sta_b (02:00:00:00:02:0b).
      # It MUST be delivered to sta_b and NOT flooded to sta_c.
      unicast_frame = b_mac + a_mac + bytes.fromhex('0800') + b'UNICAST'
      sa.sendall(struct.pack('!H', len(unicast_frame)) + unicast_frame)

      sb_len = struct.unpack('!H', sb.recv(2))[0]
      self.assertEqual(sb.recv(sb_len), unicast_frame)
      with self.assertRaises(socket.timeout):
        sc.recv(1024)

      # 3. sta_a sends a broadcast frame (ff:ff:ff:ff:ff:ff).
      # It MUST be delivered to both sta_b and sta_c.
      bcast_frame = (b'\xff' * 6) + a_mac + bytes.fromhex('0800') + b'BCAST'
      sa.sendall(struct.pack('!H', len(bcast_frame)) + bcast_frame)

      sb_bcast_len = struct.unpack('!H', sb.recv(2))[0]
      self.assertEqual(sb.recv(sb_bcast_len), bcast_frame)
      sc_bcast_len = struct.unpack('!H', sc.recv(2))[0]
      self.assertEqual(sc.recv(sc_bcast_len), bcast_frame)

  def test_dual_mode_env_toggle_and_l4_checksum_helper(self):
    """Verifies CIRQUE_USE_LEGACY_HWSIM fallback and IPv4 UDP checksum fix."""
    os.environ['CIRQUE_USE_LEGACY_HWSIM'] = '0'
    cap = WiFiCapability()
    self.assertTrue(cap.use_virtual_wifi_tcp)
    self.assertTrue(cap.station_id.startswith('wifi'))

    eth_hdr = bytes.fromhex('02000000020b02000000020a0800')
    ip_a = socket.inet_aton('10.0.1.10')
    ip_b = socket.inet_aton('10.0.1.11')
    ip_hdr = struct.pack(
        '!BBHHHBBH4s4s', 0x45, 0, 32, 1, 0, 64, 17, 0, ip_a, ip_b
    )
    udp_hdr = struct.pack('!HHHH', 5540, 5540, 12, 0) + b'PING'
    fixed = fix_l4_checksum(eth_hdr + ip_hdr + udp_hdr)
    self.assertNotEqual(struct.unpack('!H', fixed[40:42])[0], 0)

  def test_checksum_edge_cases_ipv4(self):
    """Covers odd-length payloads and IPv4 TCP/ICMP checksum paths."""
    from cirque.virtual_wifi.server import _ones_complement_checksum

    self.assertIsInstance(_ones_complement_checksum(b'\x01\x02\x03'), int)
    self.assertEqual(fix_l4_checksum(b'short'), b'short')
    arp = bytes.fromhex('ffffffffffff02000000020a0806') + b'\x00' * 28
    self.assertEqual(fix_l4_checksum(arp), arp)

    eth4 = bytes.fromhex('02000000020b02000000020a0800')
    ipa, ipb = socket.inet_aton('10.0.1.10'), socket.inet_aton('10.0.1.11')
    bad_ip = struct.pack(
        '!BBHHHBBH4s4s', 0x45, 0, 10, 1, 0, 64, 17, 0, ipa, ipb
    )
    self.assertEqual(fix_l4_checksum(eth4 + bad_ip), eth4 + bad_ip)

    tcp = struct.pack('!HHIIBBHHH', 1234, 80, 1, 1, 0x50, 2, 1024, 0, 0) + b'A'
    ip_tcp = struct.pack(
        '!BBHHHBBH4s4s', 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0, ipa, ipb
    )
    fixed_tcp = fix_l4_checksum(eth4 + ip_tcp + tcp)
    self.assertNotEqual(struct.unpack('!H', fixed_tcp[50:52])[0], 0)

    ip_icmp = struct.pack(
        '!BBHHHBBH4s4s', 0x45, 0, 28, 1, 0, 64, 1, 0, ipa, ipb
    )
    icmp = eth4 + ip_icmp + b'\x08\x00\x00\x00\x00\x01\x00\x01'
    self.assertEqual(fix_l4_checksum(icmp), icmp)

  def test_fix_l4_checksum_skips_ipv4_fragments(self):
    """Verifies that IPv4 packets with MF flag or offset are untouched."""
    eth4 = bytes.fromhex('02000000020b02000000020a0800')
    ipa, ipb = socket.inet_aton('10.0.1.10'), socket.inet_aton('10.0.1.11')
    udp_payload = struct.pack('!HHHH', 5540, 5540, 12, 0) + b'FRAG'

    # 1. Packet with MF (More Fragments) bit set: flags_offset = 0x2000
    ip_mf = struct.pack(
        '!BBHHHBBH4s4s',
        0x45,
        0,
        20 + len(udp_payload),
        1,
        0x2000,
        64,
        17,
        0,
        ipa,
        ipb,
    )
    frame_mf = eth4 + ip_mf + udp_payload
    self.assertEqual(fix_l4_checksum(frame_mf), frame_mf)

    # 2. Packet with non-zero Fragment Offset: offset=100 * 8 bytes
    ip_frag = struct.pack(
        '!BBHHHBBH4s4s',
        0x45,
        0,
        20 + len(udp_payload),
        1,
        100,
        64,
        17,
        0,
        ipa,
        ipb,
    )
    frame_frag = eth4 + ip_frag + udp_payload
    self.assertEqual(fix_l4_checksum(frame_frag), frame_frag)

  def test_checksum_edge_cases_ipv6(self):
    """Covers IPv6 short, unknown next_hdr, ICMPv6, and UDP 0xFFFF."""
    from cirque.virtual_wifi.server import _ones_complement_checksum

    eth6 = bytes.fromhex('02000000020b02000000020a86dd')
    s6 = socket.inet_pton(socket.AF_INET6, 'fd11:22::10')
    d6 = socket.inet_pton(socket.AF_INET6, 'fd11:22::11')
    hdr6 = lambda plen, nh: eth6 + struct.pack(
        '!IHBB16s16s', 0x60000000, plen, nh, 64, s6, d6
    )

    self.assertEqual(fix_l4_checksum(hdr6(50, 17)), hdr6(50, 17))
    self.assertEqual(
        fix_l4_checksum(hdr6(4, 99) + b'1234'), hdr6(4, 99) + b'1234'
    )
    self.assertEqual(
        fix_l4_checksum(hdr6(4, 17) + b'1234'), hdr6(4, 17) + b'1234'
    )

    fixed_icmp6 = fix_l4_checksum(hdr6(8, 58) + b'\x80\x00\x00\x00TEST')
    self.assertNotEqual(struct.unpack('!H', fixed_icmp6[56:58])[0], 0)

    pseudo = s6 + d6 + struct.pack('!I3xB', 10, 17)
    udp_pfx = struct.pack('!HHHH', 1000, 2000, 10, 0)
    partial = _ones_complement_checksum(pseudo + udp_pfx + b'\x00\x00')
    fixed_udp6 = fix_l4_checksum(
        hdr6(10, 17) + udp_pfx + struct.pack('!H', partial)
    )
    self.assertEqual(struct.unpack('!H', fixed_udp6[60:62])[0], 0xFFFF)

  def test_server_lifecycle_and_rpc_commands(self):
    """Covers idempotent start, AP/station updates, and RPCs."""
    self.server.start()
    ap1 = self.server.register_ap('TestSSID', 'psk1', ap_id='ap0')
    ap2 = self.server.register_ap('TestSSID', 'psk2', frequency=5180)
    self.assertEqual(ap1.ap_id, ap2.ap_id)
    self.assertEqual(ap2.psk, 'psk2')

    self.server.register_station('st1', ipv4_addr='10.0.1.11')
    st_up = self.server.register_station(
        'st1', ipv4_addr='10.0.1.12', ipv6_addr='fd11:22::12', is_ap_bridge=True
    )
    self.assertEqual(st_up.ipv4_addr, '10.0.1.12')
    self.assertEqual(st_up.state, 'completed')

    no_ssid = self.server.authenticate_and_associate('st_new', 'Missing', 'psk')
    self.assertFalse(no_ssid['ok'])
    self.assertTrue(self._send_ctrl({'cmd': 'list_stations'})['ok'])
    self.assertTrue(
        self._send_ctrl({'cmd': 'disconnect', 'station_id': 'st1'})['ok']
    )
    self.assertTrue(self._send_ctrl({'cmd': 'get_status'})['ok'])
    self.assertFalse(self._send_ctrl({'cmd': 'no_such_cmd'})['ok'])

  def test_server_socket_and_data_relay_edge_cases(self):
    """Covers invalid JSON, partial headers, and broken peer sockets."""
    c_addr = ('127.0.0.1', self.server.control_port)
    d_addr = ('127.0.0.1', self.server.data_port)
    with socket.create_connection(c_addr, timeout=1.0) as s:
      s.sendall(b'   \nNOT_JSON\n')
    with socket.create_connection(d_addr, timeout=1.0):
      pass
    with socket.create_connection(d_addr, timeout=1.0) as s:
      s.sendall(struct.pack('!H', 4))
    with socket.create_connection(d_addr, timeout=1.0) as s:
      s.sendall(struct.pack('!H', 6) + b'unreg1')
      s.sendall(
          struct.pack('!H', 14) + b'12345678901234' + struct.pack('!H', 0)
      )
      s.shutdown(socket.SHUT_WR)
      self.assertEqual(s.recv(1), b'')
    with socket.create_connection(d_addr, timeout=1.0) as s:
      s.sendall(struct.pack('!H', 8) + b'st_trunc')
      s.sendall(struct.pack('!H', 100))

    bad_a, bad_b = socket.socketpair()
    bad_b.close()
    self.server.register_station('st_peer', is_ap_bridge=False)
    self.server._data_clients['st_peer'] = bad_a
    self.server.register_station('st_sender', is_ap_bridge=True)
    self.server._relay_l2_frame('st_sender', b'12345678901234')
    self.server.register_station('st_peer', is_ap_bridge=True)
    self.server._relay_l2_frame('st_sender', b'12345678901234')
    self.server._relay_l2_frame('st_sender', b'short')

    self.server.register_station('st_self', is_ap_bridge=True)
    own_mac = b'\x02\x00\x00\x00\x00\x01'
    self.server._mac_to_station[own_mac] = 'st_self'
    self.server._relay_l2_frame('st_self', own_mac + own_mac + b'\x08\x00data')

    self.server.unregister_station('st_peer')
    self.server.unregister_ap('ap0')

    class _ErrSock:

      def close(self):
        raise OSError('close failure')

    self.server._mac_to_station[b'\x00\x11\x22\x33\x44\x55'] = 'st_err'
    self.server._data_clients['st_err'] = _ErrSock()
    self.server.unregister_station('st_err')

    self.server._mac_to_station[b'\x00\x11\x22\x33\x44\x66'] = 'st_disc'
    self.server.disconnect_station('st_disc')

    self.server._cleanup_data_client('st_err2', _ErrSock())

    closed_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed_sock.close()
    self.server._accept_loop(closed_sock, self.server._handle_json_client)
    self.server._handle_data_client(closed_sock)
    st_cleanup_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    self.server._data_clients['st_cleanup'] = st_cleanup_sock
    st_cleanup_sock.close()

    self.server._control_sock = _ErrSock()
    self.server.stop()

  def test_docker_wifi_bridge_methods(self):
    """Covers DockerVirtualWiFiManager proxies and container setup helpers."""
    import shutil
    import tempfile
    import time
    from types import SimpleNamespace
    from cirque.virtual_wifi.docker_wifi_bridge import DockerVirtualWiFiManager

    tmp_dir = tempfile.mkdtemp(prefix='vwifi_ut_')
    try:
      self.server.register_ap('HomeSSID', 'HomePass123', ap_id='ap0')
      with open(
          os.path.join(tmp_dir, 'control.sock'), 'w', encoding='utf-8'
      ) as f:
        f.write('stale')
      mgr = DockerVirtualWiFiManager(self.server, runtime_dir=tmp_dir)
      dbus_dir = mgr.get_container_dbus_dir('wifi0')
      with open(os.path.join(dbus_dir, 'pid'), 'w', encoding='utf-8') as f:
        f.write('123')
      mgr.get_container_dbus_dir('wifi0')

      with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as usock:
        usock.settimeout(2.0)
        usock.connect(mgr.control_sock_path)
        usock.sendall(b'{"cmd": "list_aps"}\n')
        self.assertTrue(json.loads(usock.recv(4096).decode('utf-8'))['ok'])

      mgr.register_station('ap_br', 0, is_ap=True)
      mac, ipv4, ipv6 = mgr.register_station('wifi0', 0, is_ap=False)
      with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as dsock:
        dsock.settimeout(2.0)
        dsock.connect(mgr.data_sock_path)
        dsock.sendall(struct.pack('!H', 5) + b'ap_br')
        deadline = time.monotonic() + 1.5
        while (
            'ap_br' not in self.server._data_clients
            and time.monotonic() < deadline
        ):
          time.sleep(0.01)
        self.assertIn('ap_br', self.server._data_clients)
        eth_pkt = (
            b'\xff' * 6 + b'\x02\x00\x00\x00\x01\x01\x08\x06' + b'\x00' * 28
        )
        dsock.sendall(struct.pack('!H', len(eth_pkt)) + eth_pkt)
        while (
            self.server.get_station('ap_br').tx_packets < 1
            and time.monotonic() < deadline
        ):
          time.sleep(0.01)
        self.assertGreaterEqual(self.server.get_station('ap_br').tx_packets, 1)

      mgr.setup_container_interface(
          'w_none', SimpleNamespace(container=None), mac
      )
      exec_cmds = []
      fnode = SimpleNamespace(
          container=SimpleNamespace(
              exec_run=lambda cmd, **_: exec_cmds.append(cmd)
          )
      )
      mgr.setup_container_interface('wifi0', fnode, mac, ipv4, ipv6, True)
      self.assertIn(f'ip addr replace {ipv4}/24 dev wlan0', exec_cmds[0])
      self.assertIn(
          f'ip -6 addr replace {ipv6}/64 dev wlan0 nodad', exec_cmds[0]
      )
      mgr.setup_container_interface('wifi0', fnode, mac, '', '', True)
      self.assertNotIn('ip addr replace', exec_cmds[2])
      self.assertNotIn('ip -6 addr replace', exec_cmds[2])
      self.assertFalse(
          mgr.connect_station_to_ap('wifi0', 'HomeSSID', 'Bad')['ok']
      )
      self.assertTrue(
          mgr.connect_station_to_ap('wifi0', 'HomeSSID', 'HomePass123')['ok']
      )
      mgr.disconnect_station('wifi0')
      mgr.unregister_station('wifi0')
      mgr.stop_all()
      self.assertFalse(os.path.exists(mgr.control_sock_path))
      self.assertFalse(os.path.exists(mgr.data_sock_path))
      mgr.stop_all()  # A second call must be a no-op.

      bad_dir = os.path.join(tmp_dir, 'bad_rt')
      os.makedirs(os.path.join(bad_dir, 'data.sock'))
      with self.assertRaises(OSError):
        DockerVirtualWiFiManager(self.server, runtime_dir=bad_dir)
      self.assertFalse(os.path.exists(os.path.join(bad_dir, 'control.sock')))
    finally:
      shutil.rmtree(tmp_dir, ignore_errors=True)

  def _exercise_wpa_dbus_handlers(self, wpa, conn):
    """Exercises all D-Bus methods on WpaSupplicantDbusService."""
    import time
    from types import SimpleNamespace
    from gi.repository import GLib
    from cirque.virtual_wifi.wpa_dbus_daemon import (
        WPA_IFACE_PATH,
        WPA_ROOT_PATH,
    )

    inv = SimpleNamespace(
        ret=None, return_value=lambda v: setattr(inv, 'ret', v)
    )
    root_i, ifc_i = 'fi.w1.wpa_supplicant1', 'fi.w1.wpa_supplicant1.Interface'
    call = lambda p, i, m, a: wpa._handle_method_call(
        conn, ':1.1', p, i, m, a, inv
    )

    signals_received = []

    def on_signal(
        connection,
        sender,
        path,
        interface,
        signal,
        params,
        *user_data,
    ):
      del connection, sender, path, user_data
      signals_received.append((interface, signal, params.unpack()))

    sub_id_scan = conn.signal_subscribe(
        None,
        'fi.w1.wpa_supplicant1.Interface',
        'ScanDone',
        WPA_IFACE_PATH,
        None,
        0,
        on_signal,
    )
    sub_id_props = conn.signal_subscribe(
        None,
        'org.freedesktop.DBus.Properties',
        'PropertiesChanged',
        WPA_IFACE_PATH,
        None,
        0,
        on_signal,
    )

    call(WPA_ROOT_PATH, root_i, 'GetInterface', GLib.Variant('(s)', ('wlan0',)))
    r_arg = GLib.Variant('(o)', (WPA_IFACE_PATH,))
    call(WPA_ROOT_PATH, root_i, 'RemoveInterface', r_arg)

    # Calling Scan() must set Scanning to True immediately
    call(WPA_IFACE_PATH, ifc_i, 'Scan', GLib.Variant('(a{sv})', ({},)))
    scan_prop = wpa._handle_get_property(
        conn, ':1.1', WPA_IFACE_PATH, ifc_i, 'Scanning'
    )
    self.assertTrue(scan_prop.unpack())

    # Run context loop briefly so GLib timeout (20ms) fires ScanDone and
    # resets Scanning.
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
      ctx.iteration(False)
      if any(sig == 'ScanDone' for _, sig, _ in signals_received):
        break
      time.sleep(0.01)

    scan_done_signals = [s for s in signals_received if s[1] == 'ScanDone']
    self.assertGreaterEqual(len(scan_done_signals), 1)
    self.assertEqual(scan_done_signals[0][2], (True,))

    scan_prop_after = wpa._handle_get_property(
        conn, ':1.1', WPA_IFACE_PATH, ifc_i, 'Scanning'
    )
    self.assertFalse(scan_prop_after.unpack())

    conn.signal_unsubscribe(sub_id_scan)
    conn.signal_unsubscribe(sub_id_props)

    for psk in ('HomePass123', 'BadPass'):
      nd = {
          'ssid': GLib.Variant('s', 'HomeSSID'),
          'psk': GLib.Variant('s', psk),
      }
      call(WPA_IFACE_PATH, ifc_i, 'AddNetwork', GLib.Variant('(a{sv})', (nd,)))
      net_path = inv.ret.unpack()[0]
      call(
          WPA_IFACE_PATH,
          ifc_i,
          'SelectNetwork',
          GLib.Variant('(o)', (net_path,)),
      )

    self._exercise_wpa_dbus_properties(wpa, conn, net_path)
    wpa._station_state['wifi0']['current_network'] = net_path
    call(
        WPA_IFACE_PATH, ifc_i, 'RemoveNetwork', GLib.Variant('(o)', (net_path,))
    )
    for mname in ('RemoveAllNetworks', 'Disconnect', 'SaveConfig'):
      call(WPA_IFACE_PATH, ifc_i, mname, GLib.Variant('()', ()))

  def _exercise_wpa_dbus_properties(self, wpa, conn, net_path):
    """Exercises all D-Bus property getters and setters."""
    from gi.repository import GLib
    from cirque.virtual_wifi.wpa_dbus_daemon import (
        WPA_IFACE_PATH,
        WPA_ROOT_PATH,
    )

    root_i, ifc_i = 'fi.w1.wpa_supplicant1', 'fi.w1.wpa_supplicant1.Interface'
    bss_i, net_i = 'fi.w1.wpa_supplicant1.BSS', 'fi.w1.wpa_supplicant1.Network'
    getp = lambda p, i, k: wpa._handle_get_property(conn, ':1.1', p, i, k)

    self.assertEqual(
        getp(WPA_ROOT_PATH, root_i, 'Interfaces').unpack(), [WPA_IFACE_PATH]
    )
    for prop in 'BSSs State Scanning CurrentBSS CurrentNetwork Unknown'.split():
      val = getp(WPA_IFACE_PATH, ifc_i, prop)
      if prop != 'Unknown':
        self.assertIsNotNone(val)
    bss_path = f'{WPA_IFACE_PATH}/BSSs/0'
    self.assertEqual(bytes(getp(bss_path, bss_i, 'SSID').unpack()), b'HomeSSID')
    self.assertEqual(getp(bss_path, bss_i, 'Frequency').unpack(), 2437)

    # Non-vacuous assertions on BSS properties
    bssid_bytes = bytes(getp(bss_path, bss_i, 'BSSID').unpack())
    self.assertEqual(bssid_bytes, bytes.fromhex('020000000101'))
    self.assertEqual(getp(bss_path, bss_i, 'Signal').unpack(), -40)
    self.assertEqual(
        getp(bss_path, bss_i, 'Rates').unpack(),
        [54000000, 48000000, 36000000, 24000000],
    )
    wpa_dict = getp(bss_path, bss_i, 'WPA').unpack()
    self.assertIn('KeyMgmt', wpa_dict)
    rsn_dict = getp(bss_path, bss_i, 'RSN').unpack()
    self.assertIn('KeyMgmt', rsn_dict)
    self.assertEqual(getp(bss_path, bss_i, 'WPS').unpack(), {})
    self.assertIsNone(getp(bss_path, bss_i, 'Unknown'))

    self.assertIsInstance(getp(net_path, net_i, 'Enabled').unpack(), bool)
    net_props = getp(net_path, net_i, 'Properties').unpack()
    self.assertIn('ssid', net_props)
    self.assertIsNone(getp(net_path, net_i, 'Unknown'))
    self.assertIsNone(getp(WPA_ROOT_PATH, 'unknown.Iface', 'Prop'))
    wpa._handle_set_property(
        conn, ':1.1', net_path, net_i, 'Enabled', GLib.Variant('b', False)
    )
    self.assertFalse(getp(net_path, net_i, 'Enabled').unpack())

  def test_wpa_dbus_daemon_lifecycle_and_handlers(self):
    """Covers WpaSupplicantDbusService attachment, methods, and properties."""
    import shutil
    import subprocess
    import tempfile
    from cirque.virtual_wifi.docker_wifi_bridge import DockerVirtualWiFiManager
    from cirque.virtual_wifi.wpa_dbus_daemon import (
        WpaSupplicantDbusService,
        _decode_dbus_bytes_or_str,
    )

    for raw in (b'CHIP', [67, 72, 73, 80], '"CHIP"', 'CHIP'):
      self.assertEqual(_decode_dbus_bytes_or_str(raw), 'CHIP')

    tmp_dir = tempfile.mkdtemp(prefix='vwifi_wpa_')
    try:
      self.server.register_ap('HomeSSID', 'HomePass123', ap_id='ap0')
      mgr = DockerVirtualWiFiManager(self.server, runtime_dir=tmp_dir)
      dbus_dir = mgr.get_container_dbus_dir('wifi0')
      wpa = WpaSupplicantDbusService(mgr)
      no_sock = os.path.join(tmp_dir, 'no.sock')
      self.assertFalse(
          wpa.attach_container_bus('w_miss', no_sock, timeout_s=0.1)
      )
      bad_sock = os.path.join(tmp_dir, 'bad.sock')
      with open(bad_sock, 'w', encoding='utf-8') as f:
        f.write('x')
      self.assertFalse(
          wpa.attach_container_bus('w_bad', bad_sock, timeout_s=0.1)
      )
      self.assertTrue(wpa.start())

      bus_sock = os.path.join(dbus_dir, 'system_bus_socket')
      cmd = [
          'dbus-daemon',
          '--session',
          f'--address=unix:path={bus_sock}',
          '--nofork',
          '--nopidfile',
      ]
      dbus_proc = subprocess.Popen(cmd)
      try:
        self.assertTrue(
            wpa.attach_container_bus('wifi0', bus_sock, timeout_s=3.0)
        )
        self._exercise_wpa_dbus_handlers(wpa, wpa._conns['wifi0'])
      finally:
        wpa.stop()
        dbus_proc.terminate()
        dbus_proc.wait(timeout=3)
      mgr.stop_all()
    finally:
      shutil.rmtree(tmp_dir, ignore_errors=True)

  def test_virtual_home_topology_builder_and_verifiers(self):
    """Covers VirtualHomeNodeSpec and VirtualHomeTopology."""
    from types import SimpleNamespace
    from cirque.home import VirtualHomeNodeSpec, VirtualHomeTopology

    node_spec = VirtualHomeNodeSpec(
        name='combo',
        device_type='Combo',
        enable_thread=True,
        bd_addr='AA:BB:CC:DD:EE:99',
    )
    self.assertEqual(
        node_spec.to_device_config()['capability'],
        ['Bluetooth', 'WiFi', 'Thread'],
    )
    home_cfg = VirtualHomeTopology.default_two_node_ble_wifi_config()
    self.assertIn('wifi_ap', home_cfg)

    ping_ok = {'loss': '0% packet loss'}
    exec_log = []

    def fake_exec(cmd, nid, stream=False):
      del stream
      exec_log.append((nid, cmd))
      if 'GetManagedObjects' in cmd:
        if nid == 'mobile_controller':
          return SimpleNamespace(
              output=(
                  b'/org/bluez/hci0 AA:BB:CC:DD:EE:01 '
                  b'/org/bluez/hci0/dev_AA_BB_CC_DD_EE_02'
              )
          )
        return '/org/bluez/hci1 AA:BB:CC:DD:EE:02'
      if 'ip -4 addr show' in cmd:
        ip = '10.0.1.10' if nid == 'mobile_controller' else '10.0.1.11'
        return SimpleNamespace(output=f'inet {ip}/24'.encode())
      if cmd.startswith('ping '):
        return SimpleNamespace(output=ping_ok['loss'].encode())
      return SimpleNamespace(output=b"('completed',)")

    def assert_add_network_creds(ssid, psk):
      """Asserts each node's AddNetwork used ssid/psk, then clears the log."""
      add_network = [(n, c) for n, c in exec_log if 'AddNetwork' in c]
      self.assertEqual(
          [nid for nid, _ in add_network],
          ['mobile_controller', 'iot_end_device'],
      )
      for _, cmd in add_network:
        self.assertIn(f"'ssid': <'{ssid}'>", cmd)
        self.assertIn(f"'psk': <'{psk}'>", cmd)
      exec_log.clear()

    f_home = SimpleNamespace(execute_device_cmd=fake_exec)
    bt_res = VirtualHomeTopology.verify_virtual_bt_between_nodes(
        f_home, 'mobile_controller', 'iot_end_device'
    )
    self.assertIn(
        '/org/bluez/hci0/dev_AA_BB_CC_DD_EE_02', bt_res['controller_bt']
    )
    self.assertIn('AA:BB:CC:DD:EE:02', bt_res['device_bt'])
    exec_log.clear()
    wifi_res = (
        VirtualHomeTopology.verify_virtual_wifi_commissioning_and_data_plane(
            f_home,
            'mobile_controller',
            'iot_end_device',
            'CIRQUE_HOME_AP',
            'psk',
        )
    )
    self.assertEqual(wifi_res['controller_ip'], '10.0.1.10')
    self.assertEqual(wifi_res['device_ip'], '10.0.1.11')
    self.assertTrue(wifi_res['packet_loss_zero'])
    assert_add_network_creds('CIRQUE_HOME_AP', 'psk')
    ping_ok['loss'] = '100% packet loss'
    wifi_fail = (
        VirtualHomeTopology.verify_virtual_wifi_commissioning_and_data_plane(
            f_home, 'mobile_controller', 'iot_end_device'
        )
    )
    self.assertFalse(wifi_fail['packet_loss_zero'])
    assert_add_network_creds('CIRQUE_HOME_AP', 'cirque_home_psk')
    VirtualHomeTopology.verify_virtual_wifi_commissioning_and_data_plane(
        f_home,
        'mobile_controller',
        'iot_end_device',
        ssid='OtherAP',
        psk='otherpsk',
    )
    assert_add_network_creds('OtherAP', 'otherpsk')


if __name__ == '__main__':
  unittest.main()
