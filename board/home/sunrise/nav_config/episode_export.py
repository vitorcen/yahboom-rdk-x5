#!/usr/bin/env python3
"""Export a rosbag2 episode into tensor-friendly artifacts (CSV + JPEG frames).

Single source of truth on the board, shared by episode_lab.ipynb and the GUI.

Usage:
    python3 episode_export.py <ep_name> [--preview] [--force]

Episodes live under EP_ROOT (/home/sunrise/episodes). This script does NOT
source the ROS environment itself — the caller must do that first, e.g.
    source /opt/tros/humble/setup.bash

Modes:
    full (default): artifacts written directly into the episode dir
        (odom.csv, cmd_vel*.csv, scan.csv, frames/ with every frame).
    --preview:      artifacts written into <ep>/preview/
        (same CSVs, which are small anyway, plus preview/frames/ with an
        evenly-spaced subsample of at most PREVIEW_MAX_FRAMES frames and
        preview/manifest.yaml). Built in preview.tmp then atomically renamed
        to preview so no half-baked dir is ever left behind. Idempotent:
        skips if preview/ already exists unless --force is given.

CDR note: /image_jpeg, /odom, /cmd_vel*, /scan are all CDR-serialized. We must
deserialize_message and read the message fields — the raw sqlite blob is the
whole CDR message, NOT a bare JPEG. Only m.data holds the real JPEG bytes.
"""

import os
import sys
import csv
import glob
import math
import shutil
from datetime import datetime, timezone

EP_ROOT = "/home/sunrise/episodes"
CMD_TOPICS = ["/cmd_vel", "/cmd_vel_joy", "/cmd_vel_follow", "/cmd_vel_mux", "/cmd_vel_drv"]
SCAN_BINS = 36          # angular downsample: 36 bins per scan
SCAN_EVERY = 5          # temporal downsample: keep 1 of every 5 scans
PREVIEW_MAX_FRAMES = 150


def yaw_from_quat(z, w):
    return math.atan2(2.0 * w * z, 1.0 - 2.0 * z * z)   # planar yaw


def find_bag(ep_dir):
    """Return (bag_uri, storage_id) for the bag containing metadata.yaml."""
    md = glob.glob(os.path.join(ep_dir, "**", "metadata.yaml"), recursive=True)
    if not md:
        return None, None
    import yaml
    with open(md[0]) as f:
        meta = yaml.safe_load(f) or {}
    storage_id = (meta.get("rosbag2_bagfile_information", {})
                      .get("storage_identifier") or "sqlite3")
    return os.path.dirname(md[0]), storage_id


def open_reader(bag_uri, storage_id):
    import rosbag2_py
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=bag_uri, storage_id=storage_id),
                rosbag2_py.ConverterOptions("", ""))
    typemap = {t.name: t.type for t in reader.get_all_topics_and_types()}
    return reader, typemap


def count_frames(bag_uri, storage_id):
    """Cheap first pass: count /image_jpeg messages without deserializing."""
    reader, _ = open_reader(bag_uri, storage_id)
    n = 0
    while reader.has_next():
        topic, _data, _t = reader.read_next()
        if topic == "/image_jpeg":
            n += 1
    return n


