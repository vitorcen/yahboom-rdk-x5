#!/usr/bin/env python3
"""Astra Pro depth + color -> JPEG for the GUI, over rosbridge.

We do NOT use the vendor astra_camera ROS driver: on this unit its laser/LDP
handling leaves depth all-zero (it boots the IR laser off, re-enabling briefly
works then LDP kills it, and `set_ldp_enable` fails with "Couldn't set LDP
enable"). Raw OpenNI2, by contrast, delivers rock-stable depth (~12-14k valid
px @ 30fps indefinitely). So this node talks OpenNI2 directly for depth and the
plain UVC node for color:

  OpenNI2 depth (16-bit mm) --JET colormap--> /camera/depth/color_jpeg
  UVC /dev/video0 (MJPEG)   --------------->  /image_jpeg

Depth colormap: near = red, far = blue (matches the GUI legend); no-return
pixels render black. Color is downscaled to ~480 wide — a full-res color stream
starves the depth transfer on this USB2 link, so it is OFF by default. Both
outputs are sensor_msgs/CompressedImage (jpeg), throttled to ~PUB_HZ.

Liveness: only *new* depth frames are published (a frozen sensor stops the
stream instead of re-sending stale frames), and a watchdog exits the process on
depth death/stall so systemd restarts it clean — that also recovers hot-unplug.

Requires: pip3 install primesense ; OpenNI2 redist at OPENNI_LIB (ships in the
Yahboom library_ws; override with $ASTRA_OPENNI_LIB).
"""
import os
import time
import threading
import numpy as np
import cv2
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from primesense import openni2
from primesense import _openni2 as c_api

OPENNI_LIB = os.environ.get('ASTRA_OPENNI_LIB',
                            '/home/sunrise/software/library_ws/install/astra_camera/lib')
NEAR_MM = 300      # red end
FAR_MM = 4000      # blue end
COLOR_W = 480      # downscale width for the right-window color (if enabled)
PUB_HZ = 15.0      # depth source is 30fps; 15 is smooth and cheap (colorize+encode ~4ms)
JPEG_Q = 80
FIRST_FRAME_GRACE = 12.0   # allow this long for the first depth frame at startup
STALE_TIMEOUT = 3.0        # no new depth frame for this long -> exit for restart
# The UVC color stream (stuck at 960x544@30, no smaller mode) is isochronous and
# starves the depth bulk transfer on this USB2 link: depth drops from 30fps to
# ~1.5fps the moment color streams. Depth is the point, so color is OFF by
# default. Enable with `enable_color:=true` only if you accept choppy depth.
ENABLE_COLOR_DEFAULT = False

# Precompute mm -> JET colormap index once (65536-entry uint8 LUT). Per frame
# this turns the colorize into a single fancy-index instead of a float
# convert + clip + arithmetic over the whole image — much cheaper on the SoC.
_MM = np.arange(65536, dtype=np.float32)
_DN = np.clip((_MM - NEAR_MM) / (FAR_MM - NEAR_MM), 0.0, 1.0)
DEPTH_LUT = ((1.0 - _DN) * 255.0).astype(np.uint8)      # near(small mm) -> 255 -> red


