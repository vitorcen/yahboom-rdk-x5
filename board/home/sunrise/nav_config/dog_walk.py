#!/usr/bin/env python3
"""Dog-walk mode: memoryless reactive wander/sniff on /scan -> /cmd_vel_dog.

The car ambles like a dog on a lead: trots toward the most-open sector,
slows and "sniffs" an isolated small obstacle (a table/chair leg), backs off
a step and turns to the next spot. No map, no odom, no Nav2 — just single
frames of the MS200 lidar. It rides the mux at the LOWEST priority
(/cmd_vel_dog, P3): any human/follow/Nav2 command overrides it, and grabbing
the joystick makes it EXIT (a person taking over means the behavior was
already wrong — resuming autonomy needs a fresh R1).

Why fail-CLOSE (opposite of safety_stop): safety_stop keeps the joystick
usable when the lidar dies, so it fails OPEN. Dog-walk is UNSUPERVISED motion,
so every state first checks scan freshness and stops on stale/blind data;
persistent loss exits active. It also REFUSES to start while the lidar brake
is off — the one autonomous mode that must not inherit A-key's fail-open.

Boot state is idle and stateless-ish (respawn-safe): active is held state,
flipped by /dog_toggle, forced off by /dog_stop (idempotent). Beeps follow the
joy_teleop/episode_recorder /Buzzer convention: two short = start, one long =
stop/timeout, three short = refuse-start.
"""
import math
import random

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, qos_profile_sensor_data
from action_msgs.srv import CancelGoal
from geometry_msgs.msg import Twist
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool, Empty

RATE = 10.0                     # Hz, same as the MS200; mux watchdog is 0.5 s
SCAN_FRESH = 0.4                # s, scan older than this -> stop this tick
SCAN_LOST = 1.5                 # s, stale this long -> exit active (fail-close)
HARD_TIMEOUT = 600.0            # s, unconditional auto-stop (10 min of roaming)
RANGE_VALID = 0.05              # ranges <= this (or NaN/Inf) are blocked/invalid

FRONT_HALF = math.radians(30)   # half-angle of the front sector for clearance
APPROACH_V = 0.15               # m/s near an object, slow enough to "sniff"
APPROACH_STOP = 0.32            # m, lidar range at stop = ~20cm at the bumper
                               # (lidar sits mid-body); hugs safety_stop's 0.3 m
                               # floor so it sniffs close without lowering the brake
APPROACH_MAX = 6.0              # s, give up an approach that drags on
LOST_T = 0.5                    # s, target unmatched this long -> abandon
K_YAW = 1.9                     # rad/s per rad of bearing error (snappier steer)
WZ_MAX = 1.6                    # rad/s, wander/approach steering cap (livelier)

# Isolated small-obstacle ("leg") detection.
LEG_ARC = math.radians(70)      # forward ±arc scanned for leg-like clusters
LEG_NEAR, LEG_FAR = 0.2, 1.2    # m, a leg lives in this range band
LEG_STEP = 0.3                  # m, a range jump this big bounds a cluster
LEG_WMIN, LEG_WMAX = 0.02, 0.30 # m, accepted PHYSICAL width (not angular)
LEG_MIN_PTS = 2                 # a one-beam blip is noise, not an object
LEG_FRAMES = 3                  # same candidate must persist this many frames
MATCH_ANG = math.radians(15)    # bearing tolerance for "same candidate"
MATCH_RNG = 0.3                 # m, range tolerance for "same candidate"

SNIFF_T = 3.0                   # s, dwell time "sniffing"
SNIFF_WAG = 0.5                 # rad/s, head-wag amplitude
SNIFF_F = 0.5                   # Hz, wag frequency

REAR_HALF = math.radians(30)    # half-angle of the rear clearance check
REAR_MIN = 0.3                  # m, back up only if the rear is clearer
BACKUP_V = 0.15                 # m/s reverse
BACKUP_T = 0.8                  # s (~0.12 m); no guard on P3 lets vx<0 through

