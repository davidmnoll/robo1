#!/usr/bin/env python3
"""
Audio Stream Node - Microphone to ROS2

Captures audio from the microphone and publishes to /{robot_id}/audio_raw.
Used for robot-to-operator voice communication.
"""

import array
import os
import re
import socket
import struct
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


class AudioStreamNode(Node):
    """ROS2 node that captures microphone audio and publishes to a topic."""

    # Audio parameters - USB audio typically only supports 44100/48000 Hz
    SAMPLE_RATE = 48000
    CHANNELS = 1
    CHUNK_SIZE = 9600  # 100ms of audio at 48kHz mono S16_LE (48000 * 0.1 * 2 bytes)
    AUDIO_GAIN = 4.0  # Software gain multiplier (adjust as needed)

    def __init__(self, robot_id: str = None):
        self.robot_id = robot_id or get_robot_id()
        super().__init__(f"audio_stream_{self.robot_id}")

        # QoS for audio - BEST_EFFORT for low latency (drop frames rather than queue)
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)

        # Publisher for audio data
        topic = f"/{self.robot_id}/audio_raw"
        self.publisher = self.create_publisher(UInt8MultiArray, topic, qos)
        self.get_logger().info(f"Publishing audio to {topic}")

        # Start arecord process
        self.proc = None
        self._start_capture()

        # Timer to read audio and publish (~10 Hz for 100ms chunks)
        self.create_timer(0.1, self.capture_audio)

        self.get_logger().info(
            f"AudioStreamNode initialized for robot_id={self.robot_id}"
        )

    def _start_capture(self) -> None:
        """Start the audio capture subprocess."""
        try:
            self.proc = subprocess.Popen(
                [
                    "arecord",
                    "-D", "hw:2,0",  # Explicitly specify USB audio device
                    "-f", "S16_LE",
                    "-r", str(self.SAMPLE_RATE),
                    "-c", str(self.CHANNELS),
                    "-q",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,  # Capture stderr for debugging
            )
            self.get_logger().info("Started audio capture with arecord")
        except Exception as e:
            self.get_logger().error(f"Failed to start arecord: {e}")
            self.proc = None

    def capture_audio(self) -> None:
        """Read audio data and publish."""
        if self.proc is None or self.proc.poll() is not None:
            # Process died, try to restart (but not too often)
            if not hasattr(self, '_last_restart'):
                self._last_restart = 0
                self._restart_count = 0
            import time
            now = time.time()

            # Log why process died
            if self.proc is not None:
                exit_code = self.proc.poll()
                stderr_out = ""
                try:
                    stderr_out = self.proc.stderr.read().decode('utf-8', errors='ignore')[:200]
                except Exception:
                    pass
                self.get_logger().warning(
                    f"arecord died with code {exit_code}: {stderr_out}"
                )

            if now - self._last_restart < 5.0:
                self._restart_count += 1
                if self._restart_count > 3:
                    # Too many restarts, back off
                    return
            else:
                self._restart_count = 0
            self._last_restart = now
            self.get_logger().info("Restarting arecord...")
            self._start_capture()
            return

        try:
            data = self.proc.stdout.read(self.CHUNK_SIZE)
            if data:
                # Apply software gain to boost audio levels
                if self.AUDIO_GAIN != 1.0:
                    # Unpack S16_LE samples, apply gain, clip to prevent overflow, repack
                    num_samples = len(data) // 2
                    samples = struct.unpack(f'<{num_samples}h', data)
                    gained = []
                    for s in samples:
                        v = int(s * self.AUDIO_GAIN)
                        # Clip to 16-bit signed range
                        v = max(-32768, min(32767, v))
                        gained.append(v)
                    data = struct.pack(f'<{num_samples}h', *gained)

                msg = UInt8MultiArray()
                # Use array.array instead of list() for much better performance
                msg.data = array.array('B', data)
                self.publisher.publish(msg)
        except Exception as e:
            self.get_logger().error(f"Error reading audio: {e}")

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
    node = AudioStreamNode(robot_id)

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
