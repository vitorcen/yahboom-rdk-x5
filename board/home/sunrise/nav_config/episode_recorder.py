#!/usr/bin/env python3
"""Episode recorder: gamepad/GUI-toggled rosbag2 capture for dataset building.

Single owner of the recording state (same pattern as safety_stop's toggle):
external parties only publish /record_toggle (Empty) or the idempotent
/record_stop (Empty, used by the stop-all chain so it can never START a
recording by accident); the latched /recording (Bool) broadcast is what the
GUI button and any other mirror display.

Each episode is a directory <data_dir>/ep_YYYYmmdd_HHMMSS/ holding the
rosbag2 output plus our meta.yaml. While recording (or after a crash) the
directory carries a .partial suffix; the rename to its final name happens
only after the bag process exits and meta.yaml is finalized, so incomplete
episodes are recognizable by name alone.

Guards: refuses to start (rapid beeps) when the disk is below min_free_gb,
re-checks every 30 s while recording and auto-stops when space runs out;
key topics missing at start are logged and listed in meta.yaml rather than
silently producing a dataset with holes.
"""
import datetime
import os
import shutil
import signal
import subprocess

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy
from std_msgs.msg import Bool, Empty

TOPICS = [
    '/scan', '/odom', '/tf', '/tf_static',
    '/cmd_vel', '/cmd_vel_joy', '/cmd_vel_follow', '/cmd_vel_mux',
    '/cmd_vel_drv', '/joy', '/safety_enabled',
]
IMAGE_TOPIC = '/image_jpeg'
# Topics whose absence means the episode is not worth training on.
KEY_TOPICS = ['/scan', '/odom', '/cmd_vel_mux', '/cmd_vel_drv']
MAX_BAG_SIZE = 1 << 30          # 1 GiB splits: 512 MB splits under load
                                # risk message loss (rosbag2#2108)
DISK_CHECK_PERIOD = 30.0        # s

# transient_local topics need explicit overrides or their latched history
# is recorded with volatile QoS and the pre-start messages are lost.
QOS_OVERRIDES = """/tf_static:
  reliability: reliable
  durability: transient_local
  history: keep_all
/safety_enabled:
  reliability: reliable
  durability: transient_local
  history: keep_all
"""

BEEP_START = [(True, .12), (False, .12)] * 3            # three short
BEEP_STOP = [(True, .12), (False, .12), (True, .5), (False, .1)]
BEEP_REFUSE = [(True, .08), (False, .08)] * 5           # rapid = rejected


