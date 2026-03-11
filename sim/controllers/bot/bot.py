"""
Robot controller using native ROS2 for TurtleBot3.

Publishes camera images and subscribes to cmd_vel via ROS2 DDS.
Connects to Discovery Server specified in ROS_DISCOVERY_SERVER env var.
"""

from __future__ import annotations

import sys
import os
import threading
import time

# Debug output
def debug(msg):
    print(f"[BOT] {msg}", flush=True)

debug(f"Python: {sys.executable}")
debug(f"CWD: {os.getcwd()}")

try:
    from controller import Robot
    debug("controller import OK")
except ImportError as e:
    debug(f"controller import FAILED: {e}")
    sys.exit(1)

import contextlib
from pathlib import Path
from typing import Callable, Dict, List, Any

# Import ROS2
try:
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from geometry_msgs.msg import Twist
    from sensor_msgs.msg import Image
    from std_msgs.msg import String
    debug("rclpy import OK")
except ImportError as e:
    debug(f"rclpy import FAILED: {e}")
    rclpy = None


def parse_controller_args(args: list[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for arg in args:
        if "=" not in arg:
            continue
        key, value = arg.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def build_logger(robot_id: str) -> Callable[[str], None]:
    log_dir = Path(os.getenv("BOT_LOG_DIR", "/tmp/bot_logs"))
    with contextlib.suppress(OSError):
        log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{robot_id}.log"

    def _log(message: str) -> None:
        text = f"[{robot_id}] {message}"
        print(text, flush=True)
        with contextlib.suppress(OSError):
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")

    return _log


class ROS2Bridge:
    """Native ROS2 bridge for camera publishing and cmd_vel subscription."""

    def __init__(self, robot_id: str, log_fn: Callable[[str], None]):
        self.robot_id = robot_id
        self.log = log_fn
        self.node = None
        self.camera_pub = None
        self.cmd_sub = None
        self.latest_cmd = {"linear": 0.0, "angular": 0.0, "timestamp": 0.0}
        self._spin_thread = None
        self._publish_count = 0
        self._cmd_count = 0

        if rclpy is None:
            raise RuntimeError("rclpy not available")

        # Initialize ROS2
        self.log(f"Initializing ROS2 for {robot_id}")
        self.log(f"ROS_DISCOVERY_SERVER={os.getenv('ROS_DISCOVERY_SERVER', 'not set')}")
        self.log(f"RMW_IMPLEMENTATION={os.getenv('RMW_IMPLEMENTATION', 'not set')}")

        try:
            rclpy.init()
        except RuntimeError:
            # Already initialized
            pass

        self.node = rclpy.create_node(f"{robot_id}_controller")

        # QoS for camera - BEST_EFFORT for video
        camera_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1
        )

        # Create camera publisher
        camera_topic = f"/{robot_id}/camera/image_raw"
        self.camera_pub = self.node.create_publisher(Image, camera_topic, camera_qos)
        self.log(f"Publishing camera to {camera_topic}")

        # Create telemetry publisher for velocity feedback
        telemetry_topic = f"/{robot_id}/telemetry"
        self.telemetry_pub = self.node.create_publisher(String, telemetry_topic, 10)
        self.log(f"Publishing telemetry to {telemetry_topic}")

        # Create cmd_vel subscriber (use default QoS for reliability)
        cmd_topic = f"/{robot_id}/cmd_vel"
        self.cmd_sub = self.node.create_subscription(
            Twist,
            cmd_topic,
            self._cmd_callback,
            10
        )
        self.log(f"Subscribed to {cmd_topic}")

        # Spin in background thread
        self._spin_thread = threading.Thread(target=self._spin, daemon=True)
        self._spin_thread.start()
        self.log("ROS2 bridge initialized")

    def _spin(self):
        """Background thread for ROS2 spinning."""
        while rclpy.ok():
            rclpy.spin_once(self.node, timeout_sec=0.01)
            time.sleep(0.001)

    def _cmd_callback(self, msg: Twist):
        """Handle incoming cmd_vel messages."""
        self.latest_cmd["linear"] = msg.linear.x
        self.latest_cmd["angular"] = msg.angular.z
        self.latest_cmd["timestamp"] = time.time()
        self._cmd_count += 1
        self.log(f"CMD #{self._cmd_count}: linear={msg.linear.x:.3f} angular={msg.angular.z:.3f}")

    def get_cmd(self) -> Dict[str, float]:
        """Get latest command."""
        return self.latest_cmd

    def publish_camera(self, camera: Any, sim_time: float):
        """Publish camera image."""
        if self.camera_pub is None:
            return

        image_data = camera.getImage()
        if image_data is None:
            return

        width = camera.getWidth()
        height = camera.getHeight()

        msg = Image()
        msg.header.stamp.sec = int(sim_time)
        msg.header.stamp.nanosec = int((sim_time - int(sim_time)) * 1e9)
        msg.header.frame_id = f"{self.robot_id}_camera"
        msg.height = height
        msg.width = width
        msg.encoding = "bgra8"
        msg.is_bigendian = False
        msg.step = width * 4
        msg.data = bytes(image_data)

        self.camera_pub.publish(msg)
        self._publish_count += 1
        if self._publish_count % 30 == 0:
            self.log(f"published {self._publish_count} camera frames")

    def publish_telemetry(self, linear_speed: float, angular_speed: float):
        """Publish velocity telemetry."""
        if self.telemetry_pub is None:
            return
        import json
        data = {
            "linear_speed": linear_speed,
            "angular_speed": angular_speed,
            "timestamp": time.time()
        }
        msg = String()
        msg.data = json.dumps(data)
        self.telemetry_pub.publish(msg)

    def shutdown(self):
        """Cleanup ROS2 resources."""
        if self.node:
            self.node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:
            pass


class ObstacleDetector:
    """Obstacle detection using LiDAR."""

    def __init__(self, robot: Robot, timestep: int, log_fn: Callable[[str], None]):
        self.robot = robot
        self.timestep = timestep
        self.log = log_fn
        self.lidar: Any = None

        try:
            lidar = robot.getDevice("LDS-01")
            if lidar:
                lidar.enable(timestep)
                lidar.enablePointCloud()
                self.lidar = lidar
                self.log("Using LiDAR (LDS-01) for obstacle detection")

                for motor_name in ["LDS-01_main_motor", "LDS-01_secondary_motor"]:
                    try:
                        motor = robot.getDevice(motor_name)
                        if motor:
                            motor.setPosition(float("inf"))
                            motor.setVelocity(30.0)
                    except Exception:
                        pass
        except Exception:
            self.log("WARNING: No LiDAR found")


def main() -> None:
    robot = Robot()
    args = parse_controller_args(sys.argv[1:])
    if args:
        os.environ.update(args)

    robot_id = args.get("ROBOT_ID", robot.getName())
    log = build_logger(robot_id)

    log(f"Starting controller for {robot_id}")

    timestep = int(robot.getBasicTimeStep())

    # Initialize motors
    left_motor = robot.getDevice("left wheel motor")
    right_motor = robot.getDevice("right wheel motor")
    left_motor.setPosition(float("inf"))
    right_motor.setPosition(float("inf"))

    # Start idle — wait for commands
    left_motor.setVelocity(0.0)
    right_motor.setVelocity(0.0)
    log("Motors initialized - idle")

    # Initialize obstacle detection
    obstacle_detector = ObstacleDetector(robot, timestep, log)

    # Debug: list all devices
    device_count = robot.getNumberOfDevices()
    log(f"Robot has {device_count} devices:")
    for i in range(device_count):
        device = robot.getDeviceByIndex(i)
        log(f"  Device {i}: {device.getName()}")

    # Initialize camera
    camera: Any = None
    for camera_name in ["camera", "Camera"]:
        try:
            cam = robot.getDevice(camera_name)
            if cam:
                cam.enable(timestep)
                camera = cam
                log(f"Camera '{camera_name}' enabled")
                break
        except Exception as e:
            log(f"Error getting camera '{camera_name}': {e}")

    # Initialize ROS2 bridge
    ros_bridge: ROS2Bridge | None = None
    if rclpy is not None:
        try:
            ros_bridge = ROS2Bridge(robot_id, log)
        except Exception as exc:
            log(f"ROS2 bridge init failed: {exc}")
    else:
        log("ROS2 not available - running without ROS integration")

    # Settings
    command_timeout = 2.0  # Increased timeout
    frame_interval = max(1, int(100 / timestep))  # ~10 fps
    frame_tick = 0
    motor_log_interval = 100  # Log motor state every N iterations
    loop_count = 0

    log("Entering main loop")
    log(f"Timestep: {timestep}ms")

    # Log motor max velocity to check limits
    log(f"Left motor max velocity: {left_motor.getMaxVelocity()}")
    log(f"Right motor max velocity: {right_motor.getMaxVelocity()}")

    # Track current velocities (persist between loops)
    current_left = 0.0
    current_right = 0.0
    last_cmd_time = 0.0
    stop_timeout = 5.0  # Stop after 5 seconds of no commands

    step_count = 0
    while robot.step(timestep) != -1:
        step_count += 1
        if step_count <= 5 or step_count % 500 == 0:
            log(f"Step {step_count} - motors at L={left_motor.getVelocity():.2f} R={right_motor.getVelocity():.2f}")
        loop_count += 1

        # Move when commands received
        if ros_bridge:
            cmd = ros_bridge.get_cmd()
            now = time.time()
            time_since_cmd = now - cmd["timestamp"] if cmd["timestamp"] > 0 else 999

            # Debug every 100 loops
            if loop_count % 100 == 0:
                log(f"DEBUG: timestamp={cmd['timestamp']:.2f} now={now:.2f} age={time_since_cmd:.2f}s linear={cmd['linear']:.3f}")

            if cmd["timestamp"] > 0 and cmd["timestamp"] > last_cmd_time:
                # New command received
                last_cmd_time = cmd["timestamp"]
                linear = cmd["linear"]
                angular = cmd["angular"]
                # Convert twist to differential drive
                # TurtleBot3 Burger: max ~6.67 rad/s, wheel_separation ~0.16m
                base_speed = linear * 6.67
                turn = angular * 3.0
                current_left = base_speed - turn
                current_right = base_speed + turn
                log(f"MOTOR: L={current_left:.2f} R={current_right:.2f} (age={time_since_cmd:.2f}s)")

            # Stop if no commands for a while
            if now - last_cmd_time > stop_timeout and last_cmd_time > 0:
                current_left = 0.0
                current_right = 0.0

        # Use current velocities
        left_speed = current_left
        right_speed = current_right

        # Clamp speeds to motor max (TurtleBot3 Burger max ~6.67 rad/s)
        max_speed = 6.67
        left_speed = max(-max_speed, min(max_speed, left_speed))
        right_speed = max(-max_speed, min(max_speed, right_speed))

        left_motor.setVelocity(left_speed)
        right_motor.setVelocity(right_speed)

        # Log actual motor state periodically
        if loop_count % 200 == 0 and (left_speed != 0 or right_speed != 0):
            log(f"VELOCITY SET: L={left_speed:.2f} R={right_speed:.2f}")

        # Stream camera frames and telemetry
        if camera and ros_bridge:
            frame_tick += 1
            if frame_tick % frame_interval == 0:
                ros_bridge.publish_camera(camera, robot.getTime())
                # Convert wheel speeds back to linear/angular for telemetry
                # Linear = (left + right) / 2, Angular = (right - left) / wheel_base
                # Using simplified conversion (inverse of what we use to convert twist to wheel speeds)
                linear_cmd = (left_speed + right_speed) / 2.0 / 20.0
                angular_cmd = (right_speed - left_speed) / 2.0 / 10.0
                ros_bridge.publish_telemetry(linear_cmd, angular_cmd)

    # Cleanup
    if ros_bridge:
        ros_bridge.shutdown()


if __name__ == "__main__":
    main()
