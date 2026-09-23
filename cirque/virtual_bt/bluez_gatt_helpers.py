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
"""BlueZ D-Bus introspection XML, ObjectManager builder, and GATT helpers."""

import os
import struct
import time
from typing import Dict, Optional

from cirque.virtual_bt.matter_ble_bridge import (
    MATTER_C1_UUID_STR,
    MATTER_C2_UUID_STR,
    MATTER_SERVICE_UUID_STR,
    VirtualBluezAdapterBridge,
)

BLUEZ_INTROSPECT_XML = """
<node>
  <interface name="org.freedesktop.DBus.ObjectManager">
    <method name="GetManagedObjects">
      <arg name="objects" type="a{oa{sa{sv}}}" direction="out"/>
    </method>
    <signal name="InterfacesAdded">
      <arg name="object" type="o"/>
      <arg name="interfaces" type="a{sa{sv}}"/>
    </signal>
    <signal name="InterfacesRemoved">
      <arg name="object" type="o"/>
      <arg name="interfaces" type="as"/>
    </signal>
  </interface>
  <interface name="org.bluez.Adapter1">
    <method name="StartDiscovery"/>
    <method name="StopDiscovery"/>
    <method name="SetDiscoveryFilter">
      <arg name="properties" type="a{sv}" direction="in"/>
    </method>
    <method name="RemoveDevice">
      <arg name="device" type="o" direction="in"/>
    </method>
    <property name="Address" type="s" access="read"/>
    <property name="AddressType" type="s" access="read"/>
    <property name="Name" type="s" access="read"/>
    <property name="Alias" type="s" access="readwrite"/>
    <property name="Class" type="u" access="read"/>
    <property name="Powered" type="b" access="readwrite"/>
    <property name="Discoverable" type="b" access="readwrite"/>
    <property name="Discovering" type="b" access="read"/>
    <property name="UUIDs" type="as" access="read"/>
  </interface>
  <interface name="org.bluez.GattManager1">
    <method name="RegisterApplication">
      <arg name="application" type="o" direction="in"/>
      <arg name="options" type="a{sv}" direction="in"/>
    </method>
    <method name="UnregisterApplication">
      <arg name="application" type="o" direction="in"/>
    </method>
  </interface>
  <interface name="org.bluez.LEAdvertisingManager1">
    <method name="RegisterAdvertisement">
      <arg name="advertisement" type="o" direction="in"/>
      <arg name="options" type="a{sv}" direction="in"/>
    </method>
    <method name="UnregisterAdvertisement">
      <arg name="advertisement" type="o" direction="in"/>
    </method>
    <property name="ActiveInstances" type="y" access="read"/>
    <property name="SupportedInstances" type="y" access="read"/>
    <property name="SupportedIncludes" type="as" access="read"/>
  </interface>
  <interface name="org.bluez.Device1">
    <method name="Connect"/>
    <method name="Disconnect"/>
    <property name="Address" type="s" access="read"/>
    <property name="AddressType" type="s" access="read"/>
    <property name="Name" type="s" access="read"/>
    <property name="Alias" type="s" access="read"/>
    <property name="Paired" type="b" access="read"/>
    <property name="Trusted" type="b" access="readwrite"/>
    <property name="Blocked" type="b" access="readwrite"/>
    <property name="LegacyPairing" type="b" access="read"/>
    <property name="RSSI" type="n" access="read"/>
    <property name="Connected" type="b" access="read"/>
    <property name="UUIDs" type="as" access="read"/>
    <property name="Adapter" type="o" access="read"/>
    <property name="ServiceData" type="a{sv}" access="read"/>
    <property name="ServicesResolved" type="b" access="read"/>
  </interface>
  <interface name="org.bluez.GattService1">
    <property name="UUID" type="s" access="read"/>
    <property name="Device" type="o" access="read"/>
    <property name="Primary" type="b" access="read"/>
  </interface>
  <interface name="org.bluez.GattCharacteristic1">
    <method name="ReadValue">
      <arg name="options" type="a{sv}" direction="in"/>
      <arg name="value" type="ay" direction="out"/>
    </method>
    <method name="WriteValue">
      <arg name="value" type="ay" direction="in"/>
      <arg name="options" type="a{sv}" direction="in"/>
    </method>
    <method name="StartNotify"/>
    <method name="StopNotify"/>
    <property name="UUID" type="s" access="read"/>
    <property name="Service" type="o" access="read"/>
    <property name="Value" type="ay" access="read"/>
    <property name="Notifying" type="b" access="read"/>
    <property name="Flags" type="as" access="read"/>
  </interface>
</node>
"""


