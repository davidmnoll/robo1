#!/usr/bin/env python3
"""
Rosmaster A1 Controller Node

A ROS2 node that subscribes to /{robot_id}/cmd_vel and controls the Rosmaster A1
motors using the Rosmaster_Lib library.

Features:
- Subscribes to Twist messages on /{robot_id}/cmd_vel
- Converts linear.x and angular.z to differential drive motor commands
- Publishes telemetry to /{robot_id}/telemetry
- Auto-stop on command timeout (safety feature)
- Velocity clamping to safe limits
"""

import json
import os
import re
import socket
import time
from pathlib import Path

import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import String, Int32MultiArray

# Try to import the Rosmaster library - it may not be available in dev environments
try:
    from Rosmaster_Lib import Rosmaster
    ROSMASTER_AVAILABLE = True
except ImportError:
    ROSMASTER_AVAILABLE = False
    Rosmaster = None


def load_env_file(env_path: str = None) -> None:
    """Load environment variables from .env file.

    Checks in order:
    1. Provided env_path
    2. /root/.env (inside Docker)
    3. Same directory as this script
    """
    paths_to_check = []
    if env_path:
        paths_to_check.append(Path(env_path))
    paths_to_check.extend([
        Path("/root/.env"),
        Path(__file__).parent / ".env",
    ])

    for path in paths_to_check:
        if path.exists():
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key and key not in os.environ:
                            os.environ[key] = value
            break


def get_robot_id() -> str:
    """Get the robot ID from environment or hostname, sanitized for ROS2."""
    robot_id = os.environ.get("ROBOT_ID")
    if not robot_id:
        robot_id = socket.gethostname()
    # Sanitize: replace hyphens, remove illegal chars
    robot_id = robot_id.replace("-", "_")
    robot_id = re.sub(r"[^a-zA-Z0-9_]", "", robot_id)
    # Ensure starts with letter
    if robot_id and not robot_id[0].isalpha():
        robot_id = "bot_" + robot_id
    return robot_id or "robot"


