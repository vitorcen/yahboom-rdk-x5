#!/usr/bin/env python3
"""GS130WI (dual SC132GS global-shutter) stereo publisher — BPU depth edition.

STAGE 3 — COLOR + BPU DEPTH (2026-07-18):
  stereo_capture (C daemon, spawned here) -> /dev/shm/stereo_cam{0,1}.nv12 @60fps
    cam0 = 0x30 = calibration cam1 (reference/left eye)
    cam1 = 0x32 = calibration cam2 (right eye)
  color : cam0 NV12 -> per-plane downscale -> WB -> JPEG -> /image_jpeg   (GUI right)
  depth : per-plane rectify (crop+iso-scale folded into target projection)
          -> 640x704 nv12 combine [Y_L][Y_R][UV_L][UV_R] -> /image_combine_raw
          -> hobot_stereonet (BPU DStereoV2.4_int8, spawned here, calib none)
          -> /StereoNetNode/stereonet_visual (bgr8) -> JPEG
          -> /camera/depth/color_jpeg                                     (GUI left)
  DEPTH_BACKEND=sgbm env falls back to the old CPU SGBM path (kept for rescue).

Geometry (codex-review fix: no anisotropic squash): stereoRectify at native
1088x1280, then a custom target projection P' = iso-scale s=640/1088 with a
vertically centered crop (1280*s=753 -> 352, offset ~200). Maps go straight
from the native frame to 640x352 — one remap does undistort+rectify+crop+scale.
UV planes use the Y maps subsampled 2x (chroma accuracy is plenty).

Pairing: eyes free-run at 60fps (LPWM sync off), fixed phase offset measured
0-9ms per boot; pairs with |ts_l - ts_r| > 8ms are dropped (codex: don't let
the net see mismatched time). Message stamps are strictly increasing or
stereonet's publisher thread drops results.

Calibration from the module EEPROM at 0x50 ("UNION"), cached to CALIB_CACHE.
See .memory/rdk-x5-stereo-camera.md and docs/gs130wi-stereo-camera-bringup.html.
"""
import array
import os
import struct
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage, Image

W, H = 1088, 1280              # sc132gs native (portrait mount)
MW, MH = 640, 352              # stereonet model input per eye
COLOR_HZ = 12
COMBINE_HZ = 15                # feed rate for the BPU (model ceiling ~27)
OUT_W = 544                    # color preview width
JPEG_Q = 80
CAPTURE = ['/home/sunrise/nav_config/stereo_capture', '-s', '4', '-s', '5', '-f', '60']
CALIB_CACHE = '/home/sunrise/nav_config/stereo_calib.bin'
MAPS_BIN = '/home/sunrise/nav_config/rect_maps.bin'
COMBINE_NODE = '/home/sunrise/nav_config/stereo_combine_node'
BACKEND = os.environ.get('DEPTH_BACKEND', 'stereonet')
STEREONET_MODEL = '/opt/tros/humble/share/hobot_stereonet/config/DStereoV2.4_int8.bin'

# 60fps free-run switch: stock init table leaves the sensor in external-trigger
# mode (vts=0x3fff, rate = LPWM = 30fps) which cannot follow a 60Hz trigger.
# Upstream 60fps master table differs only in timing regs + vts=1400 +
# trigger-mode OFF (0x3222=0); PLL/MIPI rate identical -> hot-patch over i2c.
_SEQ_60FPS = [(0x0100, 0x00), (0x3201, 0x02), (0x3203, 0x02), (0x3205, 0x55),
              (0x3207, 0x15), (0x3213, 0x0c), (0x320e, 0x05), (0x320f, 0x78),
              (0x3222, 0x00), (0x0100, 0x01)]


def _i2c(bus, args):
    return subprocess.run(['i2ctransfer', '-y', str(bus)] + args,
                          capture_output=True, text=True, timeout=2)


