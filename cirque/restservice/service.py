# Copyright 2020 Google LLC
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
import atexit

from flask import Flask
from flask import jsonify
from flask import request
from flask import Response

from cirque.common.cirquelog import CirqueLog
from cirque.common.taskrunner import TaskRunner
from cirque.home.home import CirqueHome

app = Flask(__name__)
CirqueLog.setup_cirque_logger()
logger = CirqueLog.get_cirque_logger('service')

service_mode = os.environ.get('CIRQUE_DEBUG', 0)

homes = {}


@app.route('/create_home', methods=['POST'])
def create_home():
  home = CirqueHome()
  homes[home.home_id] = home
  return jsonify(home.create_home(request.json))


@app.route('/get_homes', methods=['GET'])
def get_homes():
  return jsonify(list(homes.keys()))


@app.route('/wifi_ssid_psk/<home_id>', methods=['GET'])
def get_wifi_ssid_psk(home_id):
  return jsonify(homes[home_id].get_wifiap_ssid_psk())


@app.route('/home_devices/<home_id>', methods=['GET'])
def get_home_devices(home_id):
  if home_id not in homes:
    return ''
  return jsonify(homes[home_id].get_home_devices())


@app.route('/device_state/<home_id>/<device_id>', methods=['GET'])
def get_device_state(home_id, device_id):
  if home_id not in homes:
    return ''
  return jsonify(homes[home_id].get_device_state(device_id))


@app.route('/device_cmd/<home_id>/<device_id>/<path:cmd>', methods=['GET'])
def execute_device_cmd(home_id, device_id, cmd):
  if home_id not in homes:
    return ''
  stream = request.args.get('stream')
  ret = homes[home_id].execute_device_cmd(cmd, device_id, stream == 'True')
  return Response(
      ret.output, mimetype='text/plain') if stream == 'True' else jsonify(
          dict({
              'return_code': str(ret.exit_code),
              'output': ret.output.decode('utf-8', errors='replace')
          }))


@app.route('/stop_device/<home_id>/<device_id>', methods=['GET'])
def stop_device(home_id, device_id):
  if home_id not in homes:
    return ''
  return jsonify(homes[home_id].stop_device(device_id))


@app.route('/device_log/<home_id>/<device_id>', methods=['GET'])
def device_log(home_id, device_id):
  tail = request.args.get('tail', None)
  if tail is not None and tail.isdigit():
    tail = int(tail)
  else:
    tail = 'all'

  if home_id not in homes:
    return ''
  return homes[home_id].get_device_log(device_id, tail)


@app.route('/destroy_home/<home_id>', methods=['GET'])
def destroy_home(home_id):
  if home_id not in homes:
    return ''
  reply = jsonify(homes[home_id].destroy_home())
  del homes[home_id]
  return reply


@app.route('/virtual_bt_info', methods=['GET'])
def virtual_bt_info():
  from cirque.capabilities.bluetoothcapability import BlueToothCapability
  server = BlueToothCapability.get_or_start_virtual_server()
  return jsonify({
      'host': server.host,
      'control_port': server.control_port,
      'hci_port': server.hci_port,
      'phy_port': server.phy_port,
      'controllers': server.list_controllers(),
  })


@app.route('/virtual_wifi_info', methods=['GET'])
def virtual_wifi_info():
  from dataclasses import asdict
  from cirque.capabilities.wificapability import WiFiCapability
  server = WiFiCapability.get_or_start_virtual_server()
  return jsonify({
      'host': server.host,
      'control_port': server.control_port,
      'mgmt_port': server.mgmt_port,
      'data_port': server.data_port,
      'aps': [asdict(ap) for ap in server.list_aps()],
      'stations': [asdict(st) for st in server.list_stations()],
  })


@app.route(
    '/init_android_emulator/<home_id>/<device_id>', methods=['GET', 'POST']
)
def init_android_emulator(home_id, device_id):
  if home_id not in homes:
    return jsonify({'status': 'failed', 'error': 'home not found'}), 404
  node = homes[home_id].devices.get(device_id)
  if not node or not hasattr(node, 'run_android_emulator'):
    return (
        jsonify({'status': 'failed', 'error': 'not an android emulator node'}),
        400,
    )
  timeout_sec = float(request.args.get('timeout', 90.0))
  res = node.run_android_emulator(timeout_sec=timeout_sec)
  return jsonify(res)


