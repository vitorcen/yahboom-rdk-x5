# Chassis bringup with cmd_vel arbitration (twist_mux).
# Replaces vendor yahboomcar_bringup_launch.py (kept untouched) with two changes:
#   1. yahboom_joy publishes /cmd_vel_joy instead of /cmd_vel
#   2. Mcnamu_driver listens on /cmd_vel_drv, fed only by twist_mux
# Topology:  joy -> /cmd_vel_joy (prio 100) \
#                                            > twist_mux -> /cmd_vel_drv -> driver
#            Nav2/keyboard -> /cmd_vel (prio 10) /
import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    # respawn: the vendor driver dies on unguarded I2C errors (RGBLightcallback,
    # OSError 121). A dead driver leaves the MCU executing its last velocity —
    # respawn + the mux's continuous idle zeros bring the chassis back to stop.
    driver_node = Node(
        package='yahboomcar_bringup',
        executable='Mcnamu_driver',
        remappings=[('cmd_vel', '/cmd_vel_drv')],
        respawn=True,
        respawn_delay=1.0,
    )

    pub_odom_tf_arg = DeclareLaunchArgument('pub_odom_tf', default_value='false',
                                            description='Whether base_node publishes odom TF')
    base_node = Node(
        package='yahboomcar_base_node',
        executable='base_node',
        parameters=[{'pub_odom_tf': LaunchConfiguration('pub_odom_tf')}],
        output='screen',
    )

    imu_filter_config = os.path.join(
        get_package_share_directory('yahboomcar_bringup'), 'params', 'imu_filter_param.yaml')
    imu_filter_node = Node(
        package='imu_filter_madgwick',
        executable='imu_filter_madgwick_node',
        parameters=[imu_filter_config],
        name='imu_filter_madgwick',
    )

    ekf_node = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([os.path.join(
            get_package_share_directory('robot_localization'), 'launch'), '/ekf.launch.py'])
    )

    joy_node = Node(package='joy', executable='joy_node')
    yahboom_joy_node = Node(
        package='yahboomcar_ctrl',
        executable='yahboom_joy',
        remappings=[('cmd_vel', '/cmd_vel_joy')],
    )

    description_node = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([os.path.join(
            get_package_share_directory('yahboomcar_description'), 'launch'),
            '/description_launch.py'])
    )

    # Own 50-line mux with watchdog (ros-humble-twist-mux unavailable on the
    # tuna mirror; ours also brakes on command silence — twist_mux does not).
    from launch.actions import ExecuteProcess
    mux_node = ExecuteProcess(
        cmd=['python3', os.path.join(os.path.dirname(__file__), 'cmd_vel_mux.py')],
        output='screen',
    )

    return LaunchDescription([
        pub_odom_tf_arg,
        driver_node, base_node, imu_filter_node, ekf_node,
        joy_node, yahboom_joy_node, description_node,
        mux_node,
    ])
