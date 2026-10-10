# Cirque 2.4 GHz Multi-Radio RF Channel & Coexistence Simulation Design

> [!IMPORTANT]
> **Canonical Design Document**: This file mirrors [VIRTUAL_RF_CHANNEL_SIMULATION_DESIGN.md](./VIRTUAL_RF_CHANNEL_SIMULATION_DESIGN.md) for backward-compatible documentation links.
> - [Unified Virtual RF Architecture (`docs/VIRTUAL_RF_ARCHITECTURE.md`)](./VIRTUAL_RF_ARCHITECTURE.md)
> - [Virtual Bluetooth Design (`docs/VIRTUAL_BT_DESIGN.md`)](./VIRTUAL_BT_DESIGN.md)
> - [Virtual Wi-Fi Design (`docs/VIRTUAL_WIFI_DESIGN.md`)](./VIRTUAL_WIFI_DESIGN.md)

---

## 1. Overview & Motivation

By default, virtualized smart-home testbeds operate in an **idealized (`0%` loss, constant RSSI)** radio regime where every BLE advertisement, Wi-Fi L2 frame, and IEEE 802.15.4 Thread packet is delivered instantaneously with deterministic signal strength (`-38` to `-44 dBm`, `LQI = 255`, `PER = 0.0`). While effective for happy-path functional smoke tests, this ideal environment masks timing-sensitive protocol bugs, exchange-pool exhaustion, asymmetric link traps, and retransmission race conditions in upper-layer stacks such as Matter (`connectedhomeip`) and OpenThread.

`RfChannelSimulator` (`cirque/connectivity/rf_channel_simulator.py`) and `SpinelHdlcRfProxy` (`cirque/connectivity/threadsimpipe.py`) introduce a unified, literature-grounded **2.400–2.4835 GHz ISM Multi-Radio RF Channel & Coexistence Simulator** across all three virtual radios in Cirque—**Bluetooth Low Energy (BLE)**, **IEEE 802.11 (Wi-Fi)**, and **IEEE 802.15.4 (Thread)**—entirely within Cirque userspace and **with zero modifications to the OpenThread or Matter C++ codebases**.

---

## 2. Five-Stage 2.4 GHz Physical Layer & Coexistence Model

1. **Keenan–Motley Log-Distance Path Loss + Multi-Wall Attenuation**:
   $$PL(d) = PL(d_0) + 10 \, n \, \log_{10}\!\left(\frac{\max(d, d_{\min})}{d_0}\right) + \sum_{k} W_k$$
2. **Gudmundson `AR(1)` Temporally Correlated Log-Normal Shadowing**:
   $$X_{ij}(t) = \rho \, X_{ij}(t - \Delta t) + \sqrt{1 - \rho^2} \, \sigma_s \, \mathcal{N}(0, 1)$$
3. **Unit-Mean Normalized Rician (LoS, $K > 0$) and Rayleigh (NLoS, $K = 0$) Multipath Fading**:
   $$|h|^2 = \left(\sqrt{\frac{K}{K+1}} + \sqrt{\frac{1}{2(K+1)}} \, X\right)^{\!2} + \left(\sqrt{\frac{1}{2(K+1)}} \, Y\right)^{\!2}, \quad \mathbb{E}\!\left[|h|^2\right] = 1$$
4. **Thermal Noise Floor and Cross-Protocol 2.4 GHz Spectral Overlap SINR**:
   $$\text{SINR}_{\text{dB}} = P_{\text{rx,dBm}} - 10 \log_{10}\!\left(10^{N_{0,\text{dBm}}/10} + \sum_{k} \alpha_{ik} \, D_k \, I_{k,\text{mW}}\right)$$
5. **Modulation-Specific $\text{SINR} \to \text{BER} \to \text{PER}$ (`-expm1(8L * log1p(-BER))`) and IEEE 802.15.4 LQI (`0..255`) / OpenThread `LQ In` (`0..3`)**.

---

## 3. Nine Built-In Researcher RF Presets (`ideal` + `regime_a` .. `regime_h`)

- `ideal`: Default `0%` loss deterministic baseline (`enabled=False`).
- `regime_a_near_los`: Near-field Line-of-Sight (`d=1.5m`, $n=2.0$, Rician $K=12\text{ dB}$, $\sigma_s=0.8\text{ dB}$).
- `regime_b_far_nlos_fading`: Obstructed multi-room NLoS + Rayleigh fading (`d=15m`, $n=2.9$, `2` walls, $\sigma_s=4.2\text{ dB}$, $\rho=0.80$).
- `regime_c_cochannel_interference`: Heavy 2.4 GHz Wi-Fi Ch 4/6 + BLE Ch 38 co-channel interference overlapping Thread Ch 15.
- `regime_d_cell_edge_sensitivity`: Receiver sensitivity cliff (`d=25m`, $n=3.2$, `3` walls, thermal noise limited near `-95 dBm`).
- `regime_e_asymmetric_hidden_node`: Downlink `+20 dBm` vs. Uplink `0 dBm` SED with near-Border-Router Wi-Fi Ch 4 jammer (`0.6m` away).
- `regime_f_bursty_body_shadowing`: High temporal correlation ($\sigma_s=7.5\text{ dB}$, $\rho=0.95$) human-body blockage causing bursty deep fades.
- `regime_g_orthogonal_channel_agility`: BLE Ch 39 (`2480 MHz`) + Thread Ch 25 (`2475 MHz`) evading Wi-Fi Ch 1 + Ch 6 ($\alpha_{ik} = 0$).
- `regime_h_microwave_broadband_jammer`: Residential `2447 MHz` (`+23 dBm`, `50%` duty cycle) microwave magnetron jamming Thread Ch 19 (`2445 MHz`).

---

## 4. Protocol-Level Race Conditions Exposed by RF Presets

For Matter (`connectedhomeip`) protocol-level tests that exercise packet loss, see `docs/testing/cirque_rf_lossy_link.md` in the `connectedhomeip` repository.