def export(bag_uri, storage_id, out_dir, keep_frame):
    """Write CSVs + frames into out_dir. keep_frame(idx) decides which frames
    land in out_dir/frames/. Returns (counts, frames_written)."""
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    reader, typemap = open_reader(bag_uri, storage_id)

    f_odom = open(os.path.join(out_dir, "odom.csv"), "w", newline="")
    w_odom = csv.writer(f_odom)
    w_odom.writerow(["t", "x", "y", "yaw", "vx", "vy", "wz"])
    f_scan = open(os.path.join(out_dir, "scan.csv"), "w", newline="")
    w_scan = csv.writer(f_scan)
    cmd_w = {}
    for t in CMD_TOPICS:
        fh = open(os.path.join(out_dir, t.strip("/").replace("/", "_") + ".csv"), "w", newline="")
        wr = csv.writer(fh)
        wr.writerow(["t", "vx", "vy", "wz"])
        cmd_w[t] = (fh, wr)

    counts = {}
    scan_hdr_done = False
    scan_i = 0
    img_i = 0          # index over all /image_jpeg messages
    frame_names = []   # basenames of frames written, in time order

    while reader.has_next():
        topic, data, t_ns = reader.read_next()
        counts[topic] = counts.get(topic, 0) + 1
        ts = t_ns / 1e9
        if topic == "/odom":
            m = deserialize_message(data, get_message(typemap[topic]))
            p, o, v = m.pose.pose.position, m.pose.pose.orientation, m.twist.twist
            w_odom.writerow([f"{ts:.6f}", f"{p.x:.4f}", f"{p.y:.4f}",
                             f"{yaw_from_quat(o.z, o.w):.4f}",
                             f"{v.linear.x:.4f}", f"{v.linear.y:.4f}", f"{v.angular.z:.4f}"])
        elif topic in cmd_w:
            m = deserialize_message(data, get_message(typemap[topic]))
            cmd_w[topic][1].writerow([f"{ts:.6f}", f"{m.linear.x:.4f}",
                                      f"{m.linear.y:.4f}", f"{m.angular.z:.4f}"])
        elif topic == "/scan":
            scan_i += 1
            if scan_i % SCAN_EVERY:
                continue
            m = deserialize_message(data, get_message(typemap[topic]))
            rng = list(m.ranges)
            if not scan_hdr_done:
                w_scan.writerow(["t"] + [f"r{i}" for i in range(SCAN_BINS)])
                scan_hdr_done = True
            step = max(1, len(rng) // SCAN_BINS)
            samp = rng[::step][:SCAN_BINS]
            samp += [""] * (SCAN_BINS - len(samp))
            w_scan.writerow([f"{ts:.6f}"] + [("" if (x == "" or math.isinf(x) or math.isnan(x))
                                              else f"{x:.3f}") for x in samp])
        elif topic == "/image_jpeg":
            idx = img_i
            img_i += 1
            if not keep_frame(idx):
                continue
            # deserialize CDR -> m.data is the real JPEG byte string (no re-encode)
            m = deserialize_message(data, get_message(typemap[topic]))
            name = f"frame_{idx:05d}_{t_ns}.jpg"
            with open(os.path.join(frames_dir, name), "wb") as imgf:
                imgf.write(bytes(m.data))
            frame_names.append(name)

    for fh in [f_odom, f_scan] + [v[0] for v in cmd_w.values()]:
        fh.close()
    return counts, frame_names


def print_summary(counts, frame_names, bag_uri, storage_id):
    print("bag_uri:", bag_uri, "| storage:", storage_id)
    print("frames written:", len(frame_names))
    for k in sorted(counts):
        print(f"  {k}: {counts[k]}")


def run_full(ep_dir, bag_uri, storage_id):
    counts, frame_names = export(bag_uri, storage_id, ep_dir, lambda i: True)
    print_summary(counts, frame_names, bag_uri, storage_id)
    print("EXPORT_OK")


def run_preview(ep_dir, bag_uri, storage_id, force):
    preview = os.path.join(ep_dir, "preview")
    if os.path.isdir(preview):
        if not force:
            print(f"preview/ already exists: {preview} (use --force to rebuild)")
            print("EXPORT_OK")
            return
        shutil.rmtree(preview)

    frame_total = count_frames(bag_uri, storage_id)
    stride = max(1, math.ceil(frame_total / PREVIEW_MAX_FRAMES)) if frame_total else 1

    tmp = os.path.join(ep_dir, "preview.tmp")
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)

    counts, frame_names = export(bag_uri, storage_id, tmp, lambda i: i % stride == 0)

    manifest = os.path.join(tmp, "manifest.yaml")
    with open(manifest, "w") as f:
        f.write(f"frame_total: {frame_total}\n")
        f.write(f"frame_stride: {stride}\n")
        f.write(f"frame_count: {len(frame_names)}\n")
        f.write(f"generated_at: {datetime.now(timezone.utc).isoformat()}\n")
        # relative basenames under preview/frames/, time order — the frontend
        # enumerates frames from this list (it has no directory-listing channel).
        f.write("files:\n")
        for name in frame_names:
            f.write(f"- {name}\n")

    os.rename(tmp, preview)   # atomic: no half-baked preview/ ever visible
    print_summary(counts, frame_names, bag_uri, storage_id)
    print(f"preview: {preview} (total {frame_total}, stride {stride}, kept {len(frame_names)})")
    print("EXPORT_OK")


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    if not args:
        print("usage: episode_export.py <ep_name> [--preview] [--force]")
        return 2
    ep = args[0]
    ep_dir = ep if os.path.isabs(ep) else os.path.join(EP_ROOT, ep)
    if not os.path.isdir(ep_dir):
        print(f"NO_EPISODE: {ep_dir}")
        return 2

    bag_uri, storage_id = find_bag(ep_dir)
    if not bag_uri:
        print("NO_METADATA: broken or empty episode, nothing to export")
        return 2

    if "--preview" in flags:
        run_preview(ep_dir, bag_uri, storage_id, "--force" in flags)
    else:
        run_full(ep_dir, bag_uri, storage_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
