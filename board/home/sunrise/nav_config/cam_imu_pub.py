#!/usr/bin/env python3
"""Camera IMU bridge: the GS130WI stereo module's on-board ICM-42688-P
(6-axis, I2C 0x68 on the same bus as the calibration EEPROM) ->
sensor_msgs/Imu on /camera/imu.

Pure-stdlib I2C (/dev/i2c-N + I2C_SLAVE ioctl, no smbus module needed).
The chip powers up in standby; PWR_MGMT0=0x0F enables accel+gyro in
low-noise mode at the default FSR (gyro +/-2000 dps, accel +/-16 g,
1 kHz internal ODR) and we sample the data registers at IMU_HZ.

Axes are the raw sensor frame of the module (frame_id camera_imu) —
no mounting rotation is applied here; consumers own extrinsics.
No orientation estimate either (orientation_covariance[0] = -1).
"""
import fcntl
import math
import os

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Imu

I2C_SLAVE = 0x0703
ADDR = 0x68
REG_WHO_AM_I, WHO_AM_I = 0x75, 0x47
REG_PWR_MGMT0 = 0x4E
REG_DATA = 0x1D                 # TEMP(2) ACCEL xyz(6) GYRO xyz(6), big-endian
ACC_LSB = 2048.0                # LSB per g at +/-16 g
GYR_LSB = 16.4                  # LSB per dps at +/-2000 dps
G = 9.80665
IMU_HZ = float(os.environ.get('IMU_HZ', 50))


def s16(hi, lo):
    v = (hi << 8) | lo
    return v - 65536 if v & 0x8000 else v


def open_imu():
    """The module's I2C bus follows the CSI ribbon (4 or 6) — probe both."""
    for bus in (6, 4):
        try:
            fd = os.open(f'/dev/i2c-{bus}', os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.ioctl(fd, I2C_SLAVE, ADDR)
            os.write(fd, bytes([REG_WHO_AM_I]))
            if os.read(fd, 1)[0] == WHO_AM_I:
                os.write(fd, bytes([REG_PWR_MGMT0, 0x0F]))
                return fd, bus
        except OSError:
            pass
        os.close(fd)
    return None, None


class CamImu(Node):
    def __init__(self):
        super().__init__('cam_imu')
        self.pub = self.create_publisher(Imu, '/camera/imu',
                                         qos_profile_sensor_data)
        self.fd, bus = open_imu()
        if self.fd is None:
            raise RuntimeError('ICM-42688-P not found on i2c 6/4')
        self.get_logger().info(f'ICM-42688-P up on i2c-{bus}, {IMU_HZ:.0f}Hz')
        self.errs = 0
        self.create_timer(1.0 / IMU_HZ, self.tick)

    def tick(self):
        try:
            os.write(self.fd, bytes([REG_DATA]))
            d = os.read(self.fd, 14)
            self.errs = 0
        except OSError:
            # transient bus contention with the sensor/EEPROM users; a dead
            # bus (ribbon unplugged) exits and the supervisor respawns us
            self.errs += 1
            if self.errs > int(5 * IMU_HZ):
                raise RuntimeError('I2C dead for 5s')
            return
        m = Imu()
        m.header.stamp = self.get_clock().now().to_msg()
        m.header.frame_id = 'camera_imu'
        m.orientation_covariance[0] = -1.0      # no orientation estimate
        m.linear_acceleration.x = s16(d[2], d[3]) / ACC_LSB * G
        m.linear_acceleration.y = s16(d[4], d[5]) / ACC_LSB * G
        m.linear_acceleration.z = s16(d[6], d[7]) / ACC_LSB * G
        m.angular_velocity.x = math.radians(s16(d[8], d[9]) / GYR_LSB)
        m.angular_velocity.y = math.radians(s16(d[10], d[11]) / GYR_LSB)
        m.angular_velocity.z = math.radians(s16(d[12], d[13]) / GYR_LSB)
        self.pub.publish(m)


def main():
    rclpy.init()
    node = CamImu()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        os.close(node.fd)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
