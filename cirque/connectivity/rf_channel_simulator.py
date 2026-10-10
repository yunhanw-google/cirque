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
"""Unified 2.4 GHz Multi-Radio Stochastic RF Channel & Coexistence Simulator.

Models shared 2.400-2.4835 GHz ISM spectrum physics across Bluetooth LE (BLE),
IEEE 802.11 (Wi-Fi), and IEEE 802.15.4 (Thread):
  1. Log-Distance Path Loss with Wall Attenuation:
     PL(d) = PL(d0) + 10 * n * log10(max(d, d_min) / d0) + sum(W_k)
  2. Gudmundson AR(1) Temporally Correlated Log-Normal Shadowing:
     X_ij(t) = rho * X_ij(t - dt) + sqrt(1 - rho^2) * sigma_s * N(0, 1)
  3. Unit-Mean Normalized Rician (LOS, K > 0) and Rayleigh (NLOS, K = 0)
     Multipath Fading:
     |h|^2 = (sqrt(K/(K+1)) + sqrt(1/(2*(K+1))) * X)^2
           + (sqrt(1/(2*(K+1))) * Y)^2,   E[|h|^2] = 1
  4. Thermal Noise Floor and Cross-Protocol Spectral Overlap SINR:
     SINR_dB = P_rx_dBm - 10 * log10(10^(N0_dBm/10) + sum(alpha_ik * I_k_mW))
  5. Modulation-Specific SINR -> BER -> PER (-expm1(8*L*log1p(-BER))) and
     IEEE 802.15.4 LQI (0..255) / OpenThread Link Quality (0..3) Mappings.
"""

from dataclasses import asdict, dataclass, field
import glob
import json
import logging
import math
import os
import random
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('CirqueRfChannelSimulator')

DEFAULT_THREAD_RF_CONF_PATH = '/tmp/cirque_rf_thread.conf'
DEFAULT_THREAD_RF_STATS_GLOB = '/tmp/cirque_rf_thread_stats_*.json'

# Reference free-space path loss at d0 = 1.0 m for f_c = 2.44 GHz (dB).
PL_D0_2P4GHZ_DB = 40.05
# Cross-protocol transmissions older than this no longer add interference.
_COEX_WINDOW_S = 0.025


def wifi_channel_freq_mhz(channel: int) -> float:
  """Returns the 2.4 GHz center frequency (MHz) for an 802.11 channel (1-14)."""
  ch = max(1, min(14, int(channel)))
  if ch == 14:
    return 2484.0
  return 2407.0 + 5.0 * ch


def thread_channel_freq_mhz(channel: int) -> float:
  """Returns 2.4 GHz center frequency (MHz) for 802.15.4 channel (11-26)."""
  ch = max(11, min(26, int(channel)))
  return 2405.0 + 5.0 * (ch - 11)


def ble_channel_freq_mhz(channel: int) -> float:
  """Returns physical 2.4 GHz center frequency (MHz) for BLE channel 0-39."""
  ch = max(0, min(39, int(channel)))
  if ch == 37:
    return 2402.0
  if ch == 38:
    return 2426.0
  if ch == 39:
    return 2480.0
  if ch <= 10:
    return 2404.0 + 2.0 * ch
  return 2428.0 + 2.0 * (ch - 11)


def protocol_channel_spectrum(
    protocol: str, channel: int
) -> Tuple[float, float]:
  """Returns (center_freq_mhz, bandwidth_mhz) for BLE, Wi-Fi, or Thread."""
  proto = protocol.strip().lower()
  if proto in ('wifi', 'wlan', '802.11'):
    return wifi_channel_freq_mhz(channel), 20.0
  if proto in ('thread', '802.15.4', 'ot', 'wpan'):
    return thread_channel_freq_mhz(channel), 2.0
  return ble_channel_freq_mhz(channel), 2.0


def spectral_overlap_factor(
    f_tx_mhz: float,
    bw_tx_mhz: float,
    f_rx_mhz: float,
    bw_rx_mhz: float,
    *,
    aclr_sidelobe_db: Optional[float] = None,
) -> float:
  """Computes fractional TX power coupling into RX passband [0.0, 1.0].

  For rectangular PSD S_tx(f) = 1 / B_tx over [f_tx - B_tx/2, f_tx + B_tx/2]
  and unit-gain RX passband over [f_rx - B_rx/2, f_rx + B_rx/2]:
    alpha = max(0, min(f_tx + B_tx/2, f_rx + B_rx/2)
                 - max(f_tx - B_tx/2, f_rx - B_rx/2)) / B_tx

  Examples:
    - Wi-Fi Ch 6 (2437 MHz, 20 MHz -> [2427, 2447]) vs
      Thread Ch 15 (2425 MHz, 2 MHz -> [2424, 2426]) -> 0.0
    - Wi-Fi Ch 11 (2462 MHz, 20 MHz -> [2452, 2472]) vs
      Thread Ch 22 (2460 MHz, 2 MHz -> [2459, 2461]) -> 2.0 / 20.0 = 0.10
    - BLE Adv Ch 38 (2426 MHz, 2 MHz -> [2425, 2427]) vs
      Thread Ch 15 (2425 MHz, 2 MHz -> [2424, 2426]) -> 1.0 / 2.0 = 0.50
  """
  if bw_tx_mhz <= 0.0 or bw_rx_mhz <= 0.0:
    return 0.0
  tx_low = f_tx_mhz - 0.5 * bw_tx_mhz
  tx_high = f_tx_mhz + 0.5 * bw_tx_mhz
  rx_low = f_rx_mhz - 0.5 * bw_rx_mhz
  rx_high = f_rx_mhz + 0.5 * bw_rx_mhz
  inter = max(0.0, min(tx_high, rx_high) - max(tx_low, rx_low))
  overlap = inter / bw_tx_mhz
  if overlap > 0.0:
    return min(1.0, overlap)
  if aclr_sidelobe_db is not None and aclr_sidelobe_db > 0.0:
    edge_gap = max(tx_low - rx_high, rx_low - tx_high)
    half_sum = 0.5 * (bw_tx_mhz + bw_rx_mhz)
    if edge_gap <= half_sum:
      attenuation = aclr_sidelobe_db + 10.0 * (edge_gap / max(1.0, half_sum))
      return min(1.0, (bw_rx_mhz / bw_tx_mhz) * (10.0 ** (-attenuation / 10.0)))
  return 0.0


def _q_func(x: float) -> float:
  """Gaussian tail probability Q(x) = 0.5 * erfc(x / sqrt(2))."""
  if x <= 0.0:
    return 0.5
  return 0.5 * math.erfc(x / math.sqrt(2.0))


