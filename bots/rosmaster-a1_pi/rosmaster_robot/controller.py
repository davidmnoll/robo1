#!/usr/bin/env python3
"""
Rosmaster A1 Controller Node

A ROS2 node that subscribes to /{robot_id}/joy and controls the Rosmaster A1
motors using the Rosmaster_Lib library.

Features:
- Subscribes to Joy messages on /{robot_id}/joy
- Implements velocity ramping (accelerate when input held, decelerate when released)
- Converts joystick axes to differential drive motor commands
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
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Joy
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
    """ROS2 node that controls Rosmaster A1 motors based on joy messages.

    Implements velocity ramping:
    - When joy input is held, accelerate toward max speed
    - When joy input is released, decelerate back to zero
    """

    # Velocity limits
    MAX_LINEAR_SPEED = 0.5  # m/s (for R2: max is 1.8)
    # Note: Steering is now -1 to +1, converted to servo angle in _set_motion

    # Acceleration/deceleration rates (per second) - only for linear velocity
    LINEAR_ACCEL = 1.0      # m/s^2 - how fast to accelerate
    LINEAR_DECEL = 2.0      # m/s^2 - how fast to decelerate (faster than accel)

    # Timeout for auto-stop (seconds) - if no joy messages received
    CMD_TIMEOUT = 0.5

    # Control loop rate (Hz) - higher = smoother ramping
    CONTROL_RATE = 50.0

    # Telemetry publish rate (Hz)
    TELEMETRY_RATE = 10.0

    # Servo limits (degrees)
    SERVO_MIN_ANGLE = 0
    SERVO_MAX_ANGLE = 180
    SERVO_CENTER = 90

    # Servo IDs
    SERVO_STEERING = 1  # Ackermann front wheel steering
    SERVO_PAN = 2       # Camera horizontal rotation (if available)
    SERVO_TILT = 3      # Camera vertical rotation (if available)

    # Steering servo range (degrees from center)
    # Adjust these based on your robot's steering geometry
    STEERING_MAX_ANGLE = 30  # Max degrees left/right from center

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

        # Target linear velocity (ramped)
        self.target_linear_x = 0.0
        self.current_linear_x = 0.0

        # Steering (direct, not ramped - car-like steering)
        self.current_steering = 0.0

        # Timing
        self.last_joy_time = 0.0
        self.last_control_time = time.time()
        self.telemetry_counter = 0

        # Current camera servo angles
        self.camera_pan = self.SERVO_CENTER
        self.camera_tilt = self.SERVO_CENTER

        # QoS for joy - use RELIABLE to match publisher
        joy_qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE)

        # Subscribe to joy
        joy_topic = f"/{self.robot_id}/joy"
        self.joy_sub = self.create_subscription(
            Joy, joy_topic, self.joy_callback, joy_qos
        )
        self.get_logger().info(f"Subscribed to {joy_topic}")

        # Subscribe to camera_ptz (pan/tilt control)
        # Expects Int32MultiArray with [pan, tilt] in degrees (0-180)
        ptz_topic = f"/{self.robot_id}/camera_ptz"
        self.ptz_sub = self.create_subscription(
            Int32MultiArray, ptz_topic, self.ptz_callback, joy_qos
        )
        self.get_logger().info(f"Subscribed to {ptz_topic}")

        # Center camera on startup
        self._set_camera_servo(self.SERVO_PAN, self.SERVO_CENTER)
        self._set_camera_servo(self.SERVO_TILT, self.SERVO_CENTER)

        # Publisher for telemetry
        telemetry_topic = f"/{self.robot_id}/telemetry"
        self.telemetry_pub = self.create_publisher(String, telemetry_topic, 10)
        self.get_logger().info(f"Publishing telemetry to {telemetry_topic}")

        # Timer for control loop (ramping) - runs at high rate
        self.create_timer(1.0 / self.CONTROL_RATE, self.control_callback)

        # Telemetry is published from control callback at lower rate
        self.telemetry_interval = int(self.CONTROL_RATE / self.TELEMETRY_RATE)

        self.get_logger().info(
            f"RosmasterController initialized for robot_id={self.robot_id}"
        )

    def joy_callback(self, msg: Joy) -> None:
        """Handle incoming Joy messages.

        Joy axes mapping:
        - axes[1]: forward/back (-1 to 1, positive = forward)
        - axes[3]: turn (-1 to 1, positive = left)
        """
        axes = msg.axes if msg.axes else []

        # Extract axes with defaults
        fwd_back = axes[1] if len(axes) > 1 else 0.0
        turn = axes[3] if len(axes) > 3 else 0.0

        # Set target linear velocity (will be ramped)
        self.target_linear_x = fwd_back * self.MAX_LINEAR_SPEED

        # Set steering directly (-1 to +1, converted to servo angle in _set_motion)
        self.current_steering = turn

        self.last_joy_time = time.time()

        # Log occasionally to avoid flooding (every 20th message = ~1Hz at 20Hz input)
        if not hasattr(self, '_joy_msg_count'):
            self._joy_msg_count = 0
        self._joy_msg_count += 1
        if self._joy_msg_count % 20 == 1:
            self.get_logger().info(
                f"Joy #{self._joy_msg_count}: fwd={fwd_back:.2f}, turn={turn:.2f} -> "
                f"lin={self.target_linear_x:.2f}, steer={self.current_steering:.2f}"
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

    def _set_motion(self, linear_x: float, steering: float) -> None:
        """Send motion command to the Rosmaster hardware.

        For Ackermann steering:
        - linear_x: forward/backward velocity
        - steering: -1.0 (full right) to +1.0 (full left), controls front steering servo
        """
        if self.bot is not None:
            try:
                # Log occasionally to debug (every ~1 second at 50Hz)
                if not hasattr(self, '_motion_count'):
                    self._motion_count = 0
                self._motion_count += 1

                # Convert steering (-1 to +1) to servo angle
                # Center = 90, left = 90 - max_angle, right = 90 + max_angle
                steering_angle = int(self.SERVO_CENTER - steering * self.STEERING_MAX_ANGLE)
                steering_angle = max(self.SERVO_MIN_ANGLE, min(self.SERVO_MAX_ANGLE, steering_angle))

                if self._motion_count % 50 == 1 or (steering != 0 and self._motion_count % 10 == 1):
                    self.get_logger().info(
                        f"Motion #{self._motion_count}: linear_x={linear_x:.3f}, "
                        f"steering={steering:.2f} -> servo_angle={steering_angle}"
                    )

                # Set drive motors (linear only, no angular for Ackermann)
                self.bot.set_car_motion(linear_x, 0, 0)

                # Set steering servo
                self.bot.set_pwm_servo(self.SERVO_STEERING, steering_angle)

            except Exception as e:
                self.get_logger().error(f"Failed to set motion: {e}")

    def control_callback(self) -> None:
        """High-rate control loop for velocity ramping."""
        current_time = time.time()
        dt = current_time - self.last_control_time
        self.last_control_time = current_time

        # Check for joy timeout - if no joy messages, stop
        if self.last_joy_time > 0 and current_time - self.last_joy_time > self.CMD_TIMEOUT:
            self.target_linear_x = 0.0
            self.current_steering = 0.0

        # Ramp linear velocity toward target
        self.current_linear_x = self._ramp_velocity(
            self.current_linear_x,
            self.target_linear_x,
            self.LINEAR_ACCEL,
            self.LINEAR_DECEL,
            dt,
        )

        # Steering is direct (no ramping for car-like steering)
        # Send command to motors
        self._set_motion(self.current_linear_x, self.current_steering)

        # Publish telemetry at lower rate
        self.telemetry_counter += 1
        if self.telemetry_counter >= self.telemetry_interval:
            self.telemetry_counter = 0
            self._publish_telemetry(current_time)

    def _ramp_velocity(
        self, current: float, target: float, accel: float, decel: float, dt: float
    ) -> float:
        """Ramp current velocity toward target with acceleration/deceleration limits.

        Uses higher deceleration rate when:
        - Target is zero (releasing input)
        - Moving in opposite direction of target (reversing)
        """
        diff = target - current

        if abs(diff) < 0.001:
            return target

        # Determine which rate to use
        # Use decel when: target is zero, or current and target have opposite signs
        if target == 0 or (current * target < 0):
            rate = decel
        else:
            rate = accel

        # Calculate max change for this timestep
        max_change = rate * dt

        if abs(diff) <= max_change:
            return target
        elif diff > 0:
            return current + max_change
        else:
            return current - max_change

    def _publish_telemetry(self, current_time: float) -> None:
        """Publish telemetry data."""
        telemetry = {
            "linear_speed": self.current_linear_x,
            "steering": self.current_steering,
            "target_linear": self.target_linear_x,
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
        self.current_steering = 0.0
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
