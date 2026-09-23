# Cirque

## Architecture

Cirque creates a centralized service, that orchestrates different kinds of virtual connected IOT device in a virtual home and provides a set of APIs to service requests posted to those virtual devices.  The virtual devices are created out of Docker containers.  The Cirque service provides facilities to simulate both Thread and WiFi networks.

Cirque models the following terminology and abstractions:

- **Node** abstracts an individual device.  Nodes are built with Docker containers. The containers provide a packaging construct for libraries, configuration files, network interfaces, and processes that comprise the complete function of the node.  Containers also provide isolation of the function between the different nodes.
- **Capability** corresponds to the library constructs provided by default by Cirque service.  Capabilities expose access to different networking technologies (e.g. Thread and WiFi link layer abstractions or direct access to LAN), persistent storage, virtual display (via XVNC), configuration management and many others.
- **Home** is a set of nodes each of which represents a device at home. Many of those nodes correspond to the embedded IOT devices, while other nodes may emulate the mobile phones, WiFi routers and access points, or Thread routers.
- **Service** is the instance of Cirque.  The service manages one or more virtual homes.  The Cirque service provides APIs to create, start, stop, delete, list, query and generally manage homes and nodes. Aside from the generic CRUD and management facilities, the service provides a set of APIs to execute commands within any node.

## Implementation

### Docker management

For Docker management, Cirque utilizes the `docker-py` library to create, destroy and manage nodes running in Docker containers under Docker network.

### Service

Cirque provides a gRPC or Flask service to create, destroy and manage multiple homes with multiple nodes.  When the service receives a request to create a home or a node (virtual device), it assigns a locally unique ID to the object (`home_id` and `node_id` respectively).  The service keeps track of all its objects via a dictionary.  The `create` request creates a Docker container based on the requested object type.  The service then processes the requested capabilities to enable the nodes with the required functions.

### Capabilities

Cirque provides a set of capabilities available to any node.  Capability typically encapsulates a function that needs specialized support not only within the node (docker container) but also within the Cirque service and the host system.  Capabilities are implemented as Python objects, all inheriting from `BaseCapability`. The following capabilities are currently implemented within Cirque:
- *Thread Capability*: generic Thread network daemon check and configuration with IPv4/IPv6 setup
- *WiFi Capability*: dual-mode Wi-Fi capability supporting kernel-module-free
  userspace Virtual Wi-Fi (`cirque/virtual_wifi/`, `fi.w1.wpa_supplicant1` D-Bus
  + `wlan0` TAP L2 bridge) by default, as well as legacy `mac80211_hwsim` kernel
  simulation (`use_legacy_hwsim=True`).
- *Bluetooth Capability*: dual-mode BLE/GATT capability supporting
  kernel-module-free userspace Virtual Bluetooth (`cirque/virtual_bt/`,
  `org.bluez` D-Bus + virtual `hci0` interface + H4/TCP medium) by default, as
  well as legacy `btvirt`/`hci_vhci` simulation (`use_legacy_btvirt=True`).
- *Weave Capability*: generic Weave-enabled Docker node capability configuration with certificate path.
- *XVNC Capability*: allows Docker containers to forward its GUI to a remote client.
- *LAN Access Capability*: grants access to containers inside the Docker network via restricting iptable rules.
- *Interactive Capability*: enable/disable interactive shell for Docker node via toggle stdin_open
- *Mount capability*: allows users to mount arbitrary path to Docker
- *Traffic Control Capability*: make `tc` command available (adds `NET_ADMIN` caps to the device), also supports enable traffic control config on default interface (`eth0`) on start (requires `iproute2` package).

> For an in-depth architectural breakdown with sequence diagrams and frame
> formats, see:
> - [Unified Virtual RF Architecture](docs/VIRTUAL_RF_ARCHITECTURE.md)
> - [Virtual Bluetooth Design](docs/VIRTUAL_BT_DESIGN.md)
> - [Virtual Wi-Fi Design](docs/VIRTUAL_WIFI_DESIGN.md)

### Thread Simulation

For Thread radio simulation, Cirque utilizes the OpenThread network simulator.  When a Thread capability is enabled on a node, Cirque exposes a device in the node that behaves like a connection to a Thread chip.  The Thread device exposed in the node is implemented as a pipe to an OpenThread process running in the host namespace; there is one OpenThread simulation process running in the host namespace for every Thread-enabled node. Each of the simulation processes exchanges 802.15.4 MAC frames with all other OpenThread processes using the loopback interface on the host.  Cirque provides facilities for exporting either a Thread NCP or RCP configurations into the node.

### Bluetooth (BLE & GATT) Simulation

