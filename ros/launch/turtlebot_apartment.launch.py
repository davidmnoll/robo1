"""Launch file for TurtleBot3 apartment simulation with native ROS2."""

import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.substitutions import LaunchConfiguration
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    # Get package directories
    webots_ros2_turtlebot_dir = get_package_share_directory('webots_ros2_turtlebot')

    # Path to our custom world file
    world_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'sim', 'worlds', 'turtlebot_apartment.wbt'
    )

    # Launch arguments
    use_sim_time = LaunchConfiguration('use_sim_time', default='true')

    return LaunchDescription([
        DeclareLaunchArgument(
            'use_sim_time',
            default_value='true',
            description='Use simulation time'
        ),

        # Launch Webots with TurtleBot3 driver for bot_alpha
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(webots_ros2_turtlebot_dir, 'launch', 'robot_launch.py')
            ),
            launch_arguments={
                'world': world_file,
                'robot_name': 'bot_alpha',
            }.items()
        ),
    ])
