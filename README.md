# Cirque

## Introduction

Cirque simulates complex network topologies based upon docker nodes. On a single Linux machine, it can create multiple nodes with network stacks that are independent from each other. Some nodes may be connected to simulated Thread networks, others may connect to simulated BLE or WiFi. Cirque provides a service (gRPC or Flask REST) to create, destroy and manage multiple home environments with multiple virtual devices and radio capabilities between these devices.

## Installation:
### Prerequisites
```
sudo apt-get install bazel socat psmisc tigervnc-standalone-server tigervnc-viewer python3-pip python3-venv python3-setuptools
sudo pip3 install pycodestyle==2.5.0
```
### Make

```
make install
```

To install cirque without grpc support, just passing NO_GRPC environment variable to make:

```
make NO_GRPC=1 install
```

Note: You can consider running Cirque within a `virtualenv`

```
python3 -m venv venv
source venv/bin/activate
```

## Uninstallation:
```
make uninstall
```

## Features
- *Virtual Bluetooth (BLE & GATT)*: Provides a 100% kernel-module-free userspace
  Virtual Bluetooth H4/TCP medium (`cirque/virtual_bt/`) and container-local
  `org.bluez` D-Bus daemon + virtual `hci0` interface (`hciconfig`,
  `bluetoothctl`, `dbus-send`, `gdbus`), enabling multi-container BLE
  advertising, scanning, connection, GATT attribute read/write/notify, and
  Matter-over-BLE (`BTP`) commissioning without requiring `hci_vhci.ko` or host
  network mode (`use_legacy_btvirt=True` remains supported for legacy `btvirt`).

- *Virtual Wi-Fi*: Provides a 100% kernel-module-free userspace Virtual 802.11
  TCP medium + `fi.w1.wpa_supplicant1` D-Bus daemon + container-local `wlan0`
  TAP interface (`cirque/virtual_wifi/`), supporting 802.11 Beacon/Probe scan
  (`iwlist wlan0 scan`), WPA2-PSK 4-way handshake, `dhcpcd` IPv4/IPv6 leasing,
  and L2 Ethernet bridging between station containers only after association
  (`use_legacy_hwsim=True` remains supported for legacy `mac80211_hwsim`).

- *Thread*: Supports Thread protocol simulation via OpenThread RCP/NCP nodes and
  `socat` virtual serial pipes.

- *IPvlan*: Allows users to create multiple real devices and multiple virtual
  devices within the same private network.


## Quick Start: Interactive Virtual Home (Virtual Bluetooth + Virtual Wi-Fi)

You can spin up a 2-Docker + Virtual AP home (`MobileController` +
`IoTEndDevice`) with both Virtual Bluetooth (`hci0` + `org.bluez`) and Virtual
Wi-Fi (`wlan0` + `fi.w1.wpa_supplicant1`) without loading any host kernel
modules:

### 1. Start the Interactive Virtual Home (Terminal 1)
```bash
PYTHONPATH=. python3 examples/run_virtual_home_interactive.py
```
This builds `cirque-virtual-rf-node:latest` automatically if missing, starts the
Virtual Bluetooth and Virtual Wi-Fi mediums, registers a sample BLE GATT
Device Info Service (`0x180A`) + Battery Service (`0x180F`) + Advertisement on
`IoTEndDevice`, and keeps the containers running until you press `Ctrl+C`.

### 2. Run One-Shot Automated CLI Validation (Terminal 2)
```bash
./examples/validate_virtual_home.sh
```
This script automatically discovers the running `MobileController` and
`IoTEndDevice` containers and validates:
1. `hciconfig -a` on both containers (`hci0 UP RUNNING`).
2. `bluetoothctl` (`devices`, `info`, `gatt.list-attributes`,
   `gatt.select-attribute`, `gatt.read`).
3. `dbus-send` & `gdbus` against `org.bluez` (`GetManagedObjects` and
   `ReadValue`).
4. `iwlist wlan0 scan`, `AddNetwork`, `SelectNetwork`, `dhcpcd wlan0`, and
   `ping -I wlan0` between `10.0.1.11` and `10.0.1.12`.