def bump_sensors_60(logger):
    for addr in ('0x30', '0x32'):
        for bus in (4, 6):
            r = _i2c(bus, [f'w2@{addr}', '0x31', '0x07', 'r1'])
            if r.returncode == 0 and r.stdout.strip() == '0x01':
                for reg, val in _SEQ_60FPS:
                    _i2c(bus, [f'w3@{addr}', f'0x{reg >> 8:02x}',
                               f'0x{reg & 0xff:02x}', f'0x{val:02x}'])
                logger.info(f'sensor {addr}@i2c-{bus} switched to 60fps free-run')
                break
        else:
            logger.warn(f'sensor {addr} not found on i2c 4/6; stays 30fps')


def read_eeprom():
    for bus in (4, 6):
        try:
            chunks = []
            for off in range(0, 0x300, 32):
                out = subprocess.run(
                    ['i2ctransfer', '-y', str(bus), 'w2@0x50',
                     f'0x{off >> 8:02x}', f'0x{off & 0xff:02x}', 'r32'],
                    capture_output=True, text=True, timeout=2)
                if out.returncode != 0:
                    raise RuntimeError(out.stderr.strip())
                chunks += [int(x, 16) for x in out.stdout.split()]
            data = bytes(chunks)
            if data[:5] != b'UNION':
                raise RuntimeError('no UNION magic')
            with open(CALIB_CACHE, 'wb') as f:
                f.write(data)
            return data
        except Exception:
            continue
    if os.path.exists(CALIB_CACHE):
        return open(CALIB_CACHE, 'rb').read()
    raise RuntimeError('stereo calib EEPROM unreadable and no cache')


def parse_calib(data):
    def d(off):
        return struct.unpack('<d', data[off:off + 8])[0]

    def cam(base):
        fx, fy, cx, cy = (d(base + 8 * i) for i in range(4))
        dist = np.array([d(base + 8 * i) for i in range(4, 9)])
        return np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]]), dist

    K1, D1 = cam(0x18)
    K2, D2 = cam(0x81)
    R = np.array([d(0xEA + 8 * i) for i in range(9)]).reshape(3, 3)
    T = np.array([d(0xEA + 8 * (9 + i)) for i in range(3)]).reshape(3, 1)
    return K1, D1, K2, D2, R, T


def build_maps(K1, D1, K2, D2, R, T):
    """Rectify maps native->(MW,MH): iso-scale + centered vertical crop folded
    into the target projections. Returns maps + the intrinsics to hand to the
    stereonet node (fx, cx, cy, baseline, doffs)."""
    R1, R2, P1, P2, *_ = cv2.stereoRectify(K1, D1, K2, D2, (W, H), R, T, alpha=0)
    s = MW / W                                  # iso scale, fills width
    yoff = (H * s - MH) / 2.0                   # centered vertical crop

    def shrink(P):
        Pn = P.copy()
        Pn[0, :] *= s                           # fx, 0, cx, Tx*f
        Pn[1, 1] *= s
        Pn[1, 2] = P[1, 2] * s - yoff           # cy shifted by crop
        return Pn

    P1n, P2n = shrink(P1), shrink(P2)
    m1 = cv2.initUndistortRectifyMap(K1, D1, R1, P1n[:3, :3], (MW, MH), cv2.CV_32FC1)
    m2 = cv2.initUndistortRectifyMap(K2, D2, R2, P2n[:3, :3], (MW, MH), cv2.CV_32FC1)

    def uv(m):                                  # half-res maps for UV planes
        return (m[0][::2, ::2] * 0.5).copy(), (m[1][::2, ::2] * 0.5).copy()

    doffs = P2n[0, 2] - P1n[0, 2]               # 0 under CALIB_ZERO_DISPARITY
    baseline = abs(P2n[0, 3] / P2n[0, 0])
    return (m1, uv(m1)), (m2, uv(m2)), dict(
        fx=P1n[0, 0], fy=P1n[1, 1], cx=P1n[0, 2], cy=P1n[1, 2],
        baseline=baseline, doffs=doffs)


