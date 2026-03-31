#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from std_msgs.msg import UInt8MultiArray
import pyaudio


class RobotAudioPlayer(Node):
    def __init__(self):
        super().__init__("robot_audio_player")

        self.p = pyaudio.PyAudio()
        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=44100,
            output=True,
            frames_per_buffer=1024,
        )

        self.subscription = self.create_subscription(
            UInt8MultiArray, "/audio_raw", self.audio_callback, 10
        )
        self.get_logger().info("Playing robot audio... Ctrl+C to stop")

    def audio_callback(self, msg):
        self.stream.write(bytes(msg.data))


def main():
    rclpy.init()
    node = RobotAudioPlayer()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.stream.stop_stream()
    node.stream.close()
    node.p.terminate()
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