TURN_WZ = 1.3                   # rad/s in-place turn (no odom: time = angle/wz)
TURN_AWAY = math.radians(30)    # never turn back toward the just-sniffed bearing
TURN_OPEN_MIN = 0.5             # m, a sector must be at least this open to pick
TURN_OPEN_CAP = 3.0             # m, weight for an empty (no-return) sector

DRIFT_STEP = math.radians(3)    # rad/tick random walk of the wander drift
DRIFT_MAX = math.radians(45)    # rad, drift clamp (wider -> roams a bigger area)
TB_LP = 0.80                    # target-bearing low-pass (lower = snappier heading)
# Random exploration: instead of always beelining to the single most-open
# sector (too deterministic), re-pick a WEIGHTED-RANDOM open direction at random
# intervals — the path wanders like a dog while still only choosing open sectors.
RETARGET_MIN, RETARGET_MAX = 2.0, 5.0   # s, interval between random re-targets

# Gait: a triangle-wave pace so the trot speed ramps up and down at a CONSTANT
# rate (uniform accel/decel, dog-like) instead of holding one speed. It scales
# the clearance-limited wander speed between GAIT_MIN and full.
GAIT_PERIOD = 8.0               # s, one accelerate-then-decelerate cycle (peppier)
GAIT_MIN = 0.72                 # slowest pace as a fraction of the open speed
SUPPRESS_HALF = math.radians(30)  # ± suppression cone around the sniffed bearing
SUPPRESS_T = 15.0               # s, cooldown suppressing re-approach of THAT bearing

BEEP_START = [(True, .12), (False, .12)] * 2   # two short = dog started
BEEP_STOP = [(True, .5), (False, .1)]          # one long = stopped / timed out


def angdiff(a, b):
    d = a - b
    return math.atan2(math.sin(d), math.cos(d))


def clamp01(x):
    return max(0.0, min(1.0, x))