@app.route(
    '/commission_chiptool/<home_id>/<device_id>', methods=['GET', 'POST']
)
def commission_chiptool(home_id, device_id):
  if home_id not in homes:
    return jsonify({'status': 'failed', 'error': 'home not found'}), 404
  node = homes[home_id].devices.get(device_id)
  if not node:
    return (
        jsonify({'status': 'failed', 'error': 'not an android emulator node'}),
        400,
    )
  network_type = request.args.get('network_type', 'wifi')
  ssid = request.args.get('ssid', 'CHIP-VirtualWiFi-AP')
  psk = request.args.get('psk', 'ChipWiFiPassword123')
  discriminator = int(request.args.get('discriminator', 3840))
  setup_pin_code = int(request.args.get('setup_pin_code', 20202021))
  manual_code = request.args.get('manual_code', '34970112332')
  timeout_sec = float(request.args.get('timeout', 60.0))

  if network_type.lower() == 'thread':
    if not hasattr(node, 'commission_thread_via_chiptool_ui'):
      return (
          jsonify(
              {'status': 'failed', 'error': 'not an android emulator node'}
          ),
          400,
      )
    import base64
    from cirque.home.virtual_home_topology import (
        OTBR_DATASET_COMPLETION_HELPER,
        THREAD_JOINER_ATTACH_HELPER,
    )
    has_tbr = any(
        getattr(d, 'type', '') == 'ThreadBorderRouter'
        for d in homes[home_id].devices.values()
    )
    helper_script = (
        THREAD_JOINER_ATTACH_HELPER
        if has_tbr
        else OTBR_DATASET_COMPLETION_HELPER
    )
    b64_helper = base64.b64encode(helper_script.encode('utf-8')).decode('ascii')
    helper_cmd = (
        'sh -c "if kill -0 $(cat /tmp/joiner_helper.pid 2>/dev/null) '
        '2>/dev/null || kill -0 $(cat /tmp/thread_joiner_helper.pid '
        '2>/dev/null) 2>/dev/null; then exit 0; fi; '
        'kill -9 $(cat /tmp/dataset_helper.pid 2>/dev/null) '
        '2>/dev/null || true; '
        f'echo \\"{b64_helper}\\" | base64 -d > /tmp/dataset_helper.py && '
        'nohup python3 /tmp/dataset_helper.py >/tmp/dataset_helper.log '
        '2>&1 & echo $! > /tmp/dataset_helper.pid"'
    )
    for dev_id, dev_node in homes[home_id].devices.items():
      if (
          dev_id != device_id
          and getattr(dev_node, 'type', '') != 'ThreadBorderRouter'
          and any(
              getattr(c, 'name', '') == 'Thread'
              for c in getattr(dev_node, 'capabilities', [])
          )
      ):
        if getattr(dev_node, 'container', None) is not None:
          dev_node.container.exec_run(helper_cmd, detach=True)
        else:
          homes[home_id].execute_device_cmd(helper_cmd, dev_id)
    channel = request.args.get('channel', None)
    if channel is not None:
      channel = int(channel)
    pan_id = request.args.get('pan_id', None)
    xpan_id = request.args.get('xpan_id', None)
    master_key = request.args.get('master_key', None)
    res = node.commission_thread_via_chiptool_ui(
        channel=channel,
        pan_id=pan_id,
        xpan_id=xpan_id,
        master_key=master_key,
        setup_pin_code=setup_pin_code,
        discriminator=discriminator,
        manual_code=manual_code,
        timeout_sec=timeout_sec,
    )
  else:
    if not hasattr(node, 'commission_via_chiptool_ui'):
      return (
          jsonify(
              {'status': 'failed', 'error': 'not an android emulator node'}
          ),
          400,
      )
    trigger = request.args.get('trigger', 'ui')
    res = node.commission_via_chiptool_ui(
        ssid=ssid,
        psk=psk,
        setup_pin_code=setup_pin_code,
        discriminator=discriminator,
        manual_code=manual_code,
        timeout_sec=timeout_sec,
        trigger=trigger,
    )
  return jsonify(res)


@app.route('/toggle_chiptool/<home_id>/<device_id>', methods=['GET', 'POST'])
def toggle_chiptool(home_id, device_id):
  if home_id not in homes:
    return jsonify({'status': 'failed', 'error': 'home not found'}), 404
  node = homes[home_id].devices.get(device_id)
  if not node or not hasattr(node, 'toggle_onoff_via_chiptool_ui'):
    return (
        jsonify({'status': 'failed', 'error': 'not an android emulator node'}),
        400,
    )
  node_id = int(request.args.get('node_id', 1))
  endpoint = int(request.args.get('endpoint', 1))
  timeout_sec = float(request.args.get('timeout', 15.0))
  res = node.toggle_onoff_via_chiptool_ui(
      node_id=node_id, endpoint=endpoint, timeout_sec=timeout_sec
  )
  return jsonify(res)


@app.route('/read_chiptool/<home_id>/<device_id>', methods=['GET', 'POST'])
def read_chiptool(home_id, device_id):
  if home_id not in homes:
    return jsonify({'status': 'failed', 'error': 'home not found'}), 404
  node = homes[home_id].devices.get(device_id)
  if not node or not hasattr(node, 'read_onoff_via_chiptool_ui'):
    return (
        jsonify({'status': 'failed', 'error': 'not an android emulator node'}),
        400,
    )
  node_id = int(request.args.get('node_id', 1))
  endpoint = int(request.args.get('endpoint', 1))
  timeout_sec = float(request.args.get('timeout', 15.0))
  res = node.read_onoff_via_chiptool_ui(
      node_id=node_id, endpoint=endpoint, timeout_sec=timeout_sec
  )
  return jsonify(res)


