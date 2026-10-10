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

"""Unit tests for the multi-radio stochastic RF channel simulator."""

import json
import os
import tempfile
import unittest
from unittest import mock

from cirque.connectivity import rf_channel_simulator
from cirque.connectivity.rf_channel_simulator import (
    RfChannelSimulator,
    ber_ble_gfsk,
    ber_ieee802154_oqpsk,
    ber_wifi_ofdm,
    ble_channel_freq_mhz,
    compute_802154_lqi,
    compute_openthread_link_quality,
    compute_per_from_ber,
    spectral_overlap_factor,
    thread_channel_freq_mhz,
    wifi_channel_freq_mhz,
)


class RfChannelSimulatorTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self.temp_dir = tempfile.TemporaryDirectory()
    self.conf_path = os.path.join(self.temp_dir.name, 'cirque_rf_thread.conf')
    self.sim = RfChannelSimulator(seed=42, thread_conf_path=self.conf_path)

  def tearDown(self):
    self.temp_dir.cleanup()
    super().tearDown()

  def test_channel_frequencies_and_spectral_overlap(self):
    # Wi-Fi Ch 3 is 2422 MHz (20 MHz BW -> [2412, 2432] MHz)
    # Thread Ch 15 is 2425 MHz (2 MHz BW -> [2424, 2426] MHz)
    # PSD fraction of 20 MHz Wi-Fi captured by 2 MHz Thread RX is 2/20 = 0.1,
    # whereas 100% (2/2 = 1.0) of 2 MHz Thread TX falls inside 20 MHz Wi-Fi RX.
    self.assertAlmostEqual(wifi_channel_freq_mhz(3), 2422.0)
    self.assertAlmostEqual(thread_channel_freq_mhz(15), 2425.0)
    self.assertAlmostEqual(
        spectral_overlap_factor(2422.0, 20.0, 2425.0, 2.0), 0.1
    )
    self.assertAlmostEqual(
        spectral_overlap_factor(2425.0, 2.0, 2422.0, 20.0), 1.0
    )

    # Wi-Fi Ch 11 is 2462 MHz ([2452, 2472] MHz) -> disjoint from Thread Ch 15
    self.assertAlmostEqual(wifi_channel_freq_mhz(11), 2462.0)
    self.assertAlmostEqual(
        spectral_overlap_factor(2462.0, 20.0, 2425.0, 2.0), 0.0
    )

    # BLE adv Ch 38 is 2426 MHz ([2425, 2427] MHz) -> 50% overlap w/ Thread 15
    self.assertAlmostEqual(ble_channel_freq_mhz(38), 2426.0)
    self.assertAlmostEqual(
        spectral_overlap_factor(2426.0, 2.0, 2425.0, 2.0), 0.5
    )

  def test_path_loss_and_wall_attenuation_monotonicity(self):
    self.sim.update_config({
        'enabled': True,
        'shadowing_sigma_db': 0.0,
        'fading_mode': 'none',
        'path_loss_exp': 2.5,
        'wall_loss_db': 6.0,
    })
    self.sim.set_node_position('tx', 0.0, 0.0, 1.0, walls=0)
    self.sim.set_node_position('rx_near', 1.5, 0.0, 1.0, walls=0)
    self.sim.set_node_position('rx_far', 15.0, 0.0, 1.0, walls=0)
    self.sim.set_node_position('rx_far_walled', 15.0, 0.0, 1.0, walls=2)

    s_near = self.sim.evaluate_packet(
        'thread', 'tx', 'rx_near', frame_bytes=64, record_telemetry=False
    )
    s_far = self.sim.evaluate_packet(
        'thread', 'tx', 'rx_far', frame_bytes=64, record_telemetry=False
    )
    s_walled = self.sim.evaluate_packet(
        'thread', 'tx', 'rx_far_walled', frame_bytes=64, record_telemetry=False
    )

    self.assertGreater(s_near.rssi_dbm, s_far.rssi_dbm)
    self.assertGreater(s_far.rssi_dbm, s_walled.rssi_dbm)
    self.assertAlmostEqual(
        s_walled.path_loss_db - s_far.path_loss_db, 12.0, places=2
    )

  def test_gudmundson_ar1_shadowing_autocorrelation(self):
    self.sim.update_config({
        'enabled': True,
        'shadowing_sigma_db': 5.0,
        'shadowing_correlation_rho': 0.85,
        'fading_mode': 'none',
    })
    samples = [
        self.sim.evaluate_packet(
            'ble', 'n1', 'n2', frame_bytes=40, record_telemetry=False
        ).shadowing_db
        for _ in range(400)
    ]
    mean_x = sum(samples) / len(samples)
    var_x = sum((x - mean_x) ** 2 for x in samples) / len(samples)
    cov_lag1 = sum(
        (samples[i] - mean_x) * (samples[i + 1] - mean_x)
        for i in range(len(samples) - 1)
    ) / (len(samples) - 1)
    empirical_rho = cov_lag1 / max(1e-9, var_x)
    self.assertGreater(empirical_rho, 0.65)
    self.assertLess(empirical_rho, 0.98)

  def test_modulation_ber_and_per_monotonicity(self):
    for ber_fn in (ber_ieee802154_oqpsk, ber_ble_gfsk, ber_wifi_ofdm):
      b_low = ber_fn(-5.0)
      b_mid = ber_fn(5.0)
      b_high = ber_fn(18.0)
      self.assertGreater(b_low, b_mid)
      self.assertGreaterEqual(b_mid, b_high)

    # Small BER numerical stability via expm1/log1p
    per_tiny = compute_per_from_ber(1e-7, 100)
    self.assertGreater(per_tiny, 0.0)
    self.assertAlmostEqual(per_tiny, 1.0 - (1.0 - 1e-7) ** 800, places=9)

  def test_recent_transmissions_stay_bounded(self):
    """Regression: airtime history must not grow without bound."""
    # Disabled (the default 'ideal' regime): nothing consumes the history,
    # so nothing is recorded.
    self.sim.apply_regime_preset('ideal')
    for _ in range(2000):
      self.sim.evaluate_packet(
          'thread', 'a', 'b', frame_bytes=64, record_telemetry=False
      )
    self.assertEqual(len(self.sim._recent_transmissions), 0)

    # Enabled: each frame advances a fake clock by 1 ms, so at most the
    # frames inside the 25 ms coexistence window may be retained.
    self.sim.update_config({'enabled': True, 'fading_mode': 'none'})
    clock = [1000.0]

    def fake_time():
      clock[0] += 0.001
      return clock[0]

    with mock.patch.object(rf_channel_simulator.time, 'time', fake_time):
      for _ in range(2000):
        self.sim.evaluate_packet(
            'ble', 'a', 'b', frame_bytes=64, record_telemetry=False
        )
    self.assertGreater(len(self.sim._recent_transmissions), 0)
    self.assertLessEqual(len(self.sim._recent_transmissions), 26)

  def test_regime_presets_strict_rssi_sinr_per_lqi_ordering(self):
    # Regime A: Near-Field LoS
    self.sim.apply_regime_preset('regime_a_near_los')
    for _ in range(150):
      self.sim.evaluate_packet('ble', 'android', 'lock', frame_bytes=48)
      self.sim.evaluate_packet('wifi', 'android', 'tbr', frame_bytes=256)
      self.sim.evaluate_packet('thread', 'tbr', 'lock', frame_bytes=80)
    status_a = self.sim.get_status()['telemetry']

    # Regime B: Far-Field NLOS + Shadowing + Rayleigh Fading
    self.sim.apply_regime_preset('regime_b_far_nlos_fading')
    for _ in range(150):
      self.sim.evaluate_packet('ble', 'android', 'lock', frame_bytes=48)
      self.sim.evaluate_packet('wifi', 'android', 'tbr', frame_bytes=256)
      self.sim.evaluate_packet('thread', 'tbr', 'lock', frame_bytes=80)
    status_b = self.sim.get_status()['telemetry']

    # Regime C: 2.4 GHz Co-Channel Interference Burst
    self.sim.apply_regime_preset('regime_c_cochannel_interference')
    for _ in range(150):
      self.sim.evaluate_packet('ble', 'android', 'lock', frame_bytes=48)
      self.sim.evaluate_packet('wifi', 'android', 'tbr', frame_bytes=256)
      self.sim.evaluate_packet('thread', 'tbr', 'lock', frame_bytes=80)
    status_c = self.sim.get_status()['telemetry']

    for proto in ('ble', 'wifi', 'thread'):
      self.assertGreater(
          status_a[proto]['mean_rssi_dbm'],
          status_b[proto]['mean_rssi_dbm'],
          f'{proto}: Regime A mean RSSI must exceed Regime B mean RSSI',
      )
      self.assertGreater(
          status_a[proto]['mean_sinr_db'],
          status_b[proto]['mean_sinr_db'],
          f'{proto}: Regime A mean SINR must exceed Regime B mean SINR',
      )
      self.assertGreater(
          status_b[proto]['mean_sinr_db'],
          status_c[proto]['mean_sinr_db'],
          f'{proto}: Regime B mean SINR must exceed Regime C mean SINR',
      )
      self.assertLess(
          status_a[proto]['mean_per'],
          status_b[proto]['mean_per'],
          f'{proto}: Regime A mean PER must be lower than Regime B mean PER',
      )
      self.assertLess(
          status_b[proto]['mean_per'],
          status_c[proto]['mean_per'],
          f'{proto}: Regime B mean PER must be lower than Regime C mean PER',
      )
      self.assertGreater(
          status_c[proto]['dropped_packets'],
          status_a[proto]['dropped_packets'],
          f'{proto}: Regime C dropped packets must exceed Regime A',
      )

    # Thread LQI ordering: Regime A > Regime B > Regime C
    self.assertGreater(
        status_a['thread']['mean_lqi'], status_b['thread']['mean_lqi']
    )
    self.assertGreater(
        status_b['thread']['mean_lqi'], status_c['thread']['mean_lqi']
    )
    self.assertEqual(
        compute_openthread_link_quality(int(status_a['thread']['mean_lqi'])), 3
    )

  def test_thread_config_file_sync_and_c_telemetry_harvest(self):
    self.sim.apply_regime_preset('regime_c_cochannel_interference')
    self.assertTrue(os.path.isfile(self.conf_path))
    with open(self.conf_path, 'r', encoding='utf-8') as f:
      conf_text = f.read()
    self.assertIn('enabled=1', conf_text)
    self.assertIn('regime_name=regime_c_cochannel_interference', conf_text)
    self.assertIn('fading_mode=2', conf_text)

    lqi_val = compute_802154_lqi(25.0, -50)
    self.assertGreaterEqual(lqi_val, 200)

  def test_ble_link_layer_and_virtual_wifi_integration(self):
    from cirque.virtual_bt.link_layer import LinkLayerHub
    from cirque.virtual_wifi.server import VirtualWiFiServer

    rf_shared = RfChannelSimulator.get_shared()
    try:
      rf_shared.apply_regime_preset('regime_a_near_los')
      hub = LinkLayerHub()
      rssi_a = [
          hub.get_rssi('AA:BB:CC:DD:EE:01', 'AA:BB:CC:DD:EE:02')
          for _ in range(30)
      ]
      rf_shared.apply_regime_preset('regime_b_far_nlos_fading')
      rssi_b = [
          hub.get_rssi('AA:BB:CC:DD:EE:01', 'AA:BB:CC:DD:EE:02')
          for _ in range(30)
      ]
      self.assertGreater(sum(rssi_a) / len(rssi_a), sum(rssi_b) / len(rssi_b))

      wifi_srv = VirtualWiFiServer()
      counters = wifi_srv.get_frame_counters()
      self.assertIn('dropped_rf_frames', counters)
    finally:
      rf_shared.apply_regime_preset('ideal')

  def test_spinel_hdlc_rf_proxy_pure_cirque_interception(self):
    from cirque.connectivity.threadsimpipe import (
        SpinelHdlcRfProxy,
        hdlc_decode_frame,
        hdlc_encode_frame,
    )

    # 1. HDLC encode/decode round-trip including all 5 escaped control bytes
    raw_body = bytes([0x81, 0x06, 0x71, 0x11, 0x13, 0x7D, 0x7E, 0xF8, 0x42])
    encoded = hdlc_encode_frame(raw_body)
    self.assertEqual(encoded[0], 0x7E)
    self.assertEqual(encoded[-1], 0x7E)
    decoded = hdlc_decode_frame(encoded[1:-1])
    self.assertEqual(decoded, raw_body)

    proxy = SpinelHdlcRfProxy(node_id=1, simulator=self.sim)

    # 2. Construct RCP -> Host SPINEL_PROP_STREAM_RAW (cmd=6, prop=0x71) frame
    # PackRadioFrame layout at offset 3:
    #   [psdu_len_lo, psdu_len_hi] + psdu + [rssi(-20), noise(-128), flags_lo,
    #   flags_hi, phy_len_lo(10), phy_len_hi(0), channel(15), lqi(0), ts(8B)]
    psdu = bytes([0x61, 0x88, 0x01, 0xCD, 0xAB, 0xFF, 0xFF, 0x01, 0x00, 0xAA])
    psdu_len = len(psdu)
    rx_body = (
        bytes([0x80, 0x06, 0x71, psdu_len & 0xFF, (psdu_len >> 8) & 0xFF])
        + psdu
        + bytes([
            (-20) & 0xFF,
            (-128) & 0xFF,
            0x00,
            0x00,
            0x0A,
            0x00,
            15,
            0,
        ])
        + bytes(8)
    )

    self.sim.apply_regime_preset('regime_a_near_los')
    wire_a = proxy.feed_rcp_to_host(hdlc_encode_frame(rx_body))
    body_a = hdlc_decode_frame(wire_a[1:-1])
    rssi_a = body_a[5 + psdu_len] - (256 if body_a[5 + psdu_len] >= 128 else 0)
    lqi_a = body_a[12 + psdu_len]

    self.sim.apply_regime_preset('regime_c_cochannel_interference')
    wire_c = proxy.feed_rcp_to_host(hdlc_encode_frame(rx_body))
    body_c = hdlc_decode_frame(wire_c[1:-1])
    rssi_c = body_c[5 + psdu_len] - (256 if body_c[5 + psdu_len] >= 128 else 0)
    lqi_c = body_c[12 + psdu_len]

    self.assertGreater(rssi_a, rssi_c + 10)
    self.assertGreater(lqi_a, lqi_c + 30)

    # 3. RCP -> Host SPINEL_PROP_LAST_STATUS (0x00) w/ ACK PackRadioFrame @ 6
    ack_psdu = bytes([0x02, 0x00, 0x42, 0x12, 0x34])
    ack_body = (
        bytes([0x83, 0x06, 0x00, 0x00, 0x00, 0x00, len(ack_psdu), 0x00])
        + ack_psdu
        + bytes([(-20) & 0xFF, (-128) & 0xFF, 0x00, 0x00, 0x0A, 0x00, 15, 0])
        + bytes(8)
    )
    ack_wire = proxy.feed_rcp_to_host(hdlc_encode_frame(ack_body))
    ack_out = hdlc_decode_frame(ack_wire[1:-1])
    ack_lqi = ack_out[15 + len(ack_psdu)]
    self.assertGreater(ack_lqi, 0)

    # 4. RCP -> Host SPINEL_PROP_PHY_RSSI (0x26) ambient CCA RSSI query
    cca_req = bytes([0x84, 0x06, 0x26, (-98) & 0xFF])
    cca_wire = proxy.feed_rcp_to_host(hdlc_encode_frame(cca_req))
    cca_out = hdlc_decode_frame(cca_wire[1:-1])
    cca_rssi_c = cca_out[3] - (256 if cca_out[3] >= 128 else 0)
    self.assertGreater(cca_rssi_c, -90)

    # 5. Host -> RCP outbound data TX drop -> SPINEL_STATUS_NO_ACK (0x11)
    #    preserving RCP headerUpdated=1 and 5-byte (keyIndex, frameCounter)
    tx_psdu = bytes([0x69, 0x88]) + bytes(78)  # 80-byte secured data frame
    tx_meta = bytes([15, 4, 3, 1, 1, 0, 1]) + bytes(10)  # channel=15, is_retx=0
    tx_body = (
        bytes([0x85, 0x03, 0x71, len(tx_psdu) & 0xFF, 0x00]) + tx_psdu + tx_meta
    )
    sec_trailer = bytes([0x01, 0x12, 0x34, 0x56, 0x78])
    rcp_tx_done_reply = (
        bytes([0x85, 0x06, 0x00, 0x00, 0x00, 0x01, len(ack_psdu), 0x00])
        + ack_psdu
        + bytes([(-20) & 0xFF, (-128) & 0xFF, 0x00, 0x00, 0x0A, 0x00, 15, 0])
        + bytes(8)
        + sec_trailer
    )
    converted_to_no_ack = False
    for _ in range(50):
      proxy.feed_host_to_rcp(hdlc_encode_frame(tx_body))
      host_reply_wire = proxy.feed_rcp_to_host(
          hdlc_encode_frame(rcp_tx_done_reply)
      )
      host_reply_body = hdlc_decode_frame(host_reply_wire[1:-1])
      if host_reply_body[:4] == bytes([0x85, 0x06, 0x00, 0x11]):
        converted_to_no_ack = True
        self.assertEqual(
            host_reply_body,
            bytes([0x85, 0x06, 0x00, 0x11, 0x00, 0x01]) + sec_trailer,
        )
        break
    self.assertTrue(
        converted_to_no_ack,
        'Expected at least one outbound data frame in Regime C to be rewritten'
        ' to SPINEL_STATUS_NO_ACK (0x11) with preserved security trailer',
    )

  def test_researcher_presets_d_through_h_physics(self):
    # Verify all 9 presets are listed in get_status()['available_presets']
    status = self.sim.get_status()
    self.assertEqual(
        len(status['available_presets']),
        9,
    )

    # 1. Regime D: Cell-Edge Sensitivity (d=25m, n=3.2, walls=3) -> marginal
    #    RSSI near receiver sensitivity cliff (-92 to -102 dBm)
    self.sim.set_seed(404)
    self.sim.apply_regime_preset('regime_d_cell_edge_sensitivity')
    samples_d = [
        self.sim.evaluate_packet('thread', 'tbr', 'sed', frame_len_bytes=90)
        for _ in range(150)
    ]
    mean_rssi_d = sum(s.rssi_dbm for s in samples_d) / len(samples_d)
    mean_per_d = sum(s.per for s in samples_d) / len(samples_d)
    self.assertLess(mean_rssi_d, -85.0)
    self.assertGreater(mean_per_d, 0.15)

    # 2. Regime E: Asymmetric Hidden Node -> Downlink (thread_node_1 ->
    #    thread_air) has +20 dBm TX and is far from the RX jammer, whereas
    #    Uplink (thread_air -> thread_node_1) has 0 dBm TX and arrives at
    #    thread_node_1 right next to the 0.6m Wi-Fi Ch 4 interferer.
    self.sim.set_seed(505)
    self.sim.apply_regime_preset('regime_e_asymmetric_hidden_node')
    downlink_e = [
        self.sim.evaluate_packet(
            'thread', 'thread_node_1', 'thread_air', frame_len_bytes=80
        )
        for _ in range(120)
    ]
    uplink_e = [
        self.sim.evaluate_packet(
            'thread', 'thread_air', 'thread_node_1', frame_len_bytes=80
        )
        for _ in range(120)
    ]
    mean_sinr_down = sum(s.sinr_db for s in downlink_e) / len(downlink_e)
    mean_sinr_up = sum(s.sinr_db for s in uplink_e) / len(uplink_e)
    mean_lqi_down = sum(s.lqi for s in downlink_e) / len(downlink_e)
    mean_lqi_up = sum(s.lqi for s in uplink_e) / len(uplink_e)
    self.assertGreater(mean_sinr_down, mean_sinr_up + 20.0)
    self.assertGreater(mean_lqi_down, mean_lqi_up + 60.0)

    # 3. Regime F: Bursty Body Shadowing (sigma=7.5 dB, rho=0.95) -> strong
    #    lag-1 autocorrelation (>= 0.85) producing correlated deep fades
    self.sim.set_seed(606)
    self.sim.apply_regime_preset('regime_f_bursty_body_shadowing')
    samples_f = [
        self.sim.evaluate_packet('ble', 'phone', 'lock', frame_len_bytes=64)
        for _ in range(300)
    ]
    sh = [s.shadowing_db for s in samples_f]
    mean_sh = sum(sh) / len(sh)
    centered = [x - mean_sh for x in sh]
    var_sh = sum(x * x for x in centered) / len(centered)
    cov_lag1 = sum(
        centered[i] * centered[i + 1] for i in range(len(centered) - 1)
    ) / (len(centered) - 1)
    rho_hat = cov_lag1 / var_sh
    self.assertGreater(var_sh, 15.0)
    self.assertGreater(rho_hat, 0.85)

    # 4. Regime G: Orthogonal Channel Agility -> Thread on Ch 25 (2475 MHz)
    #    and BLE on Ch 39 (2480 MHz) completely evade heavy Wi-Fi Ch 1 + Ch 6
    #    interferers (interference_dbm == -120.0 dBm, high SINR > 15 dB)
    self.sim.set_seed(707)
    self.sim.apply_regime_preset('regime_g_orthogonal_channel_agility')
    samples_g_thread = [
        self.sim.evaluate_packet('thread', 'tbr', 'sed', frame_len_bytes=80)
        for _ in range(100)
    ]
    for s in samples_g_thread:
      self.assertEqual(s.channel, 25)
      self.assertEqual(s.interference_dbm, -120.0)
    mean_sinr_g = sum(s.sinr_db for s in samples_g_thread) / len(
        samples_g_thread
    )
    self.assertGreater(mean_sinr_g, 15.0)

    # 5. Regime H: Microwave Broadband Jammer (2447 MHz, Ch 8, +23 dBm, 50%
    #    duty cycle) -> severely degrades Thread Ch 19 (2445 MHz) SINR
    self.sim.set_seed(808)
    self.sim.apply_regime_preset('regime_h_microwave_broadband_jammer')
    samples_h_thread = [
        self.sim.evaluate_packet('thread', 'tbr', 'sed', frame_len_bytes=80)
        for _ in range(120)
    ]
    mean_sinr_h = sum(s.sinr_db for s in samples_h_thread) / len(
        samples_h_thread
    )
    self.assertLess(mean_sinr_h, 0.0)
    self.assertLess(mean_sinr_h, mean_sinr_g - 25.0)

  def test_regime_packet_drops(self):
    """Verifies that regimes cause expected packet drops without Matter specifics."""
    def run_trial(preset_name: str, seed: int, n_trials: int = 150):
      self.sim.set_seed(seed)
      self.sim.apply_regime_preset(preset_name)
      if preset_name != 'ideal':
        self.sim.set_node_position(
            'tbr', x_m=0.0, y_m=0.0, z_m=1.0, tx_power_thread_dbm=4.0, walls=1
        )
        self.sim.set_node_position(
            'sed', x_m=15.0, y_m=0.0, z_m=1.0, tx_power_thread_dbm=0.0, walls=1
        )
      drops = 0
      for _ in range(n_trials):
        for direction, flen in (
            (('tbr', 'sed'), 64),
            (('sed', 'tbr'), 120),
            (('tbr', 'sed'), 48),
        ):
          sample = self.sim.evaluate_packet(
              'thread', direction[0], direction[1], frame_len_bytes=flen
          )
          if sample.dropped:
            drops += 1
      return drops

    ideal_drops = run_trial('ideal', seed=2026)
    regime_b_drops = run_trial('regime_b_far_nlos_fading', seed=2026)
    regime_f_drops = run_trial('regime_f_bursty_body_shadowing', seed=2026)

    self.assertEqual(ideal_drops, 0)
    self.assertGreater(regime_b_drops, 0)
    self.assertGreater(regime_f_drops, 0)


if __name__ == '__main__':
  unittest.main()