class DogWalk(Node):
    def __init__(self):
        super().__init__('dog_walk')
        self.declare_parameter('max_speed', 0.5)   # ceiling; -p max_speed:=0.4 to tame
        self.max_speed = self.get_parameter('max_speed').value

        latched = QoSProfile(depth=1,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.pub_cmd = self.create_publisher(Twist, '/cmd_vel_dog', 10)
        self.pub_active = self.create_publisher(Bool, '/dog_active', latched)
        self.pub_beep = self.create_publisher(Bool, '/Buzzer', 10)
        self.cancel_cli = self.create_client(
            CancelGoal, '/navigate_to_pose/_action/cancel_goal')
        self.create_subscription(LaserScan, '/scan', self.on_scan,
                                 qos_profile_sensor_data)
        self.create_subscription(Empty, '/dog_toggle', self.on_toggle, 10)
        self.create_subscription(Empty, '/dog_stop', self.on_stop, 10)
        self.create_subscription(Twist, '/cmd_vel_joy', self.on_joy_cmd, 10)

        self.scan = None
        self.scan_t = -1.0
        self.active = False
        self.t_start = 0.0

        # per-state working memory
        self.state = 'wander'
        self.state_t = 0.0
        self.tb = 0.0                 # smoothed wander target bearing
        self.drift = 0.0
        self.gait_phase = 0.0         # triangle-wave pace phase [0,1)
        self.wander_goal = 0.0        # current random exploration heading
        self.retarget_at = 0.0        # next time to re-pick wander_goal
        self.cand = None             # last leg candidate (multi-frame match)
        self.cand_n = 0
        self.target = None           # locked approach target {bearing,range,n}
        self.seen_t = 0.0
        self.sniff_bearing = 0.0
        self.turn_sign = 1.0
        self.turn_dur = 0.0
        self.suppress_bearing = 0.0
        self.suppress_until = 0.0

        self.beep_seq = []
        self.beep_timer = None
        self.pub_active.publish(Bool(data=False))   # boot default: idle
        self.create_timer(1.0 / RATE, self.tick)
        self.get_logger().info(f'idle (max_speed={self.max_speed})')

    def now(self):
        return self.get_clock().now().nanoseconds / 1e9

    # -- inputs -----------------------------------------------------------
    def on_scan(self, msg):
        self.scan = msg
        self.scan_t = self.now()

    def on_joy_cmd(self, msg):
        # Grabbing the stick is takeover, not a temporary yield: exit active.
        if self.active and (msg.linear.x or msg.linear.y or msg.angular.z):
            self.deactivate('takeover')

    def on_toggle(self, _):
        # No brake gate: dog-walk is lidar-driven itself (own avoidance +
        # fail-close on stale scan), so it never leans on or touches the
        # safety_stop switch. R1 just flips active.
        if self.active:
            self.deactivate('toggle')
        else:
            self.activate()

    def on_stop(self, _):
        if self.active:                # idempotent: no-op when already idle
            self.deactivate('stop')

    # -- activation -------------------------------------------------------
    def activate(self):
        if self.cancel_cli.service_is_ready():
            self.cancel_cli.call_async(CancelGoal.Request())  # drop stale P2 goal
        self.active = True
        self.t_start = self.now()
        self.cand, self.cand_n, self.target = None, 0, None
        self.tb, self.drift = 0.0, 0.0
        self.suppress_until = 0.0
        self.retarget_at = 0.0         # pick a random heading on the first tick
        self.enter('wander')
        self.pub_active.publish(Bool(data=True))
        self.beep(BEEP_START)
        self.get_logger().info('dog-walk on')

    def deactivate(self, cause):
        self.active = False
        self.pub_cmd.publish(Twist())          # one explicit stop
        self.pub_active.publish(Bool(data=False))
        self.beep(BEEP_STOP)
        self.get_logger().info(f'dog-walk off ({cause})')

    def enter(self, state):
        self.state = state
        self.state_t = self.now()

    # -- 10 Hz loop -------------------------------------------------------
    def tick(self):
        if not self.active:
            return                             # idle: mux watchdog holds zeros
        now = self.now()
        if now - self.t_start > HARD_TIMEOUT:
            self.deactivate('timeout')
            return
        if self.scan is None or now - self.scan_t > SCAN_FRESH:
            self.pub_cmd.publish(Twist())      # blind -> stop
            if self.scan is None or now - self.scan_t > SCAN_LOST:
                self.deactivate('scan_lost')
            return
        self.pub_cmd.publish(getattr(self, 'st_' + self.state)())

    # -- states -----------------------------------------------------------
    def st_wander(self):
        cand = self.nearest_leg()
        if cand and self.cand and self.match(cand, self.cand):
            self.cand_n += 1
        else:
            self.cand_n = 1 if cand else 0
        self.cand = cand
        if cand and self.cand_n >= LEG_FRAMES:
            self.target, self.seen_t = cand, self.now()
            self.enter('approach')
            return self.steer(0.0)
        # re-pick a weighted-random open heading now and then (dog-like wander),
        # then steer toward it, smoothed + a slow random drift
        now = self.now()
        if now >= self.retarget_at:
            b = self.weighted_open_bearing()
            self.wander_goal = b if b is not None else 0.0
            self.retarget_at = now + random.uniform(RETARGET_MIN, RETARGET_MAX)
        self.drift += random.uniform(-DRIFT_STEP, DRIFT_STEP)
        self.drift = max(-DRIFT_MAX, min(DRIFT_MAX, self.drift))
        self.tb += (1.0 - TB_LP) * angdiff(self.wander_goal + self.drift, self.tb)
        # triangle-wave gait: uniform ramp between GAIT_MIN and full pace
        self.gait_phase = (self.gait_phase + 1.0 / (RATE * GAIT_PERIOD)) % 1.0
        tri = 2 * self.gait_phase if self.gait_phase < 0.5 else 2 * (1 - self.gait_phase)
        gait = GAIT_MIN + (1.0 - GAIT_MIN) * tri
        # full pace once ~0.8 m is clear ahead (was 1.2 m, which crawled in a
        # cluttered room); tapers to 0 at 0.35 m, just above the sniff/brake floor
        vx = self.max_speed * clamp01((self.front_clear() - 0.35) / 0.45) * gait
        return self.steer(vx, self.tb)

    def st_approach(self):
        now = self.now()
        cand = self.nearest_leg()
        if cand and self.match(cand, self.target):
            self.target, self.seen_t = cand, now
        if self.target['range'] <= APPROACH_STOP:
            self.sniff_bearing = self.target['bearing']
            self.enter('sniff')
            return self.steer(0.0)
        if now - self.seen_t > LOST_T or now - self.state_t > APPROACH_MAX:
            self.enter('wander')               # lost / dragged on -> give up
            return self.steer(0.0)
        return self.steer(APPROACH_V, self.target['bearing'])

    def st_sniff(self):
        elapsed = self.now() - self.state_t
        if elapsed > SNIFF_T:
            self.enter('backup')
            return self.steer(0.0)
        t = Twist()                            # stand still, wag the "head"
        t.angular.z = SNIFF_WAG * math.sin(2 * math.pi * SNIFF_F * elapsed)
        return t

    def st_backup(self):
        # Check the rear FIRST; if blocked, skip backing up and just turn.
        rear = self.sector_min(math.pi, REAR_HALF)
        if rear > REAR_MIN and self.now() - self.state_t < BACKUP_T:
            t = Twist()
            t.linear.x = -BACKUP_V
            return t
        self.begin_turn()
        return self.steer(0.0)

    def st_turn(self):
        if self.now() - self.state_t >= self.turn_dur:
            self.enter('wander')
            return self.steer(0.0)
        t = Twist()
        t.angular.z = self.turn_sign * TURN_WZ
        return t

    def begin_turn(self):
        target = self.pick_turn_bearing()
        self.turn_sign = 1.0 if target >= 0 else -1.0
        self.turn_dur = abs(target) / TURN_WZ  # no odom: time-based in-place turn
        self.suppress_bearing = self.sniff_bearing
        self.suppress_until = self.now() + SUPPRESS_T
        self.enter('turn')

    # -- perception -------------------------------------------------------
    def steer(self, vx, bearing=0.0):
        t = Twist()
        t.linear.x = vx
        t.angular.z = max(-WZ_MAX, min(WZ_MAX, K_YAW * bearing))
        return t

    def match(self, a, b):
        return (abs(angdiff(a['bearing'], b['bearing'])) < MATCH_ANG
                and abs(a['range'] - b['range']) < MATCH_RNG)

    def sector_min(self, direction, half):
        """Min valid range within ±half of `direction` (robot frame); inf if
        the sector holds no valid return."""
        best = math.inf
        a0, inc = self.scan.angle_min, self.scan.angle_increment
        for i, r in enumerate(self.scan.ranges):
            if not math.isfinite(r) or r <= RANGE_VALID:
                continue
            a = angdiff(a0 + i * inc, direction)
            if abs(a) <= half and r < best:
                best = r
        return best

    def front_clear(self):
        return self.sector_min(0.0, FRONT_HALF)

    def open_bearing(self):
        """Bucket the forward ±90° arc into 15° bins (value = min valid range,
        inf if empty) and return the center bearing of the most-open bin."""
        BIN = math.radians(15)
        nb = 12
        mins = [math.inf] * nb
        a0, inc = self.scan.angle_min, self.scan.angle_increment
        for i, r in enumerate(self.scan.ranges):
            if not math.isfinite(r) or r <= RANGE_VALID:
                continue
            a = math.atan2(math.sin(a0 + i * inc), math.cos(a0 + i * inc))
            if abs(a) > math.radians(90):
                continue
            idx = min(nb - 1, int((a + math.radians(90)) / BIN))
            mins[idx] = min(mins[idx], r)
        best = max(range(nb), key=lambda k: mins[k])
        return -math.radians(90) + (best + 0.5) * BIN, mins

    def weighted_open_bearing(self, avoid=None, avoid_half=0.0):
        """Randomly pick a forward open sector, weighted by its openness (an
        emptier direction is likelier but not certain — that randomness is what
        makes the wander dog-like). Skips sectors within avoid_half of `avoid`.
        Returns None if nothing qualifies."""
        BIN = math.radians(15)
        _, mins = self.open_bearing()
        choices = []
        for idx, m in enumerate(mins):
            c = -math.radians(90) + (idx + 0.5) * BIN
            openness = m if math.isfinite(m) else TURN_OPEN_CAP
            if openness <= TURN_OPEN_MIN:
                continue
            if avoid is not None and abs(angdiff(c, avoid)) < avoid_half:
                continue
            choices.append((c, openness))
        if not choices:
            return None
        r = random.uniform(0.0, sum(w for _, w in choices))
        for c, w in choices:
            r -= w
            if r <= 0.0:
                return c
        return choices[-1][0]

    def pick_turn_bearing(self):
        """Biased-random open sector, away from the just-sniffed bearing."""
        b = self.weighted_open_bearing(self.sniff_bearing, TURN_AWAY)
        if b is None:                          # box canyon: about-face
            return math.pi if random.random() < 0.5 else -math.pi
        return b

    def nearest_leg(self):
        cands = self.find_legs()
        if self.suppress_until > self.now():
            cands = [c for c in cands
                     if abs(angdiff(c['bearing'], self.suppress_bearing)) > SUPPRESS_HALF]
        return min(cands, key=lambda c: c['range']) if cands else None

    def find_legs(self):
        """Contiguous in-band runs bounded by a step-up/invalid on both sides,
        kept only if their PHYSICAL width (2·r·sin(Δθ/2)) is leg-sized."""
        beams = self.beams(LEG_ARC)
        n = len(beams)
        cands = []
        i = 0
        while i < n:
            if not self.in_band(beams[i][1]):
                i += 1
                continue
            j = i
            while j + 1 < n and self.in_band(beams[j + 1][1]) and \
                    abs(beams[j + 1][1] - beams[j][1]) <= LEG_STEP:
                j += 1
            cand = self.leg_from_run(beams, i, j)
            if cand:
                cands.append(cand)
            i = j + 1
        return cands

    def leg_from_run(self, beams, i, j):
        if j - i + 1 < LEG_MIN_PTS:
            return None
        rng = min(beams[k][1] for k in range(i, j + 1))
        width = 2 * rng * math.sin(abs(beams[j][0] - beams[i][0]) / 2)
        if not (LEG_WMIN <= width <= LEG_WMAX):
            return None
        # Isolated only if the neighbors just outside are background (invalid,
        # arc edge, or farther by a step); a nearer neighbor = part of a wall.
        if not self.step_up(beams, i - 1, rng) or not self.step_up(beams, j + 1, rng):
            return None
        return {'bearing': (beams[i][0] + beams[j][0]) / 2,
                'range': rng, 'n': j - i + 1}

    def step_up(self, beams, k, rng):
        if k < 0 or k >= len(beams):
            return True                        # arc edge counts as a boundary
        r = beams[k][1]
        return r is None or r > rng + LEG_STEP

    def beams(self, half):
        """Forward ±half beams as (bearing, range|None), index order; invalid
        ranges become None so they read as run boundaries."""
        out = []
        a0, inc = self.scan.angle_min, self.scan.angle_increment
        for i, r in enumerate(self.scan.ranges):
            a = math.atan2(math.sin(a0 + i * inc), math.cos(a0 + i * inc))
            if abs(a) <= half:
                out.append((a, r if (math.isfinite(r) and r > RANGE_VALID) else None))
        return out

    @staticmethod
    def in_band(r):
        return r is not None and LEG_NEAR <= r <= LEG_FAR

    # -- buzzer (same stepped-sequence pattern as episode_recorder) -------
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
    rclpy.spin(DogWalk())


if __name__ == '__main__':
    main()
