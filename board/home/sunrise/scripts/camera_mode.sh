#!/bin/bash
# Switch the exclusive RDK X5 CSI0 pipeline between TogetheROS and Yahboom APP.
# Usage: sudo ./camera_mode.sh tros|yahboom|hybrid|status
set -u

if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

MODE="${1:-status}"
APP_DIR=/home/sunrise/sunriseRobot/app_SunriseRobot
APP_MAIN="$APP_DIR/app_SunriseRobot.py"
TROS_LOG=/tmp/mipi_preview.log
YAHBOOM_LOG=/tmp/yahboom_camera_app.log
CONTROL_ONLY=/home/sunrise/scripts/yahboom_control_only.py

stop_tros() {
    pkill -TERM -f '[r]os2 launch mipi_cam' 2>/dev/null || true
    sleep 2
    pkill -TERM -x mipi_cam 2>/dev/null || true
    pkill -TERM -x websocket 2>/dev/null || true
    pkill -TERM -x nginx 2>/dev/null || true
}

stop_yahboom() {
    pkill -TERM -f '[a]pp_SunriseRobot.py' 2>/dev/null || true
    pkill -TERM -f '[y]ahboom_control_only.py' 2>/dev/null || true
    for _ in $(seq 1 10); do
        if ! pgrep -f '[a]pp_SunriseRobot.py|[y]ahboom_control_only.py' >/dev/null; then
            return 0
        fi
        sleep 1
    done
    pkill -KILL -f '[a]pp_SunriseRobot.py' 2>/dev/null || true
    pkill -KILL -f '[y]ahboom_control_only.py' 2>/dev/null || true
}

wait_for_camera_release() {
    for _ in $(seq 1 15); do
        if ! fuser /dev/vin0_src /dev/vs-isp0_src /dev/vs-vse0_src >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    echo "ERROR: CSI0 is still owned by another process:" >&2
    fuser -v /dev/vin0_src /dev/vs-isp0_src /dev/vs-vse0_src >&2 || true
    return 1
}

ensure_camera_service() {
    systemctl start cam-service
    if [ "$(systemctl is-active cam-service)" != active ]; then
        echo "ERROR: cam-service failed to start" >&2
        systemctl --no-pager --full status cam-service >&2 || true
        return 1
    fi
}

show_status() {
    echo "cam-service: $(systemctl is-active cam-service 2>/dev/null || true) / $(systemctl is-enabled cam-service 2>/dev/null || true)"
    if pgrep -f '[r]os2 launch mipi_cam' >/dev/null \
        && pgrep -f '[y]ahboom_control_only.py' >/dev/null; then
        echo "mode: Hybrid (TogetheROS video + Yahboom control)"
    elif pgrep -f '[r]os2 launch mipi_cam' >/dev/null; then
        echo "mode: TogetheROS"
    elif pgrep -f '[a]pp_SunriseRobot.py' >/dev/null; then
        echo "mode: Yahboom APP"
    else
        echo "mode: idle"
    fi
    ss -ltnp | grep -E ':(6000|6500|8000)\b' || true
}

start_tros() {
    echo "==> Switching CSI0 to TogetheROS preview..."
    stop_yahboom
    stop_tros
    ensure_camera_service
    wait_for_camera_release

    setsid bash -c 'source /opt/tros/humble/setup.bash; exec ros2 launch mipi_cam mipi_cam_websocket.launch.py' \
        </dev/null >"$TROS_LOG" 2>&1 &

    for _ in $(seq 1 35); do
        if ss -ltn | grep -q ':8000 '; then
            sleep 3
            break
        fi
        sleep 1
    done

    local hz
    hz=$(timeout 7 bash -c 'source /opt/tros/humble/setup.bash; ros2 topic hz /image_raw 2>&1' \
        | grep -m1 'average rate' || true)
    if [ -z "$hz" ]; then
        echo "ERROR: TogetheROS started but /image_raw has no frames" >&2
        tail -40 "$TROS_LOG" >&2 || true
        return 1
    fi

    local ip
    ip=$(hostname -I | awk '{print $1}')
    echo "OK: $hz"
    echo "Preview: http://$ip:8000/TogetheROS/"
}

start_yahboom() {
    echo "==> Switching CSI0 to Yahboom APP..."
    stop_tros
    stop_yahboom
    wait_for_camera_release

    # mipi_cam may leave its CSI allocation cached in the /dev/isc service even
    # after all device fds are closed. Restart the middleware before libsrcampy
    # opens CSI0, otherwise Yahboom starts with control only and no video.
    systemctl restart cam-service
    sleep 3
    ensure_camera_service

    setsid bash -c "cd '$APP_DIR'; exec python3 '$APP_MAIN'" \
        </dev/null >"$YAHBOOM_LOG" 2>&1 &

    for _ in $(seq 1 45); do
        if ss -ltn | grep -q ':6000 ' && ss -ltn | grep -q ':6500 '; then
            local ip
            ip=$(hostname -I | awk '{print $1}')
            echo "OK: Yahboom APP is listening"
            echo "IP: $ip"
            echo "Control port: 6000"
            echo "Video port: 6500"
            return 0
        fi
        sleep 1
    done

    echo "ERROR: Yahboom APP did not open ports 6000 and 6500" >&2
    tail -60 "$YAHBOOM_LOG" >&2 || true
    return 1
}

start_hybrid() {
    echo "==> Switching to TogetheROS video + Yahboom control..."
    start_tros

    setsid python3 "$CONTROL_ONLY" \
        </dev/null >"$YAHBOOM_LOG" 2>&1 &

    for _ in $(seq 1 45); do
        if ss -ltn | grep -q ':6000 ' && ss -ltn | grep -q ':6500 '; then
            local ip
            ip=$(hostname -I | awk '{print $1}')
            echo "OK: hybrid mode is ready"
            echo "Yahboom control: $ip:6000"
            echo "Yahboom video port: 6500 (intentionally no CSI video)"
            echo "TogetheROS video: http://$ip:8000/TogetheROS/"
            return 0
        fi
        sleep 1
    done

    echo "ERROR: Yahboom control-only APP did not open ports 6000 and 6500" >&2
    tail -60 "$YAHBOOM_LOG" >&2 || true
    return 1
}

case "$MODE" in
    tros|preview)
        start_tros
        ;;
    yahboom|app)
        start_yahboom
        ;;
    hybrid|both)
        start_hybrid
        ;;
    status)
        show_status
        ;;
    *)
        echo "Usage: $0 tros|yahboom|hybrid|status" >&2
        exit 2
        ;;
esac