class EpisodeRecorder(Node):
    def __init__(self):
        super().__init__('episode_recorder')
        self.declare_parameter('data_dir', '/home/sunrise/episodes')
        self.declare_parameter('min_free_gb', 4.0)
        self.declare_parameter('record_image', True)
        # Hard cap per episode; sampled at start, GUI changes it via the
        # standard parameter service (applies from the next recording).
        self.declare_parameter('max_duration_s', 180.0)
        self.data_dir = self.get_parameter('data_dir').value
        os.makedirs(self.data_dir, exist_ok=True)
        self.qos_path = os.path.join(self.data_dir, 'qos_overrides.yaml')
        with open(self.qos_path, 'w') as f:
            f.write(QOS_OVERRIDES)

        latched = QoSProfile(depth=1,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.pub_state = self.create_publisher(Bool, '/recording', latched)
        self.pub_beep = self.create_publisher(Bool, '/Buzzer', 10)
        self.create_subscription(Empty, '/record_toggle', self.on_toggle, 10)
        self.create_subscription(Empty, '/record_stop', self.on_stop_req, 10)

        self.proc = None            # ros2 bag record subprocess
        self.ep_dir = None          # .partial directory while recording
        self.meta = None
        self.started = None         # datetime of start
        self.disk_timer = None
        self.beep_seq = []
        self.beep_timer = None
        self.pub_state.publish(Bool(data=False))    # boot default: idle
        self.get_logger().info(f'idle, episodes -> {self.data_dir}')

    # -- control ----------------------------------------------------------
    def on_toggle(self, _):
        if self.proc:
            self.stop('toggle')
        else:
            self.start()

    def on_stop_req(self, _):
        # Idempotent: the stop-all chain fires this blindly; when idle it
        # must be a no-op, never a start (a toggle here would arm instead).
        if self.proc:
            self.stop('stop_all')

    def start(self):
        if self.free_gb() < self.get_parameter('min_free_gb').value:
            self.get_logger().error('refusing to record: disk low')
            self.beep(BEEP_REFUSE)
            return
        topics = list(TOPICS)
        if self.get_parameter('record_image').value:
            topics.append(IMAGE_TOPIC)
        live = {t for t, _ in self.get_topic_names_and_types()}
        missing = [t for t in topics if t not in live]
        if missing:
            self.get_logger().warn(f'recording despite missing: {missing}')

        self.started = datetime.datetime.now().astimezone()
        name = self.started.strftime('ep_%Y%m%d_%H%M%S')
        self.ep_dir = os.path.join(self.data_dir, name + '.partial')
        os.makedirs(self.ep_dir)
        self.meta = {
            'episode': name,
            'start': self.started.isoformat(timespec='seconds'),
            'missing_topics': missing,
            'task': None, 'success': None, 'notes': None,
        }
        # Own session so launch's signals never reach the bag process; we
        # alone decide when it gets SIGINT and finalizes its metadata.
        self.proc = subprocess.Popen(
            ['ros2', 'bag', 'record', '-o', os.path.join(self.ep_dir, 'bag'),
             '--max-bag-size', str(MAX_BAG_SIZE),
             '--qos-profile-overrides-path', self.qos_path] + topics,
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.disk_timer = self.create_timer(DISK_CHECK_PERIOD, self.check_disk)
        self.max_timer = self.create_timer(
            max(self.get_parameter('max_duration_s').value, 1.0),
            lambda: self.stop('max_duration'))
        self.pub_state.publish(Bool(data=True))
        self.beep(BEEP_START)
        self.get_logger().info(f'recording {name}' +
                               (f' (missing {missing})' if missing else ''))

    def stop(self, stopped_by):
        proc, self.proc = self.proc, None
        self.disk_timer.cancel()
        self.max_timer.cancel()
        self.pub_state.publish(Bool(data=False))
        try:
            os.killpg(proc.pid, signal.SIGINT)   # rosbag2 finalizes on SIGINT
            proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                pass
        dur = (datetime.datetime.now().astimezone()
               - self.started).total_seconds()
        self.meta.update(duration_s=round(dur, 1), stopped_by=stopped_by)
        self.write_meta()
        final = self.ep_dir[:-len('.partial')]
        os.rename(self.ep_dir, final)            # only finished bags lose .partial
        self.ep_dir = None
        self.beep(BEEP_STOP)
        self.get_logger().info(
            f'saved {os.path.basename(final)} ({dur:.0f}s, by {stopped_by})')

    # -- guards & helpers --------------------------------------------------
    def free_gb(self):
        return shutil.disk_usage(self.data_dir).free / 1e9

    def check_disk(self):
        if self.proc and self.free_gb() < self.get_parameter('min_free_gb').value:
            self.get_logger().error('disk low mid-recording: stopping')
            self.stop('disk_full')

    def write_meta(self):
        lines = []
        for k, v in self.meta.items():
            if isinstance(v, list):
                v = '[' + ', '.join(v) + ']'
            elif isinstance(v, bool):
                v = str(v).lower()
            elif v is None:
                v = 'null'
            lines.append(f'{k}: {v}')
        tmp = os.path.join(self.ep_dir, 'meta.yaml.tmp')
        with open(tmp, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        os.replace(tmp, os.path.join(self.ep_dir, 'meta.yaml'))

    def beep(self, seq):
        if self.beep_timer:
            self.beep_timer.cancel()
            self.pub_beep.publish(Bool(data=False))
        self.beep_seq = list(seq)
        self.beep_step()

    def beep_step(self):
        if self.beep_timer:
            self.beep_timer.cancel()
            self.beep_timer = None
        if not self.beep_seq:
            return
        state, dur = self.beep_seq.pop(0)
        self.pub_beep.publish(Bool(data=state))
        self.beep_timer = self.create_timer(dur, self.beep_step)


def main():
    rclpy.init()
    node = EpisodeRecorder()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node.proc:                # launch shutdown while recording:
            node.stop('shutdown')    # finalize instead of orphaning the bag


if __name__ == '__main__':
    main()