@app.route('/commission_app/<home_id>/<device_id>', methods=['GET', 'POST'])
def commission_app(home_id, device_id):
  return commission_chiptool(home_id, device_id)


@app.route('/commission_app/<device_id>', methods=['GET', 'POST'])
def commission_app_single(device_id):
  if not homes:
    return jsonify({'status': 'failed', 'error': 'no active homes'}), 404
  home_id = next(iter(homes.keys()))
  return commission_chiptool(home_id, device_id)


@app.route('/toggle_app/<home_id>/<device_id>', methods=['GET', 'POST'])
def toggle_app(home_id, device_id):
  return toggle_chiptool(home_id, device_id)


@app.route('/toggle_app/<device_id>', methods=['GET', 'POST'])
def toggle_app_single(device_id):
  if not homes:
    return jsonify({'status': 'failed', 'error': 'no active homes'}), 404
  home_id = next(iter(homes.keys()))
  return toggle_chiptool(home_id, device_id)


@app.route('/read_app/<home_id>/<device_id>', methods=['GET', 'POST'])
def read_app(home_id, device_id):
  return read_chiptool(home_id, device_id)


@app.route('/read_app/<device_id>', methods=['GET', 'POST'])
def read_app_single(device_id):
  if not homes:
    return jsonify({'status': 'failed', 'error': 'no active homes'}), 404
  home_id = next(iter(homes.keys()))
  return read_chiptool(home_id, device_id)


@app.route(
    '/start_screen_recording/<path:home_id>/<path:device_id>',
    methods=['GET', 'POST'],
)
def start_screen_recording(home_id, device_id):
  if home_id not in homes:
    return jsonify({'status': 'failed', 'error': 'home not found'}), 404
  node = homes[home_id].devices.get(device_id)
  if not node or not hasattr(node, 'start_screen_recording'):
    return (
        jsonify({'status': 'failed', 'error': 'not an android emulator node'}),
        400,
    )
  name = request.args.get('name', 'screen_recording.mp4')
  bit_rate = int(request.args.get('bit_rate', 4000000))
  time_limit = int(request.args.get('time_limit', 180))
  size = request.args.get('size', None)
  res = node.start_screen_recording(
      name=name, bit_rate=bit_rate, time_limit_sec=time_limit, size=size
  )
  return jsonify(res)


@app.route(
    '/stop_screen_recording/<path:home_id>/<path:device_id>',
    methods=['GET', 'POST'],
)
def stop_screen_recording(home_id, device_id):
  if home_id not in homes:
    return jsonify({'status': 'failed', 'error': 'home not found'}), 404
  node = homes[home_id].devices.get(device_id)
  if not node or not hasattr(node, 'stop_screen_recording'):
    return (
        jsonify({'status': 'failed', 'error': 'not an android emulator node'}),
        400,
    )
  res = node.stop_screen_recording()
  return jsonify(res)


@app.route(
    '/list_screen_recordings/<path:home_id>/<path:device_id>',
    methods=['GET'],
)
def list_screen_recordings(home_id, device_id):
  if home_id not in homes:
    return jsonify({'status': 'failed', 'error': 'home not found'}), 404
  node = homes[home_id].devices.get(device_id)
  if not node or not hasattr(node, 'list_screen_recordings'):
    return (
        jsonify({'status': 'failed', 'error': 'not an android emulator node'}),
        400,
    )
  return jsonify({'recordings': node.list_screen_recordings()})


@app.route(
    '/get_screen_recording/<path:home_id>/<path:device_id>/'
    '<path:recording_name>',
    methods=['GET'],
)
def get_screen_recording(home_id, device_id, recording_name):
  if home_id not in homes:
    return jsonify({'status': 'failed', 'error': 'home not found'}), 404
  node = homes[home_id].devices.get(device_id)
  if not node or not hasattr(node, 'get_screen_recording_bytes'):
    return (
        jsonify({'status': 'failed', 'error': 'not an android emulator node'}),
        400,
    )
  data = node.get_screen_recording_bytes(recording_name)
  if not data:
    return (
        jsonify({'status': 'failed', 'error': 'recording not found or empty'}),
        404,
    )
  return Response(data, mimetype='video/mp4')


@app.route('/')
def destroy_homes():
  global homes
  logger.info('removing all the homes..')
  temp_homes = list(homes.keys())
  for home_id in temp_homes:
    homes[home_id].destroy_home()
    del homes[home_id]
  del homes
  return ''


# becareful not to remove this part
atexit.register(lambda: TaskRunner.stop())
if service_mode:
  atexit.register(destroy_homes)
TaskRunner.start()

if __name__ == '__main__':
  app.run()
