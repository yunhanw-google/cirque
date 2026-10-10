#!/usr/bin/env bash
export PYTHONPATH="/usr/local/google/home/yunhanw/chip-thread-fix/third_party/cirque/repo:/usr/local/google/home/yunhanw/chip-thread-fix/src/test_driver/linux-cirque/mcp"
exec python3 /usr/local/google/home/yunhanw/chip-thread-fix/third_party/cirque/repo/cirque/mcp/server.py --extension /usr/local/google/home/yunhanw/chip-thread-fix/src/test_driver/linux-cirque/mcp/matter_cirque_extension.py "$@"