def build_dbus_bus_conf_xml(socket_path: str) -> str:
  """Returns the permissive D-Bus daemon configuration XML for socket_path."""
  doctype = (
      '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus '
      'Configuration 1.0//EN"\n '
      '"http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">'
  )
  return f"""{doctype}
<busconfig>
  <type>system</type>
  <listen>unix:path={socket_path}</listen>
  <auth>EXTERNAL</auth>
  <auth>ANONYMOUS</auth>
  <allow_anonymous/>
  <policy context="default">
    <allow user="*"/>
    <allow own="*"/>
    <allow send_type="method_call"/>
    <allow send_type="signal"/>
    <allow send_type="method_return"/>
    <allow send_type="error"/>
    <allow receive_type="method_call"/>
    <allow receive_type="signal"/>
    <allow receive_type="method_return"/>
    <allow receive_type="error"/>
  </policy>
</busconfig>
"""


def _build_char_entry(
    uuid_str: str, svc_path: str, notifying: bool, flag: str
) -> dict:
  """Builds a single org.bluez.GattCharacteristic1 dictionary."""
  from gi.repository import GLib  # pylint: disable=g-import-not-at-top

  return {
      'org.bluez.GattCharacteristic1': {
          'UUID': GLib.Variant('s', uuid_str),
          'Service': GLib.Variant('o', svc_path),
          'Value': GLib.Variant('ay', []),
          'Notifying': GLib.Variant('b', notifying),
          'Flags': GLib.Variant('as', [flag]),
      }
  }


def _build_peripheral_gatt_entries(
    adapter_path: str,
    addr: str,
    pinfo: Dict[str, object],
    is_conn: bool,
) -> dict:
  """Builds Device1, GattService1, and C1/C2 entries for one peripheral."""
  from gi.repository import GLib  # pylint: disable=g-import-not-at-top

  dev_path = f'{adapter_path}/dev_{addr.replace(":", "_")}'
  svc_data = pinfo.get('service_data') or b'\x00\x00\x0f\xf1\xff\x01\x80\x00'
  disc_val = pinfo.get('discriminator', 3840)
  svc_path = f'{dev_path}/service00'
  return {
      dev_path: {
          'org.bluez.Device1': {
              'Address': GLib.Variant('s', addr),
              'AddressType': GLib.Variant('s', 'public'),
              'Name': GLib.Variant('s', f'MATTER-{disc_val}'),
              'Alias': GLib.Variant('s', f'MATTER-{disc_val}'),
              'Paired': GLib.Variant('b', False),
              'Trusted': GLib.Variant('b', True),
              'Blocked': GLib.Variant('b', False),
              'LegacyPairing': GLib.Variant('b', False),
              'RSSI': GLib.Variant('n', int(pinfo.get('rssi', -45))),
              'Connected': GLib.Variant('b', is_conn),
              'UUIDs': GLib.Variant('as', [MATTER_SERVICE_UUID_STR]),
              'Adapter': GLib.Variant('o', adapter_path),
              'ServiceData': GLib.Variant(
                  'a{sv}',
                  {MATTER_SERVICE_UUID_STR: GLib.Variant('ay', list(svc_data))},
              ),
              'ServicesResolved': GLib.Variant('b', True),
          }
      },
      svc_path: {
          'org.bluez.GattService1': {
              'UUID': GLib.Variant('s', MATTER_SERVICE_UUID_STR),
              'Device': GLib.Variant('o', dev_path),
              'Primary': GLib.Variant('b', True),
          }
      },
      f'{svc_path}/char00': _build_char_entry(
          MATTER_C1_UUID_STR, svc_path, False, 'write'
      ),
      f'{svc_path}/char01': _build_char_entry(
          MATTER_C2_UUID_STR, svc_path, True, 'indicate'
      ),
  }