class AstraPreview(Node):
    def __init__(self):
        super().__init__('astra_preview')
        self.declare_parameter('enable_color', ENABLE_COLOR_DEFAULT)
        self.enable_color = self.get_parameter('enable_color').value
        self.pub_depth = self.create_publisher(CompressedImage, '/camera/depth/color_jpeg', 1)
        self.pub_color = self.create_publisher(CompressedImage, '/image_jpeg', 1)
        self.cap = None
        self.run = True

        # Depth liveness bookkeeping. depth_seq advances per captured frame; the
        # timer only publishes when it changes, and exits if it stalls.
        self.latest_depth = None
        self.depth_seq = 0
        self.last_depth_mono = None
        self.depth_fatal = False
        self.last_pub_seq = -1
        self.start_mono = time.monotonic()

        # Depth via OpenNI2 (direct — the vendor ROS driver's laser/LDP path is broken).
        # 320x240 depth: at 640x480 the raw 16-bit stream (~18MB/s) saturates the
        # USB2 link and starves color; QVGA quarters it and is plenty for preview.
        if not os.path.isdir(OPENNI_LIB):
            raise RuntimeError(f'OpenNI lib dir not found: {OPENNI_LIB} (set $ASTRA_OPENNI_LIB)')
        openni2.initialize(OPENNI_LIB)
        self.dev = openni2.Device.open_any()
        self.depth = self.dev.create_depth_stream()
        self.depth.set_video_mode(c_api.OniVideoMode(
            pixelFormat=c_api.OniPixelFormat.ONI_PIXEL_FORMAT_DEPTH_1_MM,
            resolutionX=320, resolutionY=240, fps=30))
        self.depth.start()

        threading.Thread(target=self._depth_loop, daemon=True).start()
        if self.enable_color:
            self.cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
            if not self.cap.isOpened():
                self.get_logger().error('color camera open failed — color disabled')
                self.cap.release()
                self.cap = None
                self.enable_color = False
            else:
                self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self.last_color_mono = 0.0
                threading.Thread(target=self._color_loop, daemon=True).start()
        self.create_timer(1.0 / PUB_HZ, self._tick)
        self.get_logger().info(
            f'astra_preview (OpenNI2 direct) up: depth 320x240 @{PUB_HZ}Hz, color={self.enable_color}')

    def _depth_loop(self):
        while self.run:
            try:
                f = self.depth.read_frame()
                a = np.frombuffer(f.get_buffer_as_uint16(), dtype=np.uint16)
                self.latest_depth = a.reshape(f.height, f.width).copy()
                self.depth_seq += 1
                self.last_depth_mono = time.monotonic()
            except Exception as e:                       # noqa: BLE001
                self.get_logger().error(f'depth read failed: {e}')
                self.depth_fatal = True
                return

    def _color_loop(self):
        while self.run:
            ok, img = self.cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            now = time.monotonic()
            if now - self.last_color_mono < 1.0 / PUB_HZ:   # throttle to ~PUB_HZ
                continue
            self.last_color_mono = now
            if img.shape[1] > COLOR_W:                      # downscale for rosbridge
                img = cv2.resize(img, (COLOR_W, int(img.shape[0] * COLOR_W / img.shape[1])))
            self._emit(self.pub_color, img)

    def _emit(self, pub, bgr):
        ok, jpg = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
        if not ok:
            return
        m = CompressedImage()
        m.header.stamp = self.get_clock().now().to_msg()
        m.format = 'jpeg'
        m.data = jpg.tobytes()
        pub.publish(m)

    def _fatal(self, why):
        # The depth device is dead/stalled. Exit non-zero so systemd's
        # Restart=on-failure rebuilds a clean OpenNI process (also recovers
        # hot-unplug). os._exit avoids unloading native libs while the read
        # thread may still be inside OpenNI.
        self.get_logger().error(f'{why} — exiting for systemd restart')
        os._exit(1)

    def _tick(self):
        now = time.monotonic()
        if self.depth_fatal:
            self._fatal('depth thread died')
        if self.last_depth_mono is None:
            if now - self.start_mono > FIRST_FRAME_GRACE:
                self._fatal('no depth frame after startup')
            return
        if now - self.last_depth_mono > STALE_TIMEOUT:
            self._fatal('depth stalled')
        if self.depth_seq == self.last_pub_seq:          # no new frame -> don't re-send
            return
        self.last_pub_seq = self.depth_seq
        d = self.latest_depth
        color = cv2.applyColorMap(DEPTH_LUT[d], cv2.COLORMAP_JET)
        color[d == 0] = 0                                # no-return pixels -> black
        self._emit(self.pub_depth, color)

    def shutdown(self):
        self.run = False
        try:
            self.depth.stop()
        except Exception:                                # noqa: BLE001
            pass
        try:
            openni2.unload()
        except Exception:                                # noqa: BLE001
            pass
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:                            # noqa: BLE001
                pass


def main():
    rclpy.init()
    node = None
    try:
        node = AstraPreview()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.shutdown()
            node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
