"""Launch file for Rosmaster A1 robot."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration, EnvironmentVariable
from launch_ros.actions import Node


def generate_launch_description():
    # Declare arguments
    robot_id_arg = DeclareLaunchArgument(
        'robot_id',
        default_value=EnvironmentVariable('ROBOT_ID', default_value='rosmaster1'),
        description='Robot identifier for topic namespacing'
    )

    camera_device_arg = DeclareLaunchArgument(
        'camera_device',
        default_value='/dev/video0',
        description='Camera device path'
    )

    camera_width_arg = DeclareLaunchArgument(
        'camera_width',
        default_value='320',  # Reduced from 640 to lower CPU load
        description='Camera image width'
    )

    camera_height_arg = DeclareLaunchArgument(
        'camera_height',
        default_value='240',  # Reduced from 480 to lower CPU load
        description='Camera image height'
    )

    camera_fps_arg = DeclareLaunchArgument(
        'camera_fps',
        default_value='10.0',  # Reduced from 15 to lower CPU load on Pi
        description='Camera framerate'
    )

    robot_id = LaunchConfiguration('robot_id')
    camera_device = LaunchConfiguration('camera_device')
    camera_width = LaunchConfiguration('camera_width')
    camera_height = LaunchConfiguration('camera_height')
    camera_fps = LaunchConfiguration('camera_fps')

    # Controller node
    controller_node = Node(
        package='rosmaster_robot',
        executable='controller',
        name='controller',
        output='screen',
        parameters=[{
            'robot_id': robot_id,
        }],
    )

    # Audio stream node (microphone)
    audio_stream_node = Node(
        package='rosmaster_robot',
        executable='audio_stream',
        name='audio_stream',
        output='screen',
    )

    # Audio play node (speaker)
    audio_play_node = Node(
        package='rosmaster_robot',
        executable='audio_play',
        name='audio_play',
        output='screen',
    )

    # USB camera node - use YUYV format (camera native, converted to RGB)
    # Disable auto controls to avoid crashes on unsupported cameras
    camera_node = Node(
        package='usb_cam',
        executable='usb_cam_node_exe',
        name='camera',
        output='screen',
        parameters=[{
            'video_device': camera_device,
            'image_width': camera_width,
            'image_height': camera_height,
            'framerate': camera_fps,
            'pixel_format': 'yuyv2rgb',
            # Disable auto controls that may not be supported
            'brightness': -1,
            'contrast': -1,
            'saturation': -1,
            'sharpness': -1,
            'autoexposure': False,
            'auto_white_balance': False,
        }],
        remappings=[
            ('image_raw', ['/', robot_id, '/camera/image_raw']),
        ],
    )

    return LaunchDescription([
        robot_id_arg,
        camera_device_arg,
        camera_width_arg,
        camera_height_arg,
        camera_fps_arg,
        controller_node,
        audio_stream_node,
        audio_play_node,
        camera_node,
    ])
