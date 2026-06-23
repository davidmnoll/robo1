"""
Launch ROS2 drivers for TurtleBot3 robots in Webots.
Run this AFTER starting Webots with the apartment world.
"""

import os
from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    # Get the URDF for TurtleBot3
    turtlebot_urdf_path = os.path.join(
        get_package_share_directory('webots_ros2_turtlebot'),
        'resource',
        'turtlebot_webots.urdf'
    )

    with open(turtlebot_urdf_path, 'r') as f:
        robot_description = f.read()

    # Driver for bot_alpha
    driver_alpha = Node(
        package='webots_ros2_driver',
        executable='driver',
        name='turtlebot_driver',
        namespace='bot_alpha',
        output='screen',
        parameters=[
            {'robot_description': robot_description},
            {'use_sim_time': True},
            {'set_robot_state_publisher': True},
        ],
        additional_env={'WEBOTS_ROBOT_NAME': 'bot_alpha'},
    )

    # Driver for bot_beta
    driver_beta = Node(
        package='webots_ros2_driver',
        executable='driver',
        name='turtlebot_driver',
        namespace='bot_beta',
        output='screen',
        parameters=[
            {'robot_description': robot_description},
            {'use_sim_time': True},
            {'set_robot_state_publisher': True},
        ],
        additional_env={'WEBOTS_ROBOT_NAME': 'bot_beta'},
    )

    return LaunchDescription([
        driver_alpha,
        driver_beta,
    ])
