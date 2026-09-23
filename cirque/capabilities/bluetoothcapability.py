# Copyright 2021 Google LLC
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
import os
from os.path import abspath, dirname
import time
from cirque.capabilities.basecapability import BaseCapability
from cirque.common.cirquelog import CirqueLog
import cirque.common.utils as utils
from cirque.virtual_bt.bluez_dbus_daemon import BluezDbusVirtualService
from cirque.virtual_bt.docker_hci_bridge import DockerVirtualBtManager
from cirque.virtual_bt.server import VirtualBluetoothServer

CIRQUE_ROOT = dirname(dirname(dirname(abspath(__file__))))
BLUEZ_DIR = os.path.join(CIRQUE_ROOT, 'bluez')


class BlueToothCapability(BaseCapability):
  """Cirque Bluetooth Capability with dual-mode Virtual BT (TCP/D-Bus) and legacy btvirt support.

  Architecture Overview:
    1. Default Mode (`use_virtual_bt_tcp=True`):
       Uses the kernel-module-free `cirque.virtual_bt` subsystem:
       - `VirtualBluetoothServer`: Host TCP server emulating HCI Command/Event,
         ACL/GATT L2CAP, and Virtual 2.4 GHz BLE advertising/scanning medium.
       - `BluezDbusVirtualService`: Per-container `org.bluez` D-Bus service
         (`Adapter1`, `Device1`, `GattManager1`, `GattService1`,
         `GattCharacteristic1`, `LEAdvertisingManager1`) attached to each
         container's isolated `/run/dbus/system_bus_socket`.
       - Preserves standard Docker bridge networking (`wlan0`, `wpan0`, `eth0`
         namespace isolation) and works on unprivileged CI runners without
         `hci_vhci.ko`.

    2. Legacy Mode (`use_virtual_bt_tcp=False` or `CIRQUE_USE_LEGACY_BTVIRT=1`):
       Uses BlueZ `btvirt -L -l2` backed by the Linux kernel `hci_vhci` driver
       and a shared host `bluetoothd` daemon with `network_mode='host'`.
       Automatically falls back to `use_virtual_bt_tcp=True` if the `btvirt`
       binary or kernel module is unavailable on the host.
  """

  BLE_ADAPTS_LIST = list()
  _SHARED_VIRTUAL_SERVER = None
  _SHARED_DOCKER_MANAGER = None
  _SHARED_BLUEZ_DBUS = None

  @classmethod
  def get_or_start_virtual_server(
      cls, host='127.0.0.1', control_port=0, hci_port=0, phy_port=0
  ):
    """Starts or returns the singleton host-side VirtualBluetoothServer and D-Bus bridge."""
    if cls._SHARED_VIRTUAL_SERVER is None:
      cls._SHARED_VIRTUAL_SERVER = VirtualBluetoothServer(
          host=host,
          control_port=control_port,
          hci_port=hci_port,
          phy_port=phy_port,
      )
      cls._SHARED_VIRTUAL_SERVER.start()
      cls._SHARED_DOCKER_MANAGER = DockerVirtualBtManager(
          host=host,
          control_port=cls._SHARED_VIRTUAL_SERVER.control_port,
          hci_port=cls._SHARED_VIRTUAL_SERVER.hci_port,
          phy_port=cls._SHARED_VIRTUAL_SERVER.phy_port,
      )
      cls._SHARED_BLUEZ_DBUS = BluezDbusVirtualService(
          cls._SHARED_DOCKER_MANAGER
      )
      cls._SHARED_BLUEZ_DBUS.start()
    return cls._SHARED_VIRTUAL_SERVER

  @classmethod
  def stop_virtual_server(cls):
    """Stops the singleton VirtualBluetoothServer, Docker HCI manager, and BlueZ D-Bus router."""
    if cls._SHARED_BLUEZ_DBUS is not None:
      cls._SHARED_BLUEZ_DBUS.stop()
      cls._SHARED_BLUEZ_DBUS = None
    if cls._SHARED_DOCKER_MANAGER is not None:
      cls._SHARED_DOCKER_MANAGER.stop_all()
      cls._SHARED_DOCKER_MANAGER = None
    if cls._SHARED_VIRTUAL_SERVER is not None:
      cls._SHARED_VIRTUAL_SERVER.stop()
      cls._SHARED_VIRTUAL_SERVER = None

  def __init__(
      self,
      num_btvirts=2,
      use_virtual_bt_tcp=None,
      bd_addr=None,
      **port_kwargs,
  ):
    """Initializes a Bluetooth capability instance for a Cirque DockerNode."""
    control_port = port_kwargs.get('control_port', 0)
    hci_port = port_kwargs.get('hci_port', 0)
    phy_port = port_kwargs.get('phy_port', 0)
    self.num_btvirts = num_btvirts
    if use_virtual_bt_tcp is None:
      legacy_env = (
          os.environ.get('CIRQUE_USE_LEGACY_BTVIRT', '0').strip().lower()
      )
      use_virtual_bt_tcp = legacy_env not in ('1', 'true', 'yes')
    self.use_virtual_bt_tcp = bool(use_virtual_bt_tcp)
    self.logger = CirqueLog.get_cirque_logger(self.__class__.__name__)
    self.virtual_bt_host = '127.0.0.1'
    self.control_port = control_port
    self.hci_port = hci_port
    self.dedicated_hci_port = 0
    self.phy_port = phy_port
    self.bd_addr = bd_addr or ''
    self.ble_adapt = ''
    self.ble_adapt_id = 0
    self.pty_device_path = ''

    btvirt_bin = os.path.join(BLUEZ_DIR, 'emulator/btvirt')
    if self.use_virtual_bt_tcp or not os.path.exists(btvirt_bin):
      if not self.use_virtual_bt_tcp:
        self.logger.warning(
            'Legacy btvirt binary not found at %s; falling back to TCP'
            ' VirtualBT',
            btvirt_bin,
        )
      self.use_virtual_bt_tcp = True
      self.__setup_virtual_bt_controller()
    else:
      self.__run_bluetoothd()
      self.__run_btvirt_infs()
      self.__get_ble_controller()

  @property
  def name(self):
    return 'Bluetooth'

  @property
  def description(self):
    return {
        'ble_adapt': self.ble_adapt,
        'ble_adapt_id': self.ble_adapt_id,
        'bd_addr': self.bd_addr,
        'mode': 'virtual_bt_tcp' if self.use_virtual_bt_tcp else 'btvirt',
        'host': self.virtual_bt_host,
        'control_port': self.control_port,
        'hci_port': self.hci_port,
        'dedicated_hci_port': self.dedicated_hci_port,
        'phy_port': self.phy_port,
        'pty_device_path': self.pty_device_path,
        'hciconfig_path': (
            BlueToothCapability._SHARED_DOCKER_MANAGER.hciconfig_path
            if BlueToothCapability._SHARED_DOCKER_MANAGER
            else ''
        ),
    }

  def __setup_virtual_bt_controller(self):
    server = self.get_or_start_virtual_server(
        host=self.virtual_bt_host,
        control_port=self.control_port,
        hci_port=self.hci_port,
        phy_port=self.phy_port,
    )
    self.control_port = server.control_port
    self.hci_port = server.hci_port
    self.phy_port = server.phy_port

    idx = 0
    while f'hci{idx}' in BlueToothCapability.BLE_ADAPTS_LIST:
      idx += 1
    self.ble_adapt_id = idx
    self.ble_adapt = f'hci{idx}'
    BlueToothCapability.BLE_ADAPTS_LIST.append(self.ble_adapt)

    ctrl = server.create_controller(
        controller_id=self.ble_adapt,
        bd_addr=self.bd_addr if self.bd_addr else None,
        dedicated_port=True,
    )
    self.bd_addr = ctrl.state.bd_addr
    self.dedicated_hci_port = ctrl.dedicated_hci_port

    if BlueToothCapability._SHARED_DOCKER_MANAGER is not None:
      pty_path, _ = (
          BlueToothCapability._SHARED_DOCKER_MANAGER.register_controller(
              controller_id=self.ble_adapt,
              bd_addr=self.bd_addr,
              dedicated_hci_port=self.dedicated_hci_port,
          )
      )
      self.pty_device_path = pty_path
    if BlueToothCapability._SHARED_BLUEZ_DBUS is not None:
      BlueToothCapability._SHARED_BLUEZ_DBUS.export_adapter(self.ble_adapt)

    self.logger.info(
        'Allocated TCP VirtualBT controller %s (bd_addr=%s, control=%d, hci=%d,'
        ' dedicated_hci=%d, phy=%d, pty=%s)',
        self.ble_adapt,
        self.bd_addr,
        self.control_port,
        self.hci_port,
        self.dedicated_hci_port,
        self.phy_port,
        self.pty_device_path,
    )

  def get_docker_run_args(self, docker_node):
    env = {
        'BLE_ADAPT': self.ble_adapt,
        'BLE_ADAPT_ID': str(self.ble_adapt_id),
        'DBUS_SYSTEM_BUS_ADDRESS': 'unix:path=/var/run/dbus/system_bus_socket',
    }
    if os.environ.get('PIP_NO_INDEX'):
      env['PIP_NO_INDEX'] = os.environ['PIP_NO_INDEX']
    volumes = []
    if self.use_virtual_bt_tcp:
      env.update({
          'VIRTUAL_BT_ENABLED': '1',
          'VIRTUAL_BT_HOST': self.virtual_bt_host,
          'VIRTUAL_BT_CONTROL_PORT': str(self.control_port),
          'VIRTUAL_BT_HCI_PORT': str(self.hci_port),
          'VIRTUAL_BT_DEDICATED_HCI_PORT': str(self.dedicated_hci_port),
          'VIRTUAL_BT_PHY_PORT': str(self.phy_port),
          'VIRTUAL_BT_BDADDR': self.bd_addr,
          'VIRTUAL_BT_HCI_DEV': f'/dev/virtual_bt/{self.ble_adapt}',
      })
      if BlueToothCapability._SHARED_DOCKER_MANAGER is not None:
        rt_dir = BlueToothCapability._SHARED_DOCKER_MANAGER.runtime_dir
        c_dbus_dir = (
            BlueToothCapability._SHARED_DOCKER_MANAGER.get_container_dbus_dir(
                self.ble_adapt
            )
        )
        org_bluez_conf = (
            BlueToothCapability._SHARED_DOCKER_MANAGER.org_bluez_conf_path
        )
        hciconfig_bin = (
            BlueToothCapability._SHARED_DOCKER_MANAGER.hciconfig_path
        )
        volumes.extend([
            f'{c_dbus_dir}:/run/dbus',
            f'{org_bluez_conf}:/etc/dbus-1/system.d/org.bluez.conf:ro',
            f'{rt_dir}:/dev/virtual_bt',
            f'{hciconfig_bin}:/usr/local/bin/hciconfig:ro',
            f'{hciconfig_bin}:/usr/bin/hciconfig:ro',
        ])
      else:
        volumes.append('/var/run/dbus:/var/run/dbus')
    else:
      volumes.append('/var/run/dbus:/var/run/dbus')
    run_args = {
        'environment': env,
        'volumes': volumes,
    }
    if not self.use_virtual_bt_tcp:
      run_args['network_mode'] = 'host'
    return run_args

  @staticmethod
  def _prepare_container_dbus_socket(container, sock_path, stop_wpa=False):
    """Ensures host UID is registered in container and D-Bus socket is writable."""
    if container is None:
      return
    uid, gid = os.getuid(), os.getgid()
    try:
      container.exec_run(
          f'sh -c "id -u {uid} >/dev/null 2>&1 || '
          f'echo hostuser:x:{uid}:{gid}:host:/tmp:/bin/sh >> /etc/passwd"'
      )
      if stop_wpa:
        container.exec_run('killall -9 wpa_supplicant >/dev/null 2>&1 || true')
      for _ in range(50):
        if os.path.exists(sock_path):
          break
        time.sleep(0.1)
      container.exec_run('chmod 0777 /run/dbus/system_bus_socket')
    except Exception:  # pylint: disable=broad-exception-caught
      pass

  def enable_capability(self, docker_node):
    if not (
        self.use_virtual_bt_tcp
        and BlueToothCapability._SHARED_BLUEZ_DBUS is not None
        and BlueToothCapability._SHARED_DOCKER_MANAGER is not None
    ):
      return
    c_dbus_dir = os.path.join(
        BlueToothCapability._SHARED_DOCKER_MANAGER.containers_dir,
        self.ble_adapt,
        'dbus',
    )
    sock_path = os.path.join(c_dbus_dir, 'system_bus_socket')
    self._prepare_container_dbus_socket(
        getattr(docker_node, 'container', None), sock_path
    )
    BlueToothCapability._SHARED_BLUEZ_DBUS.attach_container_bus(
        self.ble_adapt, sock_path
    )

  def disable_capability(self, docker_node):
    self.logger.info('ble_adapt:%s', self.ble_adapt)
    self.logger.info(
        'BLE_ADAPTS_LIST: %s', BlueToothCapability.BLE_ADAPTS_LIST
    )
    if self.ble_adapt in BlueToothCapability.BLE_ADAPTS_LIST:
      BlueToothCapability.BLE_ADAPTS_LIST.remove(self.ble_adapt)
    if self.use_virtual_bt_tcp:
      if BlueToothCapability._SHARED_DOCKER_MANAGER is not None:
        BlueToothCapability._SHARED_DOCKER_MANAGER.unregister_controller(
            self.ble_adapt
        )
      if BlueToothCapability._SHARED_VIRTUAL_SERVER is not None:
        BlueToothCapability._SHARED_VIRTUAL_SERVER.destroy_controller(
            self.ble_adapt
        )
      if not BlueToothCapability.BLE_ADAPTS_LIST:
        BlueToothCapability.stop_virtual_server()
      return
    if len(BlueToothCapability.BLE_ADAPTS_LIST):
      return
    utils.host_run(self.logger, 'killall btvirt')
    utils.host_run(self.logger, 'killall bluetoothd')

  def __get_ble_controller(self):
    self.logger.info('start getting ble adapters...')
    command = os.path.join(
        BLUEZ_DIR, "tools/hciconfig | awk '/Bus: Virtual/ {print $1}' | sort"
    )
    ret = utils.host_run(self.logger, command)
    if ret.returncode != 0:
      self.logger.error(
          'Unable to retrieve ble virtual interface, please run btvirt command'
      )
      raise RuntimeError(ret.stderr)
    self.logger.info('result from getting ble adapters: {}'.format(ret.stdout))
    ble_adapts = ret.stdout.strip(b':\n').split(b':\n')
    self.logger.info(ble_adapts)
    for adapt in ble_adapts:
      if adapt in BlueToothCapability.BLE_ADAPTS_LIST:
        continue
      BlueToothCapability.BLE_ADAPTS_LIST.append(adapt)
      adapt = adapt.decode('utf-8')
      self.logger.info('assigned ble_adapt: {}'.format(adapt))
      self.ble_adapt = adapt
      if adapt.startswith('hci') and adapt[3:].isdigit():
        self.ble_adapt_id = int(adapt[3:])
      break
    else:
      self.logger.error(
          'Run out of ble virtual interfaces, '
          'please re-run btvirt to get more virtual interfaces'
      )
    self.logger.info(
        'BLE_ADAPTS_LIST: \n{}'.format(BlueToothCapability.BLE_ADAPTS_LIST)
    )

  def __is_btvirt_running(self):
    ret = utils.host_run(
        self.logger, "ps aux | grep btvirt | grep -v grep | awk '{print $11}'"
    ).stdout
    self.logger.info('btvirt running: {}'.format(ret))
    return CIRQUE_ROOT in ret.decode('utf-8')

  def __is_bluetoothd_running(self):
    ret = utils.host_run(
        self.logger,
        "ps aux | grep bluetoothd | grep -v grep | awk '{print $11}'",
    ).stdout
    self.logger.warn('bluetoothd running: {}'.format(ret))
    return CIRQUE_ROOT in ret.decode('utf-8')

  def __run_bluetoothd(self):
    if self.__is_bluetoothd_running():
      return
    self.logger.info('kill all bluetoothd')
    utils.host_run(self.logger, 'killall bluetoothd')
    time.sleep(3)
    self.logger.info('bringing up bluetoothd')
    os.system(
        os.path.join(BLUEZ_DIR, 'src/bluetoothd --experimental --debug &')
    )
    time.sleep(2)
    if not self.__is_bluetoothd_running():
      raise RuntimeError('Unable to run bluetoothd')

  def __run_btvirt_infs(self):
    if self.__is_btvirt_running():
      return
    utils.host_run(self.logger, 'killall btvirt')
    time.sleep(3)
    self.logger.info(
        'creating virtual ble interfaces({})'.format(self.num_btvirts)
    )
    os.system(os.path.join(BLUEZ_DIR, 'emulator/btvirt -L -l2 &'))
    time.sleep(2)
    self.logger.info('done creating virtual ble...')
    if not self.__is_btvirt_running():
      raise RuntimeError('Unable to run btvirt')