def ber_ieee802154_oqpsk(sinr_db: float) -> float:
  """IEEE 802.15.4-2020 Annex E O-QPSK DSSS (250 kbps) bit error rate.

  Uses the exact 16-ary quasi-orthogonal symbol error expansion with DSSS
  spreading factor calibration so 1% PER on 20-byte PSDU occurs near SINR = 0 dB
  (receiver sensitivity -95 dBm at -95 dBm noise floor).
  """
  # Effective post-despreading symbol SNR factor (dB offset +6.2 dB DSSS gain).
  gamma = 10.0 ** ((sinr_db + 6.2) / 10.0)
  if gamma > 8.0:
    return max(0.0, _q_func(math.sqrt(2.0 * gamma)))
  acc = 0.0
  for k in range(2, 17):
    sign = 1.0 if (k % 2 == 0) else -1.0
    comb = math.comb(16, k)
    exponent = -gamma * (1.0 - 1.0 / float(k))
    if exponent < -700.0:
      continue
    acc += sign * comb * math.exp(exponent)
  ber = (8.0 / 15.0) * (1.0 / 16.0) * acc
  return max(0.0, min(0.5, ber))


def ber_ble_gfsk(sinr_db: float) -> float:
  """BLE 1M GFSK (modulation index h=0.5) non-coherent bit error rate."""
  gamma = 10.0 ** ((sinr_db + 3.5) / 10.0)
  return max(0.0, min(0.5, 0.5 * math.erfc(math.sqrt(0.35 * gamma))))


def ber_wifi_ofdm(sinr_db: float) -> float:
  """IEEE 802.11g/n 2.4 GHz OFDM/CCK effective coded bit error rate."""
  gamma = 10.0 ** ((sinr_db + 2.0) / 10.0)
  return max(0.0, min(0.5, _q_func(math.sqrt(1.2 * gamma))))


def compute_per_from_ber(ber: float, frame_len_bytes: int) -> float:
  """Computes PER = 1 - (1 - BER)^(8 * L) without float cancellation."""
  b = max(0.0, min(1.0, float(ber)))
  n_bits = max(1, int(frame_len_bytes) * 8)
  if b <= 0.0:
    return 0.0
  if b >= 1.0:
    return 1.0
  return max(0.0, min(1.0, -math.expm1(n_bits * math.log1p(-b))))


def compute_802154_lqi(sinr_db: float, rssi_dbm: float) -> int:
  """Maps IEEE 802.15.4 combined SINR (dB) and RSSI (dBm) to LQI in [0, 255]."""
  rssi_term = max(0.0, min(1.0, (rssi_dbm - (-100.0)) / 40.0))
  sinr_term = 1.0 / (1.0 + math.exp(-(sinr_db - 6.0) / 4.2))
  blended = 0.65 * sinr_term + 0.35 * rssi_term
  return max(0, min(255, int(round(255.0 * blended))))


def compute_openthread_link_quality(lqi: int, margin_db: float = 25.0) -> int:
  """Maps LQI and link margin (dB) to OpenThread LQ {0, 1, 2, 3}."""
  if margin_db >= 20.0 and lqi >= 180:
    return 3
  if margin_db >= 10.0 and lqi >= 110:
    return 2
  if margin_db >= 2.0 and lqi >= 45:
    return 1
  return 0


@dataclass
class RfNodeState:
  """Spatial coordinates and per-radio transmit power of a virtual node."""

  node_id: str
  x_m: float = 0.0
  y_m: float = 0.0
  z_m: float = 1.0
  tx_power_ble_dbm: float = 0.0
  tx_power_wifi_dbm: float = 16.0
  tx_power_thread_dbm: float = 8.0
  ble_channel: int = 37
  wifi_channel: int = 6
  thread_channel: int = 15
  walls: int = 0


@dataclass
class RfInterfererState:
  """Active 2.4 GHz co-channel / adjacent-channel interference source."""

  interferer_id: str
  protocol: str = 'wifi'
  channel: int = 11
  center_freq_mhz: float = 2462.0
  bandwidth_mhz: float = 20.0
  tx_power_dbm: float = 18.0
  duty_cycle: float = 0.65
  x_m: float = 2.0
  y_m: float = 1.0
  z_m: float = 1.0
  enabled: bool = True


@dataclass
class RfChannelConfig:
  """Global 2.4 GHz stochastic propagation and fading configuration."""

  enabled: bool = False
  regime_name: str = 'ideal'
  reference_distance_m: float = 1.0
  min_distance_m: float = 0.25
  path_loss_exp: float = 2.2
  wall_loss_db: float = 6.0
  default_walls: int = 0
  default_distance_m: float = 2.0
  shadowing_sigma_db: float = 1.5
  shadowing_correlation_rho: float = 0.82
  rician_k_db: float = 10.0
  fading_mode: str = 'rician'  # 'none', 'rician', 'rayleigh'
  noise_floor_dbm: float = -95.0
  sensitivity_ble_dbm: float = -96.0
  sensitivity_wifi_dbm: float = -88.0
  sensitivity_thread_dbm: float = -100.0
  max_per_cap: float = 0.45
  ble_channel: int = 38
  wifi_channel: int = 6
  thread_channel: int = 15
  seed: Optional[int] = None


@dataclass
class RfSampleResult:
  """Instantaneous evaluation of a single packet transmission."""

  protocol: str
  src_id: str
  dst_id: str
  channel: int
  center_freq_mhz: float
  distance_m: float
  path_loss_db: float
  shadowing_db: float
  multipath_fading_db: float
  rssi_dbm: int
  noise_floor_dbm: float
  interference_dbm: float
  sinr_db: float
  ber: float
  per: float
  lqi: int
  link_quality: int
  dropped: bool
  timestamp: float


