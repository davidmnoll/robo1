"""
Launch file for TurtleBot3 apartment simulation with native ROS2.
Launches Webots and ROS2 drivers for two TurtleBots.
"""

import os
import pathlib
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, LogInfo
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory

# Try to import webots_ros2_driver, fall back gracefully
try:
    from webots_ros2_driver.webots_launcher import WebotsLauncher
    from webots_ros2_driver.webots_controller import WebotsController
    WEBOTS_ROS2_AVAILABLE = True
except ImportError:
    WEBOTS_ROS2_AVAILABLE = False


def generate_launch_description():
    # Get the directory of this launch file
    launch_dir = pathlib.Path(__file__).parent.resolve()
    ros_dir = launch_dir.parent
    project_dir = ros_dir.parent
    world_file = str(project_dir / 'sim' / 'worlds' / 'turtlebot_apartment.wbt')

    if not WEBOTS_ROS2_AVAILABLE:
        return LaunchDescription([
            LogInfo(msg='ERROR: webots_ros2_driver not found. Install with: sudo apt install ros-humble-webots-ros2')
        ])

    # Robot descriptions (URDF) for the TurtleBot3
    turtlebot_urdf = os.path.join(
        get_package_share_directory('webots_ros2_turtlebot'),
        'resource',
        'turtlebot_webots.urdf'
    )

    # Read URDF content
    with open(turtlebot_urdf, 'r') as f:
        robot_description = f.read()

    # Launch Webots simulator
    webots = WebotsLauncher(
        world=world_file,
        mode='realtime',
        ros2_supervisor=True,
    )

    # TurtleBot Alpha driver
    turtlebot_alpha_driver = WebotsController(
        robot_name='bot_alpha',
        parameters=[
            {'robot_description': robot_description},
            {'use_sim_time': True},
        ],
        namespace='bot_alpha',
        respawn=True,
    )

    # TurtleBot Beta driver
    turtlebot_beta_driver = WebotsController(
        robot_name='bot_beta',
        parameters=[
            {'robot_description': robot_description},
            {'use_sim_time': True},
        ],
        namespace='bot_beta',
        respawn=True,
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'use_sim_time',
            default_value='true',
            description='Use simulation time'
        ),

        # Start Webots
        webots,
        webots._supervisor,

        # Start robot drivers
        turtlebot_alpha_driver,
        turtlebot_beta_driver,

        # Camera forwarder (forwards frames to cloud API)
        Node(
            package='robot_bridge',
            executable='camera_forwarder',
            name='camera_forwarder',
            parameters=[
                {'use_sim_time': True},
            ],
            output='screen',
        ),
    ])