def build_managed_objects_dict(
    adapter_bridges: Dict[str, VirtualBluezAdapterBridge],
) -> dict:
  """Builds the GetManagedObjects dictionary for all virtual adapters."""
  from gi.repository import GLib  # pylint: disable=g-import-not-at-top

  objects = {}
  for cid, bridge in adapter_bridges.items():
    adapter_path = f'/org/bluez/{cid}'
    objects[adapter_path] = {
        'org.bluez.Adapter1': {
            'Address': GLib.Variant('s', bridge.bd_addr),
            'AddressType': GLib.Variant('s', 'public'),
            'Name': GLib.Variant('s', f'VirtualBT-{cid}'),
            'Alias': GLib.Variant('s', cid),
            'Class': GLib.Variant('u', 0x000104),
            'Powered': GLib.Variant('b', True),
            'Discoverable': GLib.Variant('b', False),
            'Discovering': GLib.Variant('b', False),
            'UUIDs': GLib.Variant('as', [MATTER_SERVICE_UUID_STR]),
        },
        'org.bluez.GattManager1': {},
        'org.bluez.LEAdvertisingManager1': {
            'ActiveInstances': GLib.Variant('y', 1),
            'SupportedInstances': GLib.Variant('y', 5),
            'SupportedIncludes': GLib.Variant(
                'as', ['tx-power', 'appearance', 'local-name']
            ),
        },
    }
    conn_addrs = set(bridge.connected_handles.values())
    for addr, pinfo in bridge.discovered_peripherals.items():
      objects.update(
          _build_peripheral_gatt_entries(
              adapter_path, addr, pinfo, addr in conn_addrs
          )
      )
  return objects


def acquire_gatt_characteristic_fd(
    app_conn,
    sender: str,
    char_path: str,
    method_name: str,
    periph_dev_path: str,
) -> Optional[int]:
  """Calls AcquireWrite or AcquireNotify on a peripheral GATT characteristic."""
  from gi.repository import GLib  # pylint: disable=g-import-not-at-top

  opts = GLib.Variant(
      '(a{sv})',
      (
          {
              'device': GLib.Variant('o', periph_dev_path),
              'mtu': GLib.Variant('q', 247),
          },
      ),
  )
  for _ in range(30):
    try:
      _, fd_list = app_conn.call_with_unix_fd_list_sync(
          sender,
          char_path,
          'org.bluez.GattCharacteristic1',
          method_name,
          opts,
          GLib.VariantType('(hq)'),
          0,
          500,
          None,
          None,
      )
      if fd_list and fd_list.get_length() > 0:
        fd = fd_list.steal_fds()[0]
        try:
          os.set_blocking(fd, True)
        except OSError:
          pass
        return fd
    except Exception as exc:  # pylint: disable=broad-exception-caught
      if 'ServiceUnknown' in str(exc) or 'NameHasNoOwner' in str(exc):
        break
      time.sleep(0.05)
  return None


def query_advertisement_discriminator(
    connection, sender: str, adv_path: str
) -> int:
  """Queries the Matter 0xFFF6 discriminator from an LEAdvertisement1 object."""
  from gi.repository import GLib  # pylint: disable=g-import-not-at-top

  try:
    res = connection.call_sync(
        sender,
        adv_path,
        'org.freedesktop.DBus.Properties',
        'Get',
        GLib.Variant('(ss)', ('org.bluez.LEAdvertisement1', 'ServiceData')),
        GLib.VariantType('(v)'),
        0,
        150,
        None,
    )
    sdata = res.unpack()[0]
    raw = bytes(
        sdata.get(MATTER_SERVICE_UUID_STR)
        or sdata.get('fff6')
        or sdata.get('0xfff6')
        or []
    )
    if len(raw) >= 3:
      return struct.unpack('<H', raw[1:3])[0] & 0x0FFF
  except Exception:  # pylint: disable=broad-exception-caught
    pass
  return 3840


def handle_bluez_adapter_method(
    bridge: Optional[VirtualBluezAdapterBridge],
    method_name: str,
    parameters,
    invocation,
) -> None:
  """Handles org.bluez.Adapter1 D-Bus method calls."""
  if bridge is not None and bridge.h4_client is not None:
    if method_name == 'StartDiscovery':
      with bridge._lock:  # pylint: disable=protected-access
        bridge.discovered_peripherals.clear()
      with bridge.h4_client._cv:  # pylint: disable=protected-access
        bridge.h4_client._adv_reports.clear()  # pylint: disable=protected-access
      bridge.h4_client.start_scanning(active=True)
    elif method_name == 'StopDiscovery':
      bridge.h4_client.send_command(0x200C, b'\x00\x00')
    elif method_name == 'RemoveDevice':
      dev_part = parameters.unpack()[0].split('/')[-1]
      peer_addr = dev_part.replace('dev_', '').replace('_', ':').upper()
      with bridge._lock:  # pylint: disable=protected-access
        bridge.discovered_peripherals.pop(peer_addr, None)
      with bridge.h4_client._cv:  # pylint: disable=protected-access
        bridge.h4_client._adv_reports.clear()  # pylint: disable=protected-access
  invocation.return_value(None)