def export_maps(m1, m1uv, m2, m2uv):
    """Dump float32 maps for the C++ stereo_combine node (RMAP1 format)."""
    with open(MAPS_BIN, 'wb') as f:
        f.write(b'RMAP1')
        f.write(struct.pack('<2I', MW, MH))
        for arr in (m1[0], m1[1], m1uv[0], m1uv[1], m2[0], m2[1], m2uv[0], m2uv[1]):
            f.write(np.ascontiguousarray(arr, np.float32).tobytes())


_CHROMA = {'rb': None, 'cnt': None, 'up': None, 'vp': None}
_CHROMA_BINS = 24
_CHROMA_SAT = 1.6


def chroma_fix(uv, ow, oh):
    """De-contaminate NV12 chroma in place of white balance.

    No IR-cut filter on this module: indoor NIR floods R/B, showing up as a
    big additive chroma pedestal with a radial (lens shading) component.
    Subtract the per-radius mean U/V profile (EMA-smoothed), then boost the
    surviving chroma. Localized real colors survive; the veil does not.
    """
    c = _CHROMA
    u = uv[:, 0::2].astype(np.float32) - 128.0
    v = uv[:, 1::2].astype(np.float32) - 128.0
    if c['rb'] is None or c['rb'].shape != u.shape:
        h, w = u.shape
        yy, xx = np.mgrid[0:h, 0:w]
        rad = np.sqrt((yy - h / 2) ** 2 + (xx - w / 2) ** 2)
        c['rb'] = np.minimum((rad / rad.max() * _CHROMA_BINS).astype(np.int32),
                             _CHROMA_BINS - 1)
        c['cnt'] = np.bincount(c['rb'].ravel(), minlength=_CHROMA_BINS)
        c['up'] = c['vp'] = None
    flat = c['rb'].ravel()
    k = np.ones(3) / 3.0
    up = np.convolve(np.bincount(flat, u.ravel(), _CHROMA_BINS) / c['cnt'], k, 'same')
    vp = np.convolve(np.bincount(flat, v.ravel(), _CHROMA_BINS) / c['cnt'], k, 'same')
    if c['up'] is None:
        c['up'], c['vp'] = up, vp
    else:
        c['up'] += 0.3 * (up - c['up'])
        c['vp'] += 0.3 * (vp - c['vp'])
    u = (u - c['up'][c['rb']]) * _CHROMA_SAT
    v = (v - c['vp'][c['rb']]) * _CHROMA_SAT
    out = np.empty_like(uv)
    out[:, 0::2] = np.clip(u + 128.0, 0, 255).astype(np.uint8)
    out[:, 1::2] = np.clip(v + 128.0, 0, 255).astype(np.uint8)
    return out


PAIR_PATH = '/dev/shm/stereo_pair.shm'
FRAME_BYTES = W * H * 3 // 2