For Bluetooth Low Energy (BLE) and GATT simulation, `BlueToothCapability`
provides a dual-mode architecture:
- **Userspace Virtual Bluetooth (Default, Kernel-Module-Free)**: Runs a
  `VirtualBluetoothServer` (`cirque/virtual_bt/`) over TCP alongside a
  per-container `org.bluez` system D-Bus daemon (`Adapter1`, `Device1`,
  `GattService1`, `GattCharacteristic1`, `GattDescriptor1`, `GattManager1`,
  `LEAdvertisingManager1`), a container-local virtual `hci0` netdevice
  (`hciconfig`), and an interactive `bluetoothctl` CLI wrapper. Each container
  remains in its own isolated Docker network namespace while exchanging H4 HCI
  Advertising, Connection, L2CAP, and ATT/GATT PDUs across the shared virtual RF
  medium.
- **Legacy `btvirt` Mode (`use_legacy_btvirt=True`)**: Uses BlueZ `btvirt` and
  the `hci_vhci` Linux kernel module with host network namespace sharing.

### WiFi Simulation

For Wi-Fi radio simulation, `WiFiCapability` provides a dual-mode architecture:
- **Userspace Virtual Wi-Fi (Default, Kernel-Module-Free)**: Runs a
  `VirtualWiFiServer` (`cirque/virtual_wifi/`) over TCP alongside a
  per-container `fi.w1.wpa_supplicant1` system D-Bus daemon and a
  container-local `wlan0` TAP interface. Containers can scan virtual APs
  (`iwlist wlan0 scan` or D-Bus `Scan`), authenticate via WPA2-PSK 4-way
  handshake (`AddNetwork` / `SelectNetwork`), obtain IPv4/IPv6 addresses via
  `dhcpcd wlan0`, and exchange L2 Ethernet frames across the virtual AP bridge
  only while associated.
- **Legacy `mac80211_hwsim` Mode (`use_legacy_hwsim=True`)**: Utilizes the Linux
  kernel module `mac80211_hwsim` to simulate Wi-Fi communication at the kernel
  MAC level across `hostapd` AP containers and `wpa_supplicant` station
  containers. For example, Cirque can be used to:

- create a home
- create, say, five nodes with WiFi capabilities.
- two nodes created above each encapsulate a WiFi access point.  This is accomplished via a specialized container exporting an AP function using `hostapd`. Each of those nodes exports a programmatically assigned SSID and PSK, and, when appropriate, exports other functions provided by either a bare access point (Ethernet bridging) or a more complete home router (WAN connectivity, DHCP, NAT).
- three remaining nodes act as WiFi stations.  When they scan the available WiFi networks, they will discover the two SSIDs we've created above.  As these three nodes are being provisioned, they can use the standard WiFi interactions to join a particular network and be provisioned with the correct networking configuration.

### Traffic Control

With *Traffic Control* capability enabled, and `iproute2` package installed in the docker image for device you can use `tc` command to simulate a bad network environment (high latency, packet loss, etc.). You can easily setup latency and packet loss rate on default interface `eth0` in the container by specify "latencyMs" (millisecond) and "loss" (percent) for Traffic Control capability.

### IPVlan feature
With *IpVlan feature*, we provide a way for user to be able to have mutiple real devices and mutiple virtual devices in the same private network, so user could do all kinds of tests in hybrid mode. And see the whole setting as a home.

## CASE Study

### Scenario

Imagine one virtual home with multiple WiFi stations and WiFi router, WiFi stations needs to get IP address and chat with internet via WiFi router. Both WiFi router and WiFi station can be emulated inside Docker.

Test app would like to simulate WiFi setup and IP address request between WiFi station and WiFi access point via Cirque Service. In details:
- Test app instructs service to create Cirque virtual home
- Test app instructs service to create several devices with WiFi capability under virtual home (3 devices), they are 2 WiFi stations and 1 WiFi Access point.
- Test app instructs the process inside WiFi Stations Docker to achieve WiFi provisioning(scan + authentication+ associate + DHCP + ping)
- Test app instructs service to destroy Cirque virtual home and devices under virtual home.

### Instruction
Let’s do the Cirque test as below
```
sudo sh run_tests.sh
```
Note:
It basically covers the below
Build WiFi access point and WiFi station Docker images
```
sh dependency_modules.sh
```
Bring up Cirque Flask or gRPC service:
```bash
# Flask:
FLASK_APP=cirque/restservice/service.py python3 -m flask run
# gRPC:
python3 -m cirque.grpc.service
```
Run example tests (Virtual Home BLE+Wi-Fi / Flask / gRPC):
```bash
PYTHONPATH=. python3 -m unittest -v examples/test_virtual_home_ble_wifi_e2e.py
python3 examples/test_flask_virtual_home.py
python3 examples/test_grpc_virtual_home.py
```