class RosmasterController(Node):
    """ROS2 node that controls Rosmaster A1 motors based on cmd_vel messages."""

    # Velocity limits (m/s and rad/s)
    MAX_LINEAR_SPEED = 0.5  # m/s
    MAX_ANGULAR_SPEED = 2.0  # rad/s

    # Timeout for auto-stop (seconds)
    CMD_TIMEOUT = 0.5

    # Telemetry publish rate (Hz)
    TELEMETRY_RATE = 10.0

    # Camera servo limits (degrees)
    SERVO_MIN_ANGLE = 0
    SERVO_MAX_ANGLE = 180
    SERVO_CENTER = 90

    # Servo IDs for 2-axis camera
    SERVO_PAN = 1   # Horizontal rotation
    SERVO_TILT = 2  # Vertical rotation

    def __init__(self, robot_id: str = None):
        self.robot_id = robot_id or get_robot_id()
        super().__init__(f"rosmaster_controller_{self.robot_id}")

        # Initialize Rosmaster hardware interface
        self.bot = None
        if ROSMASTER_AVAILABLE:
            try:
                self.bot = Rosmaster()
                self.bot.create_receive_threading()
                self.get_logger().info("Rosmaster hardware initialized")
            except Exception as e:
                self.get_logger().error(f"Failed to initialize Rosmaster: {e}")
                self.bot = None
        else:
            self.get_logger().warning(
                "Rosmaster_Lib not available - running in simulation mode"
            )

        # Current velocity state
        self.current_linear_x = 0.0
        self.current_angular_z = 0.0
        self.last_cmd_time = 0.0

        # Current camera servo angles
        self.camera_pan = self.SERVO_CENTER
        self.camera_tilt = self.SERVO_CENTER

        # QoS for cmd_vel - use BEST_EFFORT for low latency
        cmd_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)

        # Subscribe to cmd_vel
        cmd_topic = f"/{self.robot_id}/cmd_vel"
        self.cmd_sub = self.create_subscription(
            Twist, cmd_topic, self.cmd_callback, cmd_qos
        )
        self.get_logger().info(f"Subscribed to {cmd_topic}")

        # Subscribe to camera_ptz (pan/tilt control)
        # Expects Int32MultiArray with [pan, tilt] in degrees (0-180)
        ptz_topic = f"/{self.robot_id}/camera_ptz"
        self.ptz_sub = self.create_subscription(
            Int32MultiArray, ptz_topic, self.ptz_callback, cmd_qos
        )
        self.get_logger().info(f"Subscribed to {ptz_topic}")

        # Center camera on startup
        self._set_camera_servo(self.SERVO_PAN, self.SERVO_CENTER)
        self._set_camera_servo(self.SERVO_TILT, self.SERVO_CENTER)

        # Publisher for telemetry
        telemetry_topic = f"/{self.robot_id}/telemetry"
        self.telemetry_pub = self.create_publisher(String, telemetry_topic, 10)
        self.get_logger().info(f"Publishing telemetry to {telemetry_topic}")

        # Timer for safety timeout check and telemetry
        self.create_timer(1.0 / self.TELEMETRY_RATE, self.timer_callback)

        self.get_logger().info(
            f"RosmasterController initialized for robot_id={self.robot_id}"
        )

    def cmd_callback(self, msg: Twist) -> None:
        """Handle incoming Twist command messages."""
        # Clamp velocities to safe limits
        linear_x = max(-self.MAX_LINEAR_SPEED, min(self.MAX_LINEAR_SPEED, msg.linear.x))
        angular_z = max(
            -self.MAX_ANGULAR_SPEED, min(self.MAX_ANGULAR_SPEED, msg.angular.z)
        )

        self.current_linear_x = linear_x
        self.current_angular_z = angular_z
        self.last_cmd_time = time.time()

        # Send command to motors
        self._set_motion(linear_x, angular_z)

        self.get_logger().debug(
            f"Received cmd_vel: linear_x={linear_x:.3f}, angular_z={angular_z:.3f}"
        )

    def ptz_callback(self, msg: Int32MultiArray) -> None:
        """Handle incoming camera pan/tilt command messages.

        Expects Int32MultiArray with data = [pan, tilt] in degrees (0-180).
        """
        if len(msg.data) < 2:
            self.get_logger().warning(f"Invalid camera_ptz message: expected [pan, tilt], got {msg.data}")
            return

        pan = max(self.SERVO_MIN_ANGLE, min(self.SERVO_MAX_ANGLE, msg.data[0]))
        tilt = max(self.SERVO_MIN_ANGLE, min(self.SERVO_MAX_ANGLE, msg.data[1]))

        if pan != self.camera_pan:
            self.camera_pan = pan
            self._set_camera_servo(self.SERVO_PAN, pan)

        if tilt != self.camera_tilt:
            self.camera_tilt = tilt
            self._set_camera_servo(self.SERVO_TILT, tilt)

        self.get_logger().debug(f"Camera PTZ: pan={pan}, tilt={tilt}")

    def _set_camera_servo(self, servo_id: int, angle: int) -> None:
        """Set camera servo position."""
        if self.bot is not None:
            try:
                self.bot.set_pwm_servo(servo_id, angle)
            except Exception as e:
                self.get_logger().error(f"Failed to set servo {servo_id}: {e}")

    def _set_motion(self, linear_x: float, angular_z: float) -> None:
        """Send motion command to the Rosmaster hardware."""
        if self.bot is not None:
            try:
                # Rosmaster set_car_motion(linear_x, linear_y, angular_z)
                # For differential drive: linear_y = 0
                self.bot.set_car_motion(linear_x, 0, angular_z)
            except Exception as e:
                self.get_logger().error(f"Failed to set motion: {e}")

    def timer_callback(self) -> None:
        """Periodic callback for safety timeout and telemetry."""
        current_time = time.time()

        # Check for command timeout - stop if no commands received
        if self.last_cmd_time > 0 and current_time - self.last_cmd_time > self.CMD_TIMEOUT:
            if self.current_linear_x != 0.0 or self.current_angular_z != 0.0:
                self.get_logger().debug("Command timeout - stopping motors")
                self.current_linear_x = 0.0
                self.current_angular_z = 0.0
                self._set_motion(0.0, 0.0)

        # Publish telemetry
        telemetry = {
            "linear_speed": self.current_linear_x,
            "angular_speed": self.current_angular_z,
            "camera_pan": self.camera_pan,
            "camera_tilt": self.camera_tilt,
            "timestamp": current_time,
        }

        # Add battery info if available
        if self.bot is not None:
            try:
                battery = self.bot.get_battery_voltage()
                if battery is not None:
                    telemetry["battery_voltage"] = battery
            except Exception:
                pass

        msg = String()
        msg.data = json.dumps(telemetry)
        self.telemetry_pub.publish(msg)

    def stop(self) -> None:
        """Stop the robot motors."""
        self.get_logger().info("Stopping motors")
        self.current_linear_x = 0.0
        self.current_angular_z = 0.0
        self._set_motion(0.0, 0.0)

    def destroy_node(self) -> None:
        """Clean up when node is destroyed."""
        self.stop()
        super().destroy_node()


def main(args=None):
    # Load .env file before anything else
    load_env_file()

    # Set ROS_DOMAIN_ID if specified in environment
    domain_id = os.environ.get("ROS_DOMAIN_ID")
    if domain_id:
        os.environ["ROS_DOMAIN_ID"] = domain_id
        print(f"Using ROS_DOMAIN_ID={domain_id}")

    rclpy.init(args=args)

    robot_id = get_robot_id()
    print(f"Starting controller for robot_id={robot_id}")
    node = RosmasterController(robot_id)

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