@dataclass
class RfProtocolTelemetry:
  """Aggregated telemetry for a single radio protocol (BLE/Wi-Fi/Thread)."""

  protocol: str
  tx_packets: int = 0
  rx_packets: int = 0
  dropped_packets: int = 0
  retransmissions_estimated: int = 0
  last_rssi_dbm: int = -45
  min_rssi_dbm: Optional[int] = None
  max_rssi_dbm: Optional[int] = None
  sum_rssi_dbm: float = 0.0
  sum_sq_rssi_dbm: float = 0.0
  sum_sinr_db: float = 0.0
  sum_lqi: float = 0.0
  sum_per: float = 0.0
  sample_count: int = 0
  last_sinr_db: float = 35.0
  last_lqi: int = 255
  last_link_quality: int = 3
  last_per: float = 0.0
  last_shadowing_db: float = 0.0
  last_fading_db: float = 0.0
  last_interference_dbm: float = -120.0
  rssi_history: List[int] = field(default_factory=list)

  def record(self, sample: RfSampleResult) -> None:
    self.tx_packets += 1
    self.sample_count += 1
    if sample.dropped:
      self.dropped_packets += 1
      self.retransmissions_estimated += 1
    else:
      self.rx_packets += 1
    r = int(sample.rssi_dbm)
    self.last_rssi_dbm = r
    if self.min_rssi_dbm is None or r < self.min_rssi_dbm:
      self.min_rssi_dbm = r
    if self.max_rssi_dbm is None or r > self.max_rssi_dbm:
      self.max_rssi_dbm = r
    self.sum_rssi_dbm += float(r)
    self.sum_sq_rssi_dbm += float(r * r)
    self.sum_sinr_db += float(sample.sinr_db)
    self.sum_lqi += float(sample.lqi)
    self.sum_per += float(sample.per)
    self.last_sinr_db = round(sample.sinr_db, 2)
    self.last_lqi = int(sample.lqi)
    self.last_link_quality = int(sample.link_quality)
    self.last_per = round(sample.per, 5)
    self.last_shadowing_db = round(sample.shadowing_db, 2)
    self.last_fading_db = round(sample.multipath_fading_db, 2)
    self.last_interference_dbm = round(sample.interference_dbm, 2)
    self.rssi_history.append(r)
    if len(self.rssi_history) > 128:
      self.rssi_history = self.rssi_history[-128:]

  def to_dict(self) -> Dict[str, Any]:
    mean_rssi = (
        round(self.sum_rssi_dbm / self.sample_count, 2)
        if self.sample_count > 0
        else float(self.last_rssi_dbm)
    )
    mean_sinr = (
        round(self.sum_sinr_db / self.sample_count, 2)
        if self.sample_count > 0
        else float(self.last_sinr_db)
    )
    mean_lqi = (
        round(self.sum_lqi / self.sample_count, 1)
        if self.sample_count > 0
        else float(self.last_lqi)
    )
    mean_per = (
        round(self.sum_per / self.sample_count, 5)
        if self.sample_count > 0
        else float(self.last_per)
    )
    variance = 0.0
    if self.sample_count > 1:
      mean_val = self.sum_rssi_dbm / self.sample_count
      variance = max(
          0.0,
          (self.sum_sq_rssi_dbm / self.sample_count) - (mean_val * mean_val),
      )
    return {
        'protocol': self.protocol,
        'tx_packets': self.tx_packets,
        'rx_packets': self.rx_packets,
        'dropped_packets': self.dropped_packets,
        'retransmissions_estimated': self.retransmissions_estimated,
        'delivery_ratio': (
            round(self.rx_packets / self.tx_packets, 4)
            if self.tx_packets > 0
            else 1.0
        ),
        'last_rssi_dbm': self.last_rssi_dbm,
        'min_rssi_dbm': (
            self.min_rssi_dbm
            if self.min_rssi_dbm is not None
            else self.last_rssi_dbm
        ),
        'max_rssi_dbm': (
            self.max_rssi_dbm
            if self.max_rssi_dbm is not None
            else self.last_rssi_dbm
        ),
        'mean_rssi_dbm': mean_rssi,
        'std_rssi_db': round(math.sqrt(variance), 2),
        'sample_count': self.sample_count,
        'last_sinr_db': self.last_sinr_db,
        'mean_sinr_db': mean_sinr,
        'last_lqi': self.last_lqi,
        'mean_lqi': mean_lqi,
        'last_link_quality': self.last_link_quality,
        'last_per': self.last_per,
        'mean_per': mean_per,
        'last_shadowing_db': self.last_shadowing_db,
        'last_fading_db': self.last_fading_db,
        'last_interference_dbm': self.last_interference_dbm,
        'rssi_history': list(self.rssi_history[-32:]),
    }