def read_pair():
    """Read stereo_capture's seqlocked pair file (rescue paths only; the C++
    node maps it directly). Returns (nv0, nv1, ts_seconds) or None."""
    try:
        buf = open(PAIR_PATH, 'rb').read()
        if len(buf) < 64 + 2 * FRAME_BYTES or buf[:4] != b'STPR':
            return None
        seq, = struct.unpack('<I', buf[4:8])
        if seq == 0 or seq & 1:                 # unwritten / torn
            return None
        ts0, ts1 = struct.unpack('<2Q', buf[16:32])
        nv0 = np.frombuffer(buf, np.uint8, FRAME_BYTES, 64).reshape(H * 3 // 2, W)
        nv1 = np.frombuffer(buf, np.uint8, FRAME_BYTES, 64 + FRAME_BYTES).reshape(H * 3 // 2, W)
        return nv0, nv1, max(ts0, ts1) / 1e9
    except OSError:
        return None


class StereoCam(Node):
    def __init__(self):
        super().__init__('stereo_cam')
        self.pub_color = self.create_publisher(CompressedImage, '/image_jpeg', 5)
        self.pub_depth = self.create_publisher(CompressedImage, '/camera/depth/color_jpeg', 5)

        K1, D1, K2, D2, R, T = parse_calib(read_eeprom())
        (self.m1, self.m1uv), (self.m2, self.m2uv), self.intr = \
            build_maps(K1, D1, K2, D2, R, T)
        self.get_logger().info(
            'calib ok: fx=%.2f cx=%.2f cy=%.2f baseline=%.4fm doffs=%.3f' %
            (self.intr['fx'], self.intr['cx'], self.intr['cy'],
             self.intr['baseline'], self.intr['doffs']))

        self.cap = subprocess.Popen(CAPTURE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        self.get_logger().info(f'stereo_capture spawned pid={self.cap.pid}')
        threading.Timer(6.0, bump_sensors_60, args=(self.get_logger(),)).start()

        self.out_size = (OUT_W, int(H * OUT_W / W))
        self.running = True
        self.last_stamp = 0.0
        # Hot paths live in the C++ stereo_combine node when its binary exists
        # (python color+combine threads share one GIL and cap around 11 it/s
        # total). Python keeps calib, process supervision and the SGBM rescue.
        self.use_cpp = (BACKEND == 'stereonet' and os.path.exists(COMBINE_NODE)
                        and os.environ.get('COMBINE_BACKEND', 'cpp') == 'cpp')
        if not self.use_cpp:
            threading.Thread(target=self.loop, args=(self.tick_color, COLOR_HZ), daemon=True).start()

        if BACKEND == 'stereonet':
            self.snet = self.spawn_stereonet()
            # visual (1.35MB bgr8) must NOT flow through rclpy: python-side CDR
            # deserialization hogs the GIL for ~800ms/frame and stalls our own
            # publish. hobot_codec (C++, HW JPEG) bridges it to the GUI topic.
            self.codec = self.spawn_codec()
            if self.use_cpp:
                export_maps(self.m1, self.m1uv, self.m2, self.m2uv)
                self.combine = self.spawn_combine()
                self.codec_color = self.spawn_codec_color()
            else:
                self.pub_combine = self.create_publisher(Image, '/image_combine_raw', 2)
                threading.Thread(target=self.loop, args=(self.tick_combine, COMBINE_HZ), daemon=True).start()
        else:                                   # DEPTH_BACKEND=sgbm rescue path
            self.sgbm = cv2.StereoSGBM_create(
                minDisparity=0, numDisparities=64, blockSize=5,
                P1=8 * 25, P2=32 * 25, uniquenessRatio=10,
                speckleWindowSize=100, speckleRange=2, disp12MaxDiff=1,
                mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
            threading.Thread(target=self.loop, args=(self.tick_sgbm, 5), daemon=True).start()
        self.create_timer(5.0, self.tick_watchdog)

    def spawn_stereonet(self):
        i = self.intr
        cmd = ['ros2', 'launch', 'hobot_stereonet', 'stereonet_model.launch.py',
               f'stereonet_model_file_path:={STEREONET_MODEL}',
               'calib_method:=none',
               f'camera_fx:={i["fx"]:.4f}', f'camera_fy:={i["fy"]:.4f}',
               f'camera_cx:={i["cx"]:.4f}', f'camera_cy:={i["cy"]:.4f}',
               f'baseline:={i["baseline"]:.5f}', f'doffs:={i["doffs"]:.4f}',
               'publish_pcd_enabled:=False', 'publish_origin_enable:=False',
               'infer_thread_num:=4', 'log_level:=warn']
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=dict(os.environ))
        self.get_logger().info(f'stereonet spawned pid={p.pid}')
        return p

    def spawn_combine(self):
        # sparse logs (10s stats) -> let them flow to the journal
        p = subprocess.Popen([COMBINE_NODE], env=dict(os.environ))
        self.get_logger().info(f'stereo_combine spawned pid={p.pid}')
        return p

    def spawn_codec(self):
        cmd = ['ros2', 'launch', 'hobot_codec', 'hobot_codec_encode.launch.py',
               'codec_in_mode:=ros', 'codec_out_mode:=ros',
               'codec_sub_topic:=/StereoNetNode/stereonet_visual',
               'codec_in_format:=bgr8', 'codec_out_format:=jpeg',
               'codec_pub_topic:=/camera/depth/color_jpeg', 'log_level:=warn']
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=dict(os.environ))
        self.get_logger().info(f'hobot_codec spawned pid={p.pid}')
        return p

    def spawn_codec_color(self):
        # second VPU JENC channel: color NV12 -> /image_jpeg. Software imencode
        # in the combine node was ~30ms/frame of CPU stolen from the depth chain.
        cmd = ['ros2', 'launch', 'hobot_codec', 'hobot_codec_encode.launch.py',
               'codec_channel:=2', 'codec_in_mode:=ros', 'codec_out_mode:=ros',
               'codec_sub_topic:=/image_color_nv12',
               'codec_in_format:=nv12', 'codec_out_format:=jpeg',
               'codec_jpg_quality:=80.0',
               'codec_pub_topic:=/image_jpeg', 'log_level:=warn']
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=dict(os.environ))
        self.get_logger().info(f'hobot_codec(color) spawned pid={p.pid}')
        return p

    def loop(self, fn, hz):
        period = 1.0 / hz
        while self.running:
            t0 = time.time()
            try:
                fn()
            except Exception as e:
                self.get_logger().warn(f'{fn.__name__}: {e}')
            time.sleep(max(0.005, period - (time.time() - t0)))

    # --- color: reference eye, per-plane downscale then convert ---
    def tick_color(self):
        r = read_pair()
        if not r:
            return
        nv12 = r[0]
        ow, oh = self.out_size
        y = cv2.resize(nv12[:H], (ow, oh), interpolation=cv2.INTER_AREA)
        uv = cv2.resize(nv12[H:], (ow, oh // 2), interpolation=cv2.INTER_AREA)
        uv = chroma_fix(uv, ow, oh)
        bgr = cv2.cvtColor(np.vstack([y, uv]), cv2.COLOR_YUV2BGR_NV12)
        self.pub_jpeg(self.pub_color, bgr, 'sc132gs_color')

    # --- combine: per-plane rectified pair for the BPU ---
    def rect_eye(self, nv, maps, uvmaps):
        y = cv2.remap(nv[:H], maps[0], maps[1], cv2.INTER_LINEAR)
        uvsrc = np.ascontiguousarray(nv[H:]).reshape(H // 2, W // 2, 2)
        uv = cv2.remap(uvsrc, uvmaps[0], uvmaps[1], cv2.INTER_LINEAR)
        return y, uv.reshape(MH // 2, MW)

    def tick_combine(self):
        # Pairing now happens inside stereo_capture (seqlocked pair file);
        # here we just skip when there's nothing newer than last publish.
        r = read_pair()
        st = getattr(self, '_cstat', None)
        if st is None:
            st = self._cstat = {'try': 0, 'ok': 0, 't': time.time(), 'el': 0.0}
        st['try'] += 1
        if not r or r[2] <= self.last_stamp:
            if time.time() - st['t'] > 5:
                self.get_logger().info(f"combine stat: try={st['try']} ok={st['ok']} last_el={st['el']*1e3:.0f}ms")
                st['t'] = time.time(); st['try'] = st['ok'] = 0
            return
        _t0 = time.time()
        yl, uvl = self.rect_eye(r[0], self.m1, self.m1uv)
        yr, uvr = self.rect_eye(r[1], self.m2, self.m2uv)
        _t1 = time.time()
        msg = Image()
        stamp = r[2]
        if stamp <= self.last_stamp:            # stereonet drops non-increasing
            stamp = self.last_stamp + 1e-4
        self.last_stamp = stamp
        msg.header.stamp.sec = int(stamp)
        msg.header.stamp.nanosec = int((stamp % 1) * 1e9)
        msg.header.frame_id = 'stereo'
        msg.height = MH * 2                     # 704 = logical Y height, no UV rows
        msg.width = MW
        msg.encoding = 'nv12'
        msg.step = MW
        buf = np.concatenate(                   # [Y_L][Y_R][UV_L][UV_R]
            [yl.reshape(-1), yr.reshape(-1), uvl.reshape(-1), uvr.reshape(-1)])
        # rclpy's Image.data setter takes the fast path (zero-check reference)
        # ONLY for a matching array.array; assigning bytes walks a per-byte
        # python loop = ~700ms for 675KB (measured with py-spy).
        a8 = array.array('B')
        a8.frombytes(buf.tobytes())
        msg.data = a8
        self.pub_combine.publish(msg)
        st['ok'] += 1
        st['el'] = time.time() - _t0
        st['rect'] = _t1 - _t0
        st['pub'] = time.time() - _t1
        if time.time() - st['t'] > 5:
            self.get_logger().info(
                f"combine stat: try={st['try']} ok={st['ok']} el={st['el']*1e3:.0f}ms "
                f"rect={st.get('rect',0)*1e3:.0f}ms pub={st.get('pub',0)*1e3:.0f}ms")
            st['t'] = time.time(); st['try'] = st['ok'] = 0

    # --- rescue: CPU SGBM (DEPTH_BACKEND=sgbm) ---
    def tick_sgbm(self):
        r = read_pair()
        if not r:
            return
        yl, _ = self.rect_eye(r[0], self.m1, self.m1uv)
        yr, _ = self.rect_eye(r[1], self.m2, self.m2uv)
        disp = self.sgbm.compute(yl, yr).astype(np.float32) / 16.0
        dv = np.clip(disp / 64 * 255, 0, 255).astype(np.uint8)
        color = cv2.applyColorMap(dv, cv2.COLORMAP_JET)
        color[disp <= 0] = 0
        self.pub_jpeg(self.pub_depth, color, 'sgbm_depth')

    def tick_watchdog(self):
        if self.cap.poll() is not None:
            self.get_logger().warn('stereo_capture died, respawning')
            self.cap = subprocess.Popen(CAPTURE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            threading.Timer(6.0, bump_sensors_60, args=(self.get_logger(),)).start()
        if BACKEND == 'stereonet':
            if self.snet.poll() is not None:
                self.get_logger().warn('stereonet died, respawning')
                self.snet = self.spawn_stereonet()
            if self.codec.poll() is not None:
                self.get_logger().warn('hobot_codec died, respawning')
                self.codec = self.spawn_codec()
            if self.use_cpp and self.combine.poll() is not None:
                self.get_logger().warn('stereo_combine died, respawning')
                self.combine = self.spawn_combine()
            if self.use_cpp and self.codec_color.poll() is not None:
                self.get_logger().warn('hobot_codec(color) died, respawning')
                self.codec_color = self.spawn_codec_color()

    _FPS = {}

    def _stamp_fps(self, bgr, frame_id):
        # rolling publish-rate, drawn into the frame corner (user-visible truth)
        now = time.time()
        q = self._FPS.setdefault(frame_id, [])
        q.append(now)
        while q and now - q[0] > 3.0:
            q.pop(0)
        fps = (len(q) - 1) / (now - q[0]) if len(q) > 1 else 0.0
        txt = f'{fps:.1f}fps'
        cv2.putText(bgr, txt, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4)
        cv2.putText(bgr, txt, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 80), 2)
        return bgr

    def pub_jpeg(self, pub, bgr, frame_id):
        bgr = self._stamp_fps(bgr.copy() if not bgr.flags.writeable else bgr, frame_id)
        ok, jpg = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
        if not ok:
            return
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = frame_id
        msg.format = 'jpeg'
        msg.data = jpg.tobytes()
        pub.publish(msg)

    def destroy_node(self):
        self.running = False
        for p in (getattr(self, 'combine', None), getattr(self, 'codec_color', None),
                  getattr(self, 'codec', None), getattr(self, 'snet', None), self.cap):
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                pass
        super().destroy_node()


def main():
    rclpy.init()
    node = StereoCam()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    sys.exit(main())
