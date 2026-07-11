#!/bin/bash
# Compatibility wrapper: switch to TogetheROS MIPI camera preview mode.
set -u
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
exec "$SCRIPT_DIR/camera_mode.sh" tros