### 3. Step Inside the Docker Containers Manually
```bash
# Find the container names printed by run_virtual_home_interactive.py
docker exec -it <MobileController_Container> bash

# Inside the container:
hciconfig -a
bluetoothctl show
bluetoothctl scan on
bluetoothctl devices
bluetoothctl connect DC:A6:32:AA:BB:02
bluetoothctl gatt.list-attributes
CHAR_PATH=/org/bluez/hci0/dev_DC_A6_32_AA_BB_02/service0001/char0004
bluetoothctl gatt.select-attribute "$CHAR_PATH"
bluetoothctl gatt.read

# Validate Virtual Wi-Fi inside the container:
iwlist wlan0 scan
gdbus call --system --dest fi.w1.wpa_supplicant1 \
  --object-path /fi/w1/wpa_supplicant1/Interfaces/0 \
  --method org.freedesktop.DBus.Properties.Get \
  fi.w1.wpa_supplicant1.Interface State
ping -I wlan0 -c 4 10.0.1.12
```


## Test:
The below runs the unit test suite (including >=95% line coverage gates for
`cirque/virtual_bt` and `cirque/virtual_wifi`), the 2-Docker Virtual Home BLE +
Wi-Fi E2E test (`examples/test_virtual_home_ble_wifi_e2e.py`), and the
Flask/gRPC integration tests:

```bash
sh run_tests.sh
```

To run only the kernel-module-free Virtual Bluetooth & Virtual Wi-Fi unit and
2-Docker E2E tests directly:

```bash
PYTHONPATH=. python3 -m unittest -v \
  cirque/virtual_wifi/test_virtual_wifi_server.py
PYTHONPATH=. python3 -m unittest -v \
  cirque/capabilities/test/test_bluetooth_capability.py
PYTHONPATH=. python3 -m unittest -v \
  cirque/capabilities/test/test_wifi_capability.py
PYTHONPATH=. python3 -m unittest -v \
  examples/test_virtual_home_ble_wifi_e2e.py
```


# Directory Structure

The Cirque repository is structured as follows:

| File / Folder | Contents |
|----|----|
| `.github/workflows/main.yml` | GitHub Actions CI workflow (coverage + E2E). |
| `ARCHITECTURE.md` | High-level Cirque architecture overview. |
| `docs/VIRTUAL_RF_ARCHITECTURE.md` | Unified Virtual RF architecture. |
| `docs/VIRTUAL_BT_DESIGN.md` | Virtual Bluetooth (`cirque/virtual_bt/`) doc. |
| `docs/VIRTUAL_WIFI_DESIGN.md` | Virtual Wi-Fi (`cirque/virtual_wifi/`) doc. |
| `cirque/` | Core implementation of Cirque. |
| `cirque/capabilities/` | Node capabilities (`BlueTooth`, `WiFi`, `Thread`). |
| `cirque/common/` | Cirque utility folder (logging, exceptions, helpers). |
| `cirque/connectivity/` | Connectivity handling (`HomeLan`, `SocatPipe`). |
| `cirque/grpc/` | gRPC service. |
| `cirque/home/` | Virtual home orchestration (`CirqueHome`). |
| `cirque/nodes/` | Docker node implementations (`DockerNode`, `WiFiAPNode`). |
| `cirque/proto/` | Cirque gRPC proto files. |
| `cirque/resources/` | Reference generic, Wi-Fi AP, and Virtual RF images. |
| `cirque/restservice/` | Cirque Flask REST service. |
| `cirque/virtual_bt/` | Userspace Virtual BT medium, `org.bluez`, and `hci0`. |
| `cirque/virtual_wifi/` | Userspace Virtual Wi-Fi medium and `wlan0` bridge. |
| `dependency_modules.sh` | Script to prepare test docker nodes and emulator. |
| `examples/` | Integration examples and interactive Virtual Home scripts. |
| `LICENSE` | Cirque license file (Apache 2.0). |
| `Makefile` | Build, install, and style-check targets. |
| `requirements.txt` | Python pip requirement file. |
| `utils/` | Cirque build utilities. |
| `contributing.md` | Guidelines for contributing to Cirque. |
| `setup.py` | Build script for setuptools. |
| `README.md` | This file. |
| `run_tests.sh` | Cirque unit and integration test script. |
| `version` | Release version tag. |
