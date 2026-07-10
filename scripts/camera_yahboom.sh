#!/bin/bash
# Switch to the original Yahboom control APP and video mode.
set -u
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
exec "$SCRIPT_DIR/camera_mode.sh" yahboom