class RfChannelSimulator:
  """Centralized 2.4 GHz Multi-Radio Stochastic Channel & Coexistence Engine."""

  _SHARED_INSTANCE: Optional['RfChannelSimulator'] = None
  _SHARED_LOCK = threading.RLock()

  @classmethod
  def get_shared(cls) -> 'RfChannelSimulator':
    with cls._SHARED_LOCK:
      if cls._SHARED_INSTANCE is None:
        cls._SHARED_INSTANCE = cls()
      return cls._SHARED_INSTANCE

  def __init__(
      self,
      config: Optional[RfChannelConfig] = None,
      *,
      seed: Optional[int] = None,
      thread_conf_path: str = DEFAULT_THREAD_RF_CONF_PATH,
  ):
    self._lock = threading.RLock()
    self.config = config or RfChannelConfig()
    if seed is not None:
      self.config.seed = seed
    self.thread_conf_path = thread_conf_path
    self._rng = random.Random(self.config.seed)
    self._nodes: Dict[str, RfNodeState] = {}
    self._interferers: Dict[str, RfInterfererState] = {}
    self._shadowing_state: Dict[Tuple[str, str, str], float] = {}
    self._link_overrides: Dict[Tuple[str, str, str], Dict[str, float]] = {}
    self._recent_transmissions: List[Tuple[float, str, int, float, float]] = []
    self._telemetry: Dict[str, RfProtocolTelemetry] = {
        'ble': RfProtocolTelemetry(protocol='ble', last_rssi_dbm=-45),
        'wifi': RfProtocolTelemetry(protocol='wifi', last_rssi_dbm=-42),
        'thread': RfProtocolTelemetry(protocol='thread', last_rssi_dbm=-44),
    }
    self._link_telemetry: Dict[str, RfProtocolTelemetry] = {}
    self._regime_history: List[Dict[str, Any]] = []

  def set_seed(self, seed: Optional[int]) -> None:
    with self._lock:
      self.config.seed = seed
      self._rng = random.Random(seed)

  def set_node_position(
      self,
      node_id: str,
      x_m: float,
      y_m: float,
      z_m: float = 1.0,
      *,
      walls: Optional[int] = None,
      tx_power_ble_dbm: Optional[float] = None,
      tx_power_wifi_dbm: Optional[float] = None,
      tx_power_thread_dbm: Optional[float] = None,
  ) -> RfNodeState:
    with self._lock:
      key = str(node_id).strip()
      node = self._nodes.get(key)
      if node is None:
        node = RfNodeState(node_id=key)
        self._nodes[key] = node
      node.x_m = float(x_m)
      node.y_m = float(y_m)
      node.z_m = float(z_m)
      if walls is not None:
        node.walls = max(0, int(walls))
      if tx_power_ble_dbm is not None:
        node.tx_power_ble_dbm = float(tx_power_ble_dbm)
      if tx_power_wifi_dbm is not None:
        node.tx_power_wifi_dbm = float(tx_power_wifi_dbm)
      if tx_power_thread_dbm is not None:
        node.tx_power_thread_dbm = float(tx_power_thread_dbm)
      self.sync_thread_config_file()
      return node

  def add_interferer(
      self,
      interferer_id: str,
      *,
      protocol: str = 'wifi',
      channel: int = 11,
      tx_power_dbm: float = 18.0,
      duty_cycle: float = 0.65,
      x_m: float = 2.0,
      y_m: float = 1.0,
      z_m: float = 1.0,
      enabled: bool = True,
  ) -> RfInterfererState:
    with self._lock:
      f_mhz, bw_mhz = protocol_channel_spectrum(protocol, channel)
      state = RfInterfererState(
          interferer_id=str(interferer_id),
          protocol=protocol.lower(),
          channel=int(channel),
          center_freq_mhz=f_mhz,
          bandwidth_mhz=bw_mhz,
          tx_power_dbm=float(tx_power_dbm),
          duty_cycle=max(0.0, min(1.0, float(duty_cycle))),
          x_m=float(x_m),
          y_m=float(y_m),
          z_m=float(z_m),
          enabled=bool(enabled),
      )
      self._interferers[state.interferer_id] = state
      self.sync_thread_config_file()
      return state

  def clear_interferers(self) -> None:
    with self._lock:
      self._interferers.clear()
      self.sync_thread_config_file()

  def apply_regime_preset(self, preset: str) -> Dict[str, Any]:
    """Applies a named physical propagation & coexistence regime preset.

    Supported presets:
      - 'ideal' / 'disabled': Disables stochastic drops and returns to clean
      defaults.
      - 'regime_a_near_los' (Near-Field LoS, d=1.5m, n=2.0, K=12dB,
      sigma=0.8dB):
        High RSSI (~ -42 to -48 dBm), LQI >= 230, ~0% packet loss.
      - 'regime_b_far_nlos_fading' (Obstructed Far-Field NLOS + Shadowing +
        Rayleigh Fading, d=16m, n=3.0, walls=2, K=0, sigma=4.5dB):
        Degraded RSSI (~ -76 to -88 dBm), high RSSI variance, reduced LQI
        (~ 110..175), and ~8-20% packet error rate inducing MAC-layer and upper-layer retries.
      - 'regime_c_cochannel_interference' (2.4 GHz Co-Channel Wi-Fi/BLE/Thread
        Interference Burst):
        Adds active co-channel interferers overlapping BLE Ch 38 (2426 MHz),
        Thread Ch 15 (2425 MHz), and Wi-Fi Ch 6 (2437 MHz), degrading SINR by
        >= 15 dB and spiking packet drops/retransmissions across all 3 radios.
    """
    p = preset.strip().lower()
    with self._lock:
      self.reset_metrics()
      self._nodes.clear()
      self._interferers.clear()
      self.config.ble_channel = 38
      self.config.wifi_channel = 6
      self.config.thread_channel = 15
      if p in ('ideal', 'disabled', 'off', 'clean'):
        self.config.enabled = False
        self.config.regime_name = 'ideal'
        self.config.default_distance_m = 1.5
        self.config.path_loss_exp = 2.0
        self.config.default_walls = 0
        self.config.shadowing_sigma_db = 0.0
        self.config.fading_mode = 'none'
        self.config.rician_k_db = 15.0
        self.config.noise_floor_dbm = -95.0
        self._interferers.clear()
      elif p in ('regime_a', 'regime_a_near_los', 'near_los', 'los'):
        self.config.enabled = True
        self.config.regime_name = 'regime_a_near_los'
        self.config.default_distance_m = 1.5
        self.config.path_loss_exp = 2.0
        self.config.default_walls = 0
        self.config.wall_loss_db = 5.0
        self.config.shadowing_sigma_db = 0.8
        self.config.shadowing_correlation_rho = 0.85
        self.config.fading_mode = 'rician'
        self.config.rician_k_db = 12.0
        self.config.noise_floor_dbm = -95.0
        self.config.max_per_cap = 0.05
        self._interferers.clear()
      elif p in (
          'regime_b',
          'regime_b_far_nlos_fading',
          'far_nlos',
          'nlos_fading',
      ):
        self.config.enabled = True
        self.config.regime_name = 'regime_b_far_nlos_fading'
        self.config.default_distance_m = 15.0
        self.config.path_loss_exp = 2.9
        self.config.default_walls = 2
        self.config.wall_loss_db = 5.5
        self.config.shadowing_sigma_db = 4.2
        self.config.shadowing_correlation_rho = 0.80
        self.config.fading_mode = 'rayleigh'
        self.config.rician_k_db = 0.0
        self.config.noise_floor_dbm = -92.0
        self.config.max_per_cap = 0.28
        self._interferers.clear()
      elif p in (
          'regime_c',
          'regime_c_cochannel_interference',
          'cochannel_interference',
          'interference',
      ):
        self.config.enabled = True
        self.config.regime_name = 'regime_c_cochannel_interference'
        self.config.default_distance_m = 15.0
        self.config.path_loss_exp = 2.9
        self.config.default_walls = 2
        self.config.wall_loss_db = 5.5
        self.config.shadowing_sigma_db = 4.2
        self.config.shadowing_correlation_rho = 0.80
        self.config.fading_mode = 'rayleigh'
        self.config.rician_k_db = 0.0
        self.config.noise_floor_dbm = -88.0
        self.config.max_per_cap = 0.38
        self.config.ble_channel = 38
        self.config.wifi_channel = 6
        self.config.thread_channel = 15
        self._interferers.clear()
        # Co-channel Wi-Fi Ch 4 (2427 MHz, 20 MHz -> [2417, 2437]) overlaps
        # Thread Ch 15 (2425 MHz), BLE Ch 38 (2426 MHz), and Wi-Fi Ch 6.
        self.add_interferer(
            'wifi_cochannel_ch4',
            protocol='wifi',
            channel=4,
            tx_power_dbm=20.0,
            duty_cycle=0.75,
            x_m=1.5,
            y_m=0.5,
            z_m=1.0,
        )
        # Co-channel Wi-Fi Ch 6 (2437 MHz) + Ch 11 (2462 MHz)
        self.add_interferer(
            'wifi_cochannel_ch6',
            protocol='wifi',
            channel=6,
            tx_power_dbm=20.0,
            duty_cycle=0.75,
            x_m=1.5,
            y_m=0.5,
            z_m=1.0,
        )
        # High-duty BLE advertising/data jammer on Ch 38 (2426 MHz).
        self.add_interferer(
            'ble_thread_adj_ch38',
            protocol='ble',
            channel=38,
            tx_power_dbm=10.0,
            duty_cycle=0.60,
            x_m=1.2,
            y_m=0.5,
            z_m=1.0,
        )
      elif p in (
          'regime_d',
          'regime_d_cell_edge_sensitivity',
          'cell_edge',
          'sensitivity_edge',
      ):
        self.config.enabled = True
        self.config.regime_name = 'regime_d_cell_edge_sensitivity'
        self.config.default_distance_m = 25.0
        self.config.path_loss_exp = 3.2
        self.config.default_walls = 3
        self.config.wall_loss_db = 5.5
        self.config.shadowing_sigma_db = 2.0
        self.config.shadowing_correlation_rho = 0.85
        self.config.fading_mode = 'rayleigh'
        self.config.rician_k_db = 0.0
        self.config.noise_floor_dbm = -95.0
        self.config.max_per_cap = 0.45
        self.config.ble_channel = 38
        self.config.wifi_channel = 6
        self.config.thread_channel = 15
        self._interferers.clear()
      elif p in (
          'regime_e',
          'regime_e_asymmetric_hidden_node',
          'asymmetric',
          'hidden_node',
      ):
        self.config.enabled = True
        self.config.regime_name = 'regime_e_asymmetric_hidden_node'
        self.config.default_distance_m = 14.0
        self.config.path_loss_exp = 2.8
        self.config.default_walls = 1
        self.config.wall_loss_db = 5.0
        self.config.shadowing_sigma_db = 2.5
        self.config.shadowing_correlation_rho = 0.82
        self.config.fading_mode = 'rician'
        self.config.rician_k_db = 4.0
        self.config.noise_floor_dbm = -94.0
        self.config.max_per_cap = 0.35
        self.config.ble_channel = 38
        self.config.wifi_channel = 6
        self.config.thread_channel = 15
        self._interferers.clear()
        # High-power Border Router (+20 dBm) at origin vs. 0 dBm coin-cell SED
        # at 14m, with a hidden-node Wi-Fi Ch 4 interferer located 0.6m from
        # the Border Router receiver (drowning SED -> BR uplink).
        self.set_node_position(
            'thread_node_1',
            0.0,
            0.0,
            1.0,
            walls=1,
            tx_power_ble_dbm=9.0,
            tx_power_wifi_dbm=20.0,
            tx_power_thread_dbm=20.0,
        )
        self.set_node_position(
            'thread_air',
            14.0,
            0.0,
            1.0,
            walls=1,
            tx_power_ble_dbm=-4.0,
            tx_power_wifi_dbm=10.0,
            tx_power_thread_dbm=0.0,
        )
        self.add_interferer(
            'hidden_node_rx_jammer',
            protocol='wifi',
            channel=4,
            tx_power_dbm=20.0,
            duty_cycle=0.80,
            x_m=0.6,
            y_m=0.0,
            z_m=1.0,
        )
      elif p in (
          'regime_f',
          'regime_f_bursty_body_shadowing',
          'body_shadowing',
          'bursty_shadowing',
      ):
        self.config.enabled = True
        self.config.regime_name = 'regime_f_bursty_body_shadowing'
        self.config.default_distance_m = 12.0
        self.config.path_loss_exp = 2.8
        self.config.default_walls = 2
        self.config.wall_loss_db = 5.5
        self.config.shadowing_sigma_db = 7.5
        self.config.shadowing_correlation_rho = 0.95
        self.config.fading_mode = 'rayleigh'
        self.config.rician_k_db = 0.0
        self.config.noise_floor_dbm = -92.0
        self.config.max_per_cap = 0.42
        self.config.ble_channel = 38
        self.config.wifi_channel = 6
        self.config.thread_channel = 15
        self._interferers.clear()
      elif p in (
          'regime_g',
          'regime_g_orthogonal_channel_agility',
          'orthogonal_channel',
          'channel_agility',
      ):
        self.config.enabled = True
        self.config.regime_name = 'regime_g_orthogonal_channel_agility'
        self.config.default_distance_m = 15.0
        self.config.path_loss_exp = 2.9
        self.config.default_walls = 2
        self.config.wall_loss_db = 5.5
        self.config.shadowing_sigma_db = 3.0
        self.config.shadowing_correlation_rho = 0.80
        self.config.fading_mode = 'rician'
        self.config.rician_k_db = 6.0
        self.config.noise_floor_dbm = -93.0
        self.config.max_per_cap = 0.25
        # Orthogonal upper channels: Thread Ch 25 (2475 MHz) and BLE Ch 39
        # (2480 MHz) completely evade heavy Wi-Fi Ch 1 (2412 MHz) + Ch 6
        # (2437 MHz) interference (spectral overlap alpha = 0.0).
        self.config.ble_channel = 39
        self.config.wifi_channel = 6
        self.config.thread_channel = 25
        self._interferers.clear()
        self.add_interferer(
            'wifi_heavy_ch1',
            protocol='wifi',
            channel=1,
            tx_power_dbm=20.0,
            duty_cycle=0.85,
            x_m=1.5,
            y_m=0.5,
            z_m=1.0,
        )
        self.add_interferer(
            'wifi_heavy_ch6',
            protocol='wifi',
            channel=6,
            tx_power_dbm=20.0,
            duty_cycle=0.85,
            x_m=1.5,
            y_m=0.5,
            z_m=1.0,
        )
      elif p in (
          'regime_h',
          'regime_h_microwave_broadband_jammer',
          'microwave_jammer',
          'broadband_jammer',
      ):
        self.config.enabled = True
        self.config.regime_name = 'regime_h_microwave_broadband_jammer'
        self.config.default_distance_m = 10.0
        self.config.path_loss_exp = 2.6
        self.config.default_walls = 1
        self.config.wall_loss_db = 5.0
        self.config.shadowing_sigma_db = 3.5
        self.config.shadowing_correlation_rho = 0.85
        self.config.fading_mode = 'rayleigh'
        self.config.rician_k_db = 0.0
        self.config.noise_floor_dbm = -90.0
        self.config.max_per_cap = 0.40
        # Thread Ch 19 (2445 MHz) sits directly inside the 2447 MHz (Ch 8,
        # 20 MHz BW -> [2437, 2457] MHz) residential microwave magnetron band.
        self.config.ble_channel = 38
        self.config.wifi_channel = 8
        self.config.thread_channel = 19
        self._interferers.clear()
        self.add_interferer(
            'microwave_magnetron_2447mhz',
            protocol='wifi',
            channel=8,
            tx_power_dbm=23.0,
            duty_cycle=0.50,
            x_m=2.0,
            y_m=0.5,
            z_m=1.2,
        )
      else:
        raise ValueError(f'Unknown RF regime preset: {preset}')

      self._regime_history.append({
          'preset': self.config.regime_name,
          'timestamp': time.time(),
      })
      self.sync_thread_config_file()
      return self.get_status()

  def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Updates RF simulator parameters, nodes, or interferers from a dict."""
    with self._lock:
      if 'preset' in updates and updates['preset']:
        self.apply_regime_preset(str(updates['preset']))
      for field_name in (
          'enabled',
          'regime_name',
          'reference_distance_m',
          'min_distance_m',
          'path_loss_exp',
          'wall_loss_db',
          'default_walls',
          'default_distance_m',
          'shadowing_sigma_db',
          'shadowing_correlation_rho',
          'rician_k_db',
          'fading_mode',
          'noise_floor_dbm',
          'sensitivity_ble_dbm',
          'sensitivity_wifi_dbm',
          'sensitivity_thread_dbm',
          'max_per_cap',
          'ble_channel',
          'wifi_channel',
          'thread_channel',
      ):
        if field_name in updates and updates[field_name] is not None:
          cur_val = getattr(self.config, field_name)
          if isinstance(cur_val, bool):
            setattr(self.config, field_name, bool(updates[field_name]))
          elif isinstance(cur_val, int):
            setattr(self.config, field_name, int(updates[field_name]))
          elif isinstance(cur_val, float):
            setattr(self.config, field_name, float(updates[field_name]))
          else:
            setattr(self.config, field_name, str(updates[field_name]))
      if 'seed' in updates:
        self.set_seed(
            int(updates['seed']) if updates['seed'] is not None else None
        )
      if 'nodes' in updates and isinstance(updates['nodes'], dict):
        for nid, npayload in updates['nodes'].items():
          if isinstance(npayload, dict):
            self.set_node_position(
                nid,
                float(npayload.get('x_m', 0.0)),
                float(npayload.get('y_m', 0.0)),
                float(npayload.get('z_m', 1.0)),
                walls=npayload.get('walls'),
                tx_power_ble_dbm=npayload.get('tx_power_ble_dbm'),
                tx_power_wifi_dbm=npayload.get('tx_power_wifi_dbm'),
                tx_power_thread_dbm=npayload.get('tx_power_thread_dbm'),
            )
      if 'clear_interferers' in updates and updates['clear_interferers']:
        self._interferers.clear()
      if 'interferers' in updates and isinstance(updates['interferers'], list):
        for item in updates['interferers']:
          if isinstance(item, dict):
            self.add_interferer(
                str(
                    item.get('interferer_id', f'intf_{len(self._interferers)}')
                ),
                protocol=str(item.get('protocol', 'wifi')),
                channel=int(item.get('channel', 11)),
                tx_power_dbm=float(item.get('tx_power_dbm', 18.0)),
                duty_cycle=float(item.get('duty_cycle', 0.65)),
                x_m=float(item.get('x_m', 2.0)),
                y_m=float(item.get('y_m', 1.0)),
                z_m=float(item.get('z_m', 1.0)),
                enabled=bool(item.get('enabled', True)),
            )
      self.sync_thread_config_file()
      return self.get_status()

  def reset_metrics(self) -> None:
    """Resets packet and RSSI telemetry counters across all protocols."""
    with self._lock:
      self._telemetry = {
          'ble': RfProtocolTelemetry(protocol='ble', last_rssi_dbm=-45),
          'wifi': RfProtocolTelemetry(protocol='wifi', last_rssi_dbm=-42),
          'thread': RfProtocolTelemetry(protocol='thread', last_rssi_dbm=-44),
      }
      self._link_telemetry.clear()
      self._recent_transmissions.clear()
      self._shadowing_state.clear()
      for path in glob.glob(DEFAULT_THREAD_RF_STATS_GLOB):
        try:
          os.remove(path)
        except OSError as exc:
          _ = exc

  def _distance_and_walls(self, src_id: str, dst_id: str) -> Tuple[float, int]:
    n_src = self._nodes.get(src_id)
    n_dst = self._nodes.get(dst_id)
    if n_src is not None and n_dst is not None:
      dx = n_src.x_m - n_dst.x_m
      dy = n_src.y_m - n_dst.y_m
      dz = n_src.z_m - n_dst.z_m
      dist = math.sqrt(dx * dx + dy * dy + dz * dz)
      walls = max(n_src.walls, n_dst.walls, self.config.default_walls)
      return max(self.config.min_distance_m, dist), walls
    return (
        max(self.config.min_distance_m, self.config.default_distance_m),
        self.config.default_walls,
    )

  def _tx_power_dbm(self, protocol: str, src_id: str) -> float:
    node = self._nodes.get(src_id)
    proto = protocol.lower()
    if node is not None:
      if proto == 'ble':
        return node.tx_power_ble_dbm
      if proto == 'wifi':
        return node.tx_power_wifi_dbm
      return node.tx_power_thread_dbm
    if proto == 'ble':
      return 4.0
    if proto == 'wifi':
      return 16.0
    return 8.0

  def _sample_shadowing_db(
      self, protocol: str, src_id: str, dst_id: str
  ) -> float:
    sigma = max(0.0, self.config.shadowing_sigma_db)
    if sigma <= 1e-6:
      return 0.0
    a, b = sorted((str(src_id), str(dst_id)))
    key = (protocol.lower(), a, b)
    prev = self._shadowing_state.get(key, 0.0)
    rho = max(0.0, min(0.999, self.config.shadowing_correlation_rho))
    innovation = (
        math.sqrt(max(0.0, 1.0 - rho * rho)) * sigma * self._rng.gauss(0.0, 1.0)
    )
    curr = max(-3.5 * sigma, min(3.5 * sigma, rho * prev + innovation))
    self._shadowing_state[key] = curr
    return curr

  def _sample_multipath_fading_db(self) -> float:
    mode = self.config.fading_mode.strip().lower()
    if mode in ('none', 'off', 'static'):
      return 0.0
    if mode == 'rayleigh':
      k_lin = 0.0
    else:
      k_lin = max(0.0, 10.0 ** (self.config.rician_k_db / 10.0))
    los_amp = math.sqrt(k_lin / (k_lin + 1.0))
    scatter_std = math.sqrt(1.0 / (2.0 * (k_lin + 1.0)))
    re = los_amp + scatter_std * self._rng.gauss(0.0, 1.0)
    im = scatter_std * self._rng.gauss(0.0, 1.0)
    power_lin = max(1e-3, re * re + im * im)
    fading_db = 10.0 * math.log10(power_lin)
    return max(-22.0, min(8.0, fading_db))

  def _record_transmission(
      self,
      now: float,
      proto: str,
      channel: int,
      tx_power_dbm: float,
      airtime_weight: float,
  ) -> None:
    """Appends one airtime record and drops records older than the window.

    Caller must hold self._lock.
    """
    cutoff = now - _COEX_WINDOW_S
    recent = self._recent_transmissions
    if recent and recent[0][0] < cutoff:
      self._recent_transmissions = recent = [
          t for t in recent if t[0] >= cutoff
      ]
    recent.append((now, proto, channel, tx_power_dbm, airtime_weight))

  def _compute_interference_mw(
      self,
      rx_protocol: str,
      rx_channel: int,
      dst_id: str,
      now: float,
  ) -> float:
    f_rx, bw_rx = protocol_channel_spectrum(rx_protocol, rx_channel)
    n_dst = self._nodes.get(dst_id)
    total_i_mw = 0.0

    # 1. Configured active interferers
    for intf in self._interferers.values():
      if not intf.enabled or intf.duty_cycle <= 0.0:
        continue
      alpha = spectral_overlap_factor(
          intf.center_freq_mhz, intf.bandwidth_mhz, f_rx, bw_rx
      )
      if alpha <= 0.0:
        continue
      if n_dst is not None:
        dx = intf.x_m - n_dst.x_m
        dy = intf.y_m - n_dst.y_m
        dz = intf.z_m - n_dst.z_m
        d_i = max(
            self.config.min_distance_m, math.sqrt(dx * dx + dy * dy + dz * dz)
        )
      else:
        d_i = 2.0
      pl_i = PL_D0_2P4GHZ_DB + 10.0 * 2.2 * math.log10(
          d_i / max(0.1, self.config.reference_distance_m)
      )
      p_rx_i_dbm = intf.tx_power_dbm - pl_i
      total_i_mw += alpha * intf.duty_cycle * (10.0 ** (p_rx_i_dbm / 10.0))

    # 2. Recent cross-protocol transmissions in the coexistence window
    cutoff = now - _COEX_WINDOW_S
    self._recent_transmissions = [
        t for t in self._recent_transmissions if t[0] >= cutoff
    ]
    seen_proto_ch = set()
    for _, tx_proto, tx_ch, tx_power_dbm, airtime_weight in reversed(
        self._recent_transmissions
    ):
      if tx_proto == rx_protocol.lower():
        continue
      if (tx_proto, tx_ch) in seen_proto_ch:
        continue
      seen_proto_ch.add((tx_proto, tx_ch))
      f_tx, bw_tx = protocol_channel_spectrum(tx_proto, tx_ch)
      alpha = spectral_overlap_factor(f_tx, bw_tx, f_rx, bw_rx)
      if alpha <= 0.0:
        continue
      pl_cross = PL_D0_2P4GHZ_DB + 10.0 * 2.2 * math.log10(2.5)
      p_rx_cross_dbm = tx_power_dbm - pl_cross
      total_i_mw += alpha * airtime_weight * (10.0 ** (p_rx_cross_dbm / 10.0))

    return total_i_mw

  def evaluate_packet(
      self,
      protocol: str,
      src_id: str,
      dst_id: str,
      *,
      channel: Optional[int] = None,
      frame_len_bytes: int = 64,
      frame_bytes: Optional[int] = None,
      record_telemetry: bool = True,
  ) -> RfSampleResult:
    """Evaluates instantaneous RSSI, SINR, LQI, and packet drop for a frame."""
    if frame_bytes is not None:
      frame_len_bytes = int(frame_bytes)
    now = time.time()
    proto = protocol.strip().lower()
    with self._lock:
      if channel is None:
        if proto == 'ble':
          channel = self.config.ble_channel
        elif proto == 'wifi':
          channel = self.config.wifi_channel
        else:
          channel = self.config.thread_channel

      f_mhz, _ = protocol_channel_spectrum(proto, channel)
      dist_m, walls = self._distance_and_walls(src_id, dst_id)
      tx_power_dbm = self._tx_power_dbm(proto, src_id)

      # Record airtime activity for cross-protocol coexistence. Only the
      # enabled model consumes it, and it is pruned to the coexistence window
      # on every append so it stays bounded on every code path.
      if self.config.enabled:
        airtime_weight = min(0.5, max(0.02, frame_len_bytes / 1500.0))
        self._record_transmission(
            now, proto, int(channel), tx_power_dbm, airtime_weight
        )

      if not self.config.enabled:
        default_rssi = (
            -45 if proto == 'ble' else (-42 if proto == 'wifi' else -44)
        )
        res = RfSampleResult(
            protocol=proto,
            src_id=str(src_id),
            dst_id=str(dst_id),
            channel=int(channel),
            center_freq_mhz=f_mhz,
            distance_m=dist_m,
            path_loss_db=40.05,
            shadowing_db=0.0,
            multipath_fading_db=0.0,
            rssi_dbm=default_rssi,
            noise_floor_dbm=self.config.noise_floor_dbm,
            interference_dbm=-120.0,
            sinr_db=default_rssi - self.config.noise_floor_dbm,
            ber=0.0,
            per=0.0,
            lqi=255,
            link_quality=3,
            dropped=False,
            timestamp=now,
        )
        if record_telemetry and proto in self._telemetry:
          self._telemetry[proto].record(res)
        return res

      d0 = max(0.1, self.config.reference_distance_m)
      path_loss_db = (
          PL_D0_2P4GHZ_DB
          + 10.0
          * self.config.path_loss_exp
          * math.log10(max(dist_m, self.config.min_distance_m) / d0)
          + float(walls) * self.config.wall_loss_db
      )
      shadowing_db = self._sample_shadowing_db(proto, src_id, dst_id)
      fading_db = self._sample_multipath_fading_db()
      p_rx_dbm = tx_power_dbm - path_loss_db + shadowing_db + fading_db
      rssi_int = max(-127, min(20, int(round(p_rx_dbm))))

      noise_mw = 10.0 ** (self.config.noise_floor_dbm / 10.0)
      interference_mw = self._compute_interference_mw(
          proto, int(channel), dst_id, now
      )
      interference_dbm = (
          10.0 * math.log10(interference_mw)
          if interference_mw > 1e-15
          else -120.0
      )
      total_noise_plus_i_dbm = 10.0 * math.log10(
          max(1e-15, noise_mw + interference_mw)
      )
      sinr_db = p_rx_dbm - total_noise_plus_i_dbm

      if proto == 'ble':
        ber = ber_ble_gfsk(sinr_db)
        sens_dbm = self.config.sensitivity_ble_dbm
      elif proto == 'wifi':
        ber = ber_wifi_ofdm(sinr_db)
        sens_dbm = self.config.sensitivity_wifi_dbm
      else:
        ber = ber_ieee802154_oqpsk(sinr_db)
        sens_dbm = self.config.sensitivity_thread_dbm

      raw_per = compute_per_from_ber(ber, frame_len_bytes)
      if p_rx_dbm < sens_dbm:
        per = 1.0
      else:
        per = min(self.config.max_per_cap, raw_per)

      lqi = compute_802154_lqi(sinr_db, p_rx_dbm)
      lq = compute_openthread_link_quality(lqi, p_rx_dbm - sens_dbm)
      dropped = self._rng.random() < per

      res = RfSampleResult(
          protocol=proto,
          src_id=str(src_id),
          dst_id=str(dst_id),
          channel=int(channel),
          center_freq_mhz=f_mhz,
          distance_m=round(dist_m, 3),
          path_loss_db=round(path_loss_db, 2),
          shadowing_db=round(shadowing_db, 2),
          multipath_fading_db=round(fading_db, 2),
          rssi_dbm=rssi_int,
          noise_floor_dbm=self.config.noise_floor_dbm,
          interference_dbm=round(interference_dbm, 2),
          sinr_db=round(sinr_db, 2),
          ber=ber,
          per=round(per, 5),
          lqi=lqi,
          link_quality=lq,
          dropped=dropped,
          timestamp=now,
      )
      if record_telemetry and proto in self._telemetry:
        self._telemetry[proto].record(res)
        link_key = f'{proto}:{src_id}->{dst_id}'
        if link_key not in self._link_telemetry:
          self._link_telemetry[link_key] = RfProtocolTelemetry(
              protocol=link_key
          )
        self._link_telemetry[link_key].record(res)
      return res

  def compute_thread_effective_params(self) -> Dict[str, Any]:
    """Computes effective Thread 802.15.4 link parameters for simulation."""
    with self._lock:
      ch = self.config.thread_channel
      f_rx, bw_rx = protocol_channel_spectrum('thread', ch)
      dist_m = max(self.config.min_distance_m, self.config.default_distance_m)
      d0 = max(0.1, self.config.reference_distance_m)
      walls = self.config.default_walls
      tx_power_dbm = 8.0
      path_loss_db = (
          PL_D0_2P4GHZ_DB
          + 10.0 * self.config.path_loss_exp * math.log10(dist_m / d0)
          + float(walls) * self.config.wall_loss_db
      )
      mean_rx_dbm = tx_power_dbm - path_loss_db
      noise_mw = 10.0 ** (self.config.noise_floor_dbm / 10.0)
      intf_mw = 0.0
      for intf in self._interferers.values():
        if not intf.enabled or intf.duty_cycle <= 0.0:
          continue
        alpha = spectral_overlap_factor(
            intf.center_freq_mhz, intf.bandwidth_mhz, f_rx, bw_rx
        )
        if alpha <= 0.0:
          continue
        pl_i = PL_D0_2P4GHZ_DB + 10.0 * 2.2 * math.log10(2.0)
        p_rx_i_dbm = intf.tx_power_dbm - pl_i
        intf_mw += alpha * intf.duty_cycle * (10.0 ** (p_rx_i_dbm / 10.0))

      interference_dbm = (
          10.0 * math.log10(intf_mw) if intf_mw > 1e-15 else -120.0
      )
      eff_noise_dbm = 10.0 * math.log10(max(1e-15, noise_mw + intf_mw))
      mean_sinr_db = mean_rx_dbm - eff_noise_dbm
      mean_lqi = compute_802154_lqi(mean_sinr_db, mean_rx_dbm)
      fading_code = 0
      if self.config.fading_mode.strip().lower() == 'rician':
        fading_code = 1
      elif self.config.fading_mode.strip().lower() == 'rayleigh':
        fading_code = 2
      return {
          'enabled': 1 if self.config.enabled else 0,
          'regime_name': self.config.regime_name,
          'channel': ch,
          'tx_power_dbm': round(tx_power_dbm, 2),
          'distance_m': round(dist_m, 2),
          'path_loss_exp': round(self.config.path_loss_exp, 2),
          'walls': walls,
          'wall_loss_db': round(self.config.wall_loss_db, 2),
          'path_loss_db': round(path_loss_db, 2),
          'mean_rx_dbm': round(mean_rx_dbm, 2),
          'shadowing_sigma_db': round(self.config.shadowing_sigma_db, 2),
          'shadowing_rho': round(self.config.shadowing_correlation_rho, 3),
          'fading_mode': fading_code,
          'rician_k_db': round(self.config.rician_k_db, 2),
          'noise_floor_dbm': round(self.config.noise_floor_dbm, 2),
          'interference_dbm': round(interference_dbm, 2),
          'effective_noise_dbm': round(eff_noise_dbm, 2),
          'mean_sinr_db': round(mean_sinr_db, 2),
          'mean_lqi': int(mean_lqi),
          'max_per_cap': round(self.config.max_per_cap, 4),
      }

  def sync_thread_config_file(self) -> str:
    """Atomically writes key=value RF state for Thread simulation."""
    with self._lock:
      params = self.compute_thread_effective_params()
      lines = [f'{k}={v}' for k, v in params.items()]
      payload = '\n'.join(lines) + '\n'
      conf_dir = os.path.dirname(self.thread_conf_path) or '/tmp'
      os.makedirs(conf_dir, exist_ok=True)
      fd, tmp_path = tempfile.mkstemp(prefix='.cirque_rf_', dir=conf_dir)
      try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
          f.write(payload)
        os.chmod(tmp_path, 0o666)
        os.replace(tmp_path, self.thread_conf_path)
      except OSError:
        try:
          os.unlink(tmp_path)
        except OSError as exc:
          _ = exc
      return self.thread_conf_path

  def _harvest_thread_c_telemetry(self) -> Dict[str, Any]:
    """Reads per-node stats written by OpenThread simulation radio.c."""
    nodes_stats: Dict[str, Any] = {}
    total_rx = 0
    total_tx = 0
    total_drops = 0
    rssi_values: List[int] = []
    lqi_values: List[int] = []
    sinr_values: List[float] = []
    for path in sorted(glob.glob(DEFAULT_THREAD_RF_STATS_GLOB)):
      try:
        with open(path, 'r', encoding='utf-8') as f:
          data = json.load(f)
        node_key = f"node_{data.get('node_id', os.path.basename(path))}"
        nodes_stats[node_key] = data
        total_rx += int(data.get('rx_packets', 0))
        total_tx += int(data.get('tx_packets', 0))
        total_drops += int(data.get('dropped_packets', 0))
        if 'last_rssi_dbm' in data:
          rssi_values.append(int(data['last_rssi_dbm']))
        if 'last_lqi' in data:
          lqi_values.append(int(data['last_lqi']))
        if 'last_sinr_db' in data:
          sinr_values.append(float(data['last_sinr_db']))
      except (OSError, ValueError, json.JSONDecodeError):
        continue
    return {
        'nodes': nodes_stats,
        'total_tx_packets': total_tx,
        'total_rx_packets': total_rx,
        'total_dropped_packets': total_drops,
        'mean_last_rssi_dbm': (
            round(sum(rssi_values) / len(rssi_values), 2)
            if rssi_values
            else None
        ),
        'mean_last_lqi': (
            round(sum(lqi_values) / len(lqi_values), 1) if lqi_values else None
        ),
        'mean_last_sinr_db': (
            round(sum(sinr_values) / len(sinr_values), 2)
            if sinr_values
            else None
        ),
    }

  def get_status(self) -> Dict[str, Any]:
    """Returns configuration, active interferers, and multi-radio telemetry."""
    with self._lock:
      thread_c_stats = self._harvest_thread_c_telemetry()
      thread_tel = self._telemetry['thread'].to_dict()
      if (
          thread_c_stats['total_rx_packets'] > 0
          or thread_c_stats['total_dropped_packets'] > 0
      ):
        thread_tel['c_radio_nodes'] = thread_c_stats['nodes']
        thread_tel['c_total_tx_packets'] = thread_c_stats['total_tx_packets']
        thread_tel['c_total_rx_packets'] = thread_c_stats['total_rx_packets']
        thread_tel['c_total_dropped_packets'] = thread_c_stats[
            'total_dropped_packets'
        ]
        if thread_c_stats['mean_last_rssi_dbm'] is not None:
          thread_tel['c_mean_last_rssi_dbm'] = thread_c_stats[
              'mean_last_rssi_dbm'
          ]
        if thread_c_stats['mean_last_lqi'] is not None:
          thread_tel['c_mean_last_lqi'] = thread_c_stats['mean_last_lqi']
        if thread_c_stats['mean_last_sinr_db'] is not None:
          thread_tel['c_mean_last_sinr_db'] = thread_c_stats[
              'mean_last_sinr_db'
          ]
      return {
          'config': asdict(self.config),
          'available_presets': [
              'ideal',
              'regime_a_near_los',
              'regime_b_far_nlos_fading',
              'regime_c_cochannel_interference',
              'regime_d_cell_edge_sensitivity',
              'regime_e_asymmetric_hidden_node',
              'regime_f_bursty_body_shadowing',
              'regime_g_orthogonal_channel_agility',
              'regime_h_microwave_broadband_jammer',
          ],
          'nodes': {k: asdict(v) for k, v in self._nodes.items()},
          'interferers': {k: asdict(v) for k, v in self._interferers.items()},
          'thread_effective_params': self.compute_thread_effective_params(),
          'telemetry': {
              'ble': self._telemetry['ble'].to_dict(),
              'wifi': self._telemetry['wifi'].to_dict(),
              'thread': thread_tel,
          },
          'regime_history': list(self._regime_history[-16:]),
      }
