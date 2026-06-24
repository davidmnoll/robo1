#!/usr/bin/env python3
"""
Audio Stream Node - Microphone to ROS2

Captures audio from the microphone and publishes to /{robot_id}/audio_raw.
Used for robot-to-operator voice communication.

Includes echo cancellation via audio ducking - mutes mic when speaker audio
is received, then ramps back up after speaker goes quiet.
"""

import array
import os
import re
import socket
import struct
import subprocess
import threading

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import UInt8MultiArray


class EchoCanceller:
    """Simple mic muting when speaker is playing.

    Binary mute - no ramping, no gain changes. Just silence or pass-through.
    """

    def __init__(self, mute_duration_ms: float = 500, logger=None):
        import time
        self._time = time
        self.mute_duration_sec = mute_duration_ms / 1000.0
        self._last_loud_time = 0.0
        self.lock = threading.Lock()
        self.logger = logger

    def speaker_audio_received(self, samples: np.ndarray) -> None:
        """Called when speaker audio frame is received."""
        self._speaker_count = getattr(self, '_speaker_count', 0) + 1
        rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2))

        # Log every 50 frames to confirm subscription is working
        if self.logger and self._speaker_count % 50 == 1:
            self.logger.info(f"[AEC] Speaker frame #{self._speaker_count}, rms={rms:.0f}")

        # Only mute if audio has significant amplitude
        if rms > 300:
            with self.lock:
                self._last_loud_time = self._time.time()
            if self.logger:
                self.logger.info(f"[AEC] MUTING mic (speaker rms={rms:.0f})")

    def process(self, mic_samples: np.ndarray) -> np.ndarray:
        """Return silence if speaker was recently loud, otherwise pass through."""
        self._process_count = getattr(self, '_process_count', 0) + 1

        with self.lock:
            time_since_loud = self._time.time() - self._last_loud_time

        if time_since_loud < self.mute_duration_sec:
            if self.logger:
                self.logger.info(f"[AEC] SILENCING mic #{self._process_count} (time_since={time_since_loud:.3f}s)")
            # Return silence - same shape and dtype as input
            return np.zeros_like(mic_samples)

        # Pass through unchanged
        return mic_samples


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
    ENABLE_AEC = True  # Enable echo cancellation

    def __init__(self, robot_id: str = None):
        self.robot_id = robot_id or get_robot_id()
        super().__init__(f"audio_stream_{self.robot_id}")

        # QoS for audio - BEST_EFFORT for low latency (drop frames rather than queue)
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)

        # Publisher for audio data
        topic = f"/{self.robot_id}/audio_raw"
        self.publisher = self.create_publisher(UInt8MultiArray, topic, qos)
        self.get_logger().info(f"Publishing audio to {topic}")

        # Echo cancellation - subscribe to speaker audio
        if self.ENABLE_AEC:
            self.echo_canceller = EchoCanceller(mute_duration_ms=1000, logger=self.get_logger())
            speaker_topic = f"/{self.robot_id}/audio_play"
            self.speaker_sub = self.create_subscription(
                UInt8MultiArray, speaker_topic, self._on_speaker_audio, qos
            )
            self.get_logger().info(f"AEC enabled, monitoring speaker on {speaker_topic}")
        else:
            self.echo_canceller = None

        # Start arecord process
        self.proc = None
        self._start_capture()

        # Timer to read audio and publish (~10 Hz for 100ms chunks)
        self.create_timer(0.1, self.capture_audio)

        self.get_logger().info(
            f"AudioStreamNode initialized for robot_id={self.robot_id}"
        )

    def _on_speaker_audio(self, msg: UInt8MultiArray) -> None:
        """Called when speaker audio is about to play - notify echo canceller."""
        self._speaker_cb_count = getattr(self, '_speaker_cb_count', 0) + 1
        if self._speaker_cb_count % 50 == 1:
            self.get_logger().info(f"[AEC] Received speaker msg #{self._speaker_cb_count}, len={len(msg.data)}")

        if self.echo_canceller is None:
            return
        try:
            data = bytes(msg.data)
            samples = np.frombuffer(data, dtype=np.int16)
            self.echo_canceller.speaker_audio_received(samples)
        except Exception as e:
            self.get_logger().warning(f"Error processing speaker audio: {e}")

    def _start_capture(self) -> None:
        """Start the audio capture subprocess."""
        try:
            self.proc = subprocess.Popen(
                [
                    "arecord",
                    "-D", "plughw:2,0",  # USB audio device (plughw for format conversion)
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
                # Convert to numpy for processing
                samples = np.frombuffer(data, dtype=np.int16).copy()

                # Apply software gain
                if self.AUDIO_GAIN != 1.0:
                    samples = np.clip(
                        samples.astype(np.float32) * self.AUDIO_GAIN,
                        -32768, 32767
                    ).astype(np.int16)

                # Apply echo cancellation
                if self.echo_canceller is not None:
                    samples = self.echo_canceller.process(samples)

                # Convert back to bytes and publish
                msg = UInt8MultiArray()
                msg.data = array.array('B', samples.tobytes())
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
