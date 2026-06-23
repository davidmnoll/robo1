#!/usr/bin/env python3
"""
Audio Play Node - ROS2 to Speaker

Subscribes to /{robot_id}/audio_play and plays audio through the speaker.
Used for operator-to-robot voice communication.
"""

import os
import re
import socket
import subprocess

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import UInt8MultiArray


def get_robot_id() -> str:
    """Get the robot ID from environment or hostname, sanitized for ROS2."""
    robot_id = os.environ.get("ROBOT_ID")
    if not robot_id:
        robot_id = socket.gethostname()
    robot_id = robot_id.replace("-", "_")
    robot_id = re.sub(r"[^a-zA-Z0-9_]", "", robot_id)
    if robot_id and not robot_id[0].isalpha():
        robot_id = "bot_" + robot_id
    return robot_id or "robot"


class AudioPlayNode(Node):
    """ROS2 node that receives audio messages and plays them through the speaker."""

    # Audio parameters - must match audio_stream.py
    SAMPLE_RATE = 48000
    CHANNELS = 1

    def __init__(self, robot_id: str = None):
        self.robot_id = robot_id or get_robot_id()
        super().__init__(f"audio_play_{self.robot_id}")

        # Start aplay process
        self.proc = None
        self._start_playback()

        # QoS for audio - BEST_EFFORT for low latency
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)

        # Subscribe to audio topic
        topic = f"/{self.robot_id}/audio_play"
        self.subscription = self.create_subscription(
            UInt8MultiArray, topic, self.play_audio, qos
        )
        self.get_logger().info(f"Listening for audio on {topic}")

        self.get_logger().info(
            f"AudioPlayNode initialized for robot_id={self.robot_id}"
        )

    def _start_playback(self) -> None:
        """Start the audio playback subprocess."""
        try:
            self.proc = subprocess.Popen(
                [
                    "aplay",
                    "-f", "S16_LE",
                    "-r", str(self.SAMPLE_RATE),
                    "-c", str(self.CHANNELS),
                    "-q",
                    "-",
                ],
                stdin=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self.get_logger().info("Started audio playback with aplay")
        except Exception as e:
            self.get_logger().error(f"Failed to start aplay: {e}")
            self.proc = None

    def play_audio(self, msg: UInt8MultiArray) -> None:
        """Play received audio data."""
        if self.proc is None or self.proc.poll() is not None:
            # Process died, restart
            self.get_logger().warning("aplay process died, restarting...")
            self._start_playback()
            if self.proc is None:
                return

        try:
            self.proc.stdin.write(bytes(msg.data))
            self.proc.stdin.flush()
        except BrokenPipeError:
            self.get_logger().warning("Broken pipe to aplay, restarting...")
            self._start_playback()
        except Exception as e:
            self.get_logger().error(f"Error playing audio: {e}")

    def destroy_node(self) -> None:
        """Clean up subprocess."""
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)

    robot_id = get_robot_id()
    node = AudioPlayNode(robot_id)

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
